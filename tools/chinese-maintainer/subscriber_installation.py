"""Recognize signed subscriber outputs; never use rollback bytes as official input.

maintain prepares signed metadata online. Diagnostics only read it and reverify
the existing signature, journal, protected files and exact current output bytes.
"""
import base64
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from common import ROOT, fingerprint, read_json, regular, require

LAYERS = {'card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle'}


def journal_path():
    root = Path(os.environ.get('MTGA_SUBSCRIBER_STATE', str(
        Path.home() / 'Library/Application Support/MTGAChineseSubscriber'))).expanduser()
    require(root.is_absolute() and root == root.resolve(), 'Subscriber state path is not canonical')
    return root / 'installation.json'


def applicable(snapshot):
    path = journal_path()
    if not path.exists():
        return None
    journal = read_json(path)
    if (journal.get('game'), journal.get('client_version'), journal.get('steam_build')) != (
            snapshot['game'], snapshot['client_version'], snapshot['steam_build']):
        return None
    require(journal.get('schema') == 'MTGA_CN_SUBSCRIBER_TX_V1' and
            journal.get('state') == 'installed',
            'Subscriber installation is incomplete or has an unknown schema/state')
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}',
                              journal.get('release_id', ''))), 'Invalid subscriber release identity')
    return journal


def verify_manifest(path, client, public_file):
    expected_key = read_json(ROOT.parent / 'subscriber/profile.json')['public_key']
    require(regular(public_file).read_text().strip() == expected_key, 'Subscriber recognition public key changed')
    regular(path)
    regular(client)
    result = subprocess.run([str(client), 'verify-envelope', '--input', str(path),
                             '--key', str(public_file)], text=True, capture_output=True)
    require(result.returncode == 0, 'Subscriber recognition signature verification failed')
    return json.loads(base64.b64decode(read_json(path)['payload'], validate=True))


def manifest_path(state, journal):
    return Path(state) / 'subscriber-installation' / (journal['release_id'] + '.json')


def prepare(config, snapshot, state):
    """Fetch public signed metadata only; never alter either installation state."""
    from official_base import opaque_baseline
    try:
        opaque = opaque_baseline(snapshot, state)
        clean = all(
            {key: snapshot['files'][layer][key] for key in ('sha256', 'bytes')} == (
                opaque[layer]['fingerprint'] if layer in opaque else
                snapshot['official_entries'][Path(snapshot['files'][layer]['relative']).name]['fingerprint'])
            for layer in LAYERS)
    except (OSError, RuntimeError, KeyError, ValueError):
        clean = False
    if clean:
        return  # Only all five verified originals can skip installation metadata.
    journal = applicable(snapshot)
    if journal is None:
        return
    client, public_file = config['client'], config['public_key_file']
    destination = manifest_path(state, journal)
    if destination.is_file():
        verify_manifest(destination, client, public_file)
        return
    sys.path.insert(0, str(ROOT.parent / 'subscriber'))
    from github_publish import download, check, validate_repo
    repo = config.get('repo', '1996Neil/mtga-chinese-updates')
    validate_repo(repo)
    destination.parent.mkdir(parents=True, exist_ok=True)
    index_path = destination.parent / 'index-stable.json'
    feed = f'https://raw.githubusercontent.com/{repo}/main/feed'

    def fetch_verified(url, target, expected=None):
        # Failed transport/signature/hash checks must never leave a final cache
        # entry that prevents the next attempt from resuming normally.
        with tempfile.TemporaryDirectory(prefix='.fetch-', dir=target.parent) as directory:
            temporary = Path(directory) / target.name
            download(url, temporary, 2 * 1024 * 1024, 180)
            if expected is not None:
                check(temporary, expected)
            value = verify_manifest(temporary, client, public_file)
            temporary.replace(target)
            return value

    index = fetch_verified(feed + '/index-stable.json', index_path)
    require(index.get('schema') == 'MTGA_CN_INDEX_V1' and index.get('channel') == 'stable' and
            dt.datetime.fromisoformat(index['expires_at'].replace('Z', '+00:00')) >
            dt.datetime.now(dt.timezone.utc), 'Subscriber recognition stable index is invalid/expired')
    refs = [ref for ref in index['releases'] if ref['id'] == journal['release_id']]
    require(len(refs) == 1 and refs[0]['client_version'] == snapshot['client_version'] and
            refs[0]['steam_build'] == snapshot['steam_build'], 'Installed subscriber release is not in signed stable index')
    artifact = refs[0]['manifest']
    require(artifact['name'] == destination.name, 'Subscriber signed manifest identity mismatch')
    fetch_verified(feed + '/' + artifact['name'], destination, artifact['fingerprint'])


def recognize(snapshot, game, state):
    """Return exact signed installed-output identity, not official bytes or approval."""
    # No prepared metadata means diagnostics retain the original closed behavior.
    if not (Path(state) / 'subscriber-installation').is_dir():
        return None
    journal = applicable(snapshot)
    if journal is None:
        return None
    path = manifest_path(state, journal)
    require(path.is_file(), 'Signed subscriber manifest has not been prepared')
    client = Path(os.environ.get('MTGA_PUBLISHER_CLIENT', ''))
    public_file = Path(os.environ.get('MTGA_PUBLIC_KEY_FILE', ''))
    require(client.is_absolute() and public_file.is_absolute(),
            'Subscriber recognition needs the configured production verifier and public key')
    inputs = {str(p): fingerprint(p) for p in (journal_path(), path, client, public_file)}
    release = verify_manifest(path, client, public_file)
    require(release.get('schema') == 'MTGA_CN_RELEASE_V1' and
            release.get('acceptance', {}).get('status') == 'accepted' and
            all(release.get(field) == journal.get(field) for field in (
                'client_version', 'steam_build', 'revision')) and
            release.get('id') == journal['release_id'], 'Signed subscriber release does not match installation')
    resources, rows = release.get('resources', []), journal.get('rows', [])
    require(len(resources) == len(rows) == 5 and
            {r['layer'] for r in resources} == {r['layer'] for r in rows} == LAYERS,
            'Subscriber resource layer set is incomplete or duplicated')
    protected = [{'relative': row['relative'],
                  'fingerprint': {key: row[key] for key in ('sha256', 'bytes')}}
                 for name, row in snapshot['files'].items() if name not in LAYERS]
    protected += [{'relative': name, 'fingerprint': value}
                  for name, value in snapshot['manifests'].items()]
    require(release.get('protected_files') == journal.get('protected_files') == protected,
            'Subscriber protected files or active manifests changed')
    for item in protected:
        require(fingerprint(game / item['relative']) == item['fingerprint'], 'Subscriber protected bytes drifted')
    outputs, originals = {}, {}
    for item in resources:
        layer = item['layer']
        row = next(row for row in rows if row['layer'] == layer)
        current = snapshot['files'][layer]
        require(item['relative'] == row['relative'] == current['relative'] and
                row.get('original') == item.get('input') and
                row.get('after') == item.get('output') ==
                {key: current[key] for key in ('sha256', 'bytes')} and
                fingerprint(game / current['relative']) == item['output'],
                'Current subscriber output is unrecognized: ' + layer)
        outputs[layer] = item['output']
        originals[layer] = item['input']
    for name, expected in inputs.items():
        require(fingerprint(Path(name)) == expected, 'Subscriber recognition evidence drifted')
    return {'release_id': release['id'], 'inputs': inputs, 'outputs': outputs, 'originals': originals}
