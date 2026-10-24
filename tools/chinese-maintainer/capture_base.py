#!/usr/bin/env python3
"""Capture a new official opaque base after an official MTGA update.

The two opaque layers (resources.assets, sharedassets0.assets) carry no
publisher manifest hash, so official_base.py only trusts a previously
reviewed local capture pinned by release. This module produces the evidence;
it never decides on its own that a capture is trustworthy.

Judgement is delegated to an oracle that IS manifest-verified: the official
client localization database. Comparing the UI asset against it answers both
"is this the official English text" and "does this belong to this release",
which comparing against an old capture cannot.

Read-only against the game. Writes only under state/base-captures/<id>/.

    python capture_base.py --probe    # collect evidence + verdict
    python capture_base.py --stage    # also build the run directory
"""
import argparse
import datetime
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import UnityPy

from common import GAME, STATE, digest, fingerprint, has_chinese, read_json, require, save_json, managed_path, evidence_path

OPAQUE = ('resources', 'bootstrap_fonts')
ASSET_RELATIVE = {'resources': 'MTGA.app/Contents/Resources/Data/resources.assets',
                  'bootstrap_fonts': 'MTGA.app/Contents/Resources/Data/sharedassets0.assets'}
FONT_SLOTS = ('DFMARUGOTHICSTD-W3', 'MOTOYAAPOROSTD-W6')
# Seals of retired receipts, so an older generation can still be used as the
# comparison baseline after PIN_* has moved on to the newer capture.
PRIOR_RECEIPTS = {
    str(STATE / 'pro-handoff-20261010/protected-before-next.json'): {
        'sha256': 'f4b01918684cabd321737864f5c8c7f4b928ad1250b4aef53cffe06d87760357',
        'bytes': 30329},
}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_stamp():
    return utc_now().strftime('%Y%m%dT%H%M%S%fZ')


def is_cjk(text):
    """CJK ideographs; note this also matches Japanese kanji."""
    return bool(has_chinese(text))


def steam_state(game=GAME):
    acf = game.parent.parent / 'appmanifest_2141910.acf'
    text = acf.read_text(errors='replace')
    def field(name):
        m = __import__('re').search(r'"' + name + r'"\s+"([^"]*)"', text)
        return m.group(1) if m else None
    return {'path': str(acf), 'fingerprint': fingerprint(acf),
            'StateFlags': field('StateFlags'), 'UpdateResult': field('UpdateResult'),
            'buildid': field('buildid'), 'TargetBuildID': field('TargetBuildID'),
            'LastUpdated': field('LastUpdated'), 'LastPlayed': field('LastPlayed'),
            'BytesDownloaded': field('BytesDownloaded'),
            'BytesToDownload': field('BytesToDownload')}


def ui_oracle(snapshot, game=GAME):
    """Manifest-verified official English text for every client key."""
    path = game / snapshot['files']['client']['relative']
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        return dict(connection.execute('SELECT Key, enUS FROM Loc'))
    finally:
        connection.close()


def ui_text(assets):
    env = UnityPy.load(str(assets))
    rows = [o for o in env.objects
            if o.type.name == 'TextAsset' and o.read().m_Name == 'LocLibraryData']
    require(len(rows) == 1, 'Unknown UI localization object layout')
    data = json.loads(rows[0].read().m_Script)
    text = {}
    for row in data:
        slot = next((t['Translation'] for t in row['Translations'] if t['Language'] == 'en-US'), None)
        text[row['Key']] = slot
    return text


def tmp_fallback_pointers(assets):
    env = UnityPy.load(str(assets))
    rows = [o for o in env.objects
            if o.type.name == 'MonoBehaviour' and
            str(o.read_typetree(check_read=False).get('m_Name')) == 'TMP Settings']
    require(len(rows) == 1, 'Unknown TMP settings layout')
    raw = rows[0].get_raw_data()
    from tmp_settings import parse_settings
    fields, _, _ = parse_settings(raw)
    return [dict(r) for r in fields['m_fallbackFontAssets']]


def object_digests(assets):
    env = UnityPy.load(str(assets))
    return {o.path_id: digest(o.get_raw_data()) for o in env.objects}


def patched_deviation(current, official, patched):
    """Objects that left the official form, split by whether they took the patch form.

    Untouched objects equal both references, so only the deviating set is
    informative: patch residue is a deviation that matches the patched capture.
    """
    deviating = {key for key in current if key in official and current[key] != official[key]}
    like_patch = {key for key in deviating if key in patched and current[key] == patched[key]}
    return deviating, like_patch


def font_payload(assets):
    env = UnityPy.load(str(assets))
    result = {}
    for obj in env.objects:
        if obj.type.name != 'Font':
            continue
        data = obj.read()
        if data.m_Name in FONT_SLOTS:
            payload = data.m_FontData or b''
            if not isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload)
            result[data.m_Name] = {'m_FontNames': list(data.m_FontNames),
                                   'bytes': len(payload),
                                   'sha256': digest(payload)}
    require(set(result) == set(FONT_SLOTS), 'Unknown bootstrap font layout')
    return result


def pinned_references(run_name=None, receipt_path=None):
    """The prior reviewed capture, verified against its own pinned receipt.

    Pass the previous generation explicitly once a newer base is pinned, since
    PIN_RUN then points at the base being judged rather than at its predecessor.
    """
    from official_base import PIN_RECEIPT, PIN_RECEIPT_PATH, PIN_RUN
    receipt_path = managed_path(receipt_path).absolute() if receipt_path else PIN_RECEIPT_PATH
    expected_seal = PIN_RECEIPT
    if Path(receipt_path) != PIN_RECEIPT_PATH:
        expected_seal = PRIOR_RECEIPTS.get(str(receipt_path))
        require(expected_seal, f'No pinned seal known for prior receipt: {receipt_path}')
    require(fingerprint(receipt_path) == expected_seal, 'Pinned receipt changed')
    receipt = read_json(receipt_path)
    run = STATE / 'runs' / (run_name or PIN_RUN)
    official = {layer: run / 'official' / ASSET_RELATIVE[layer] for layer in OPAQUE}
    patched = {layer: run / Path(ASSET_RELATIVE[layer]).name for layer in OPAQUE}
    for label, path in [*official.items(), *patched.items()]:
        require(path.exists(), f'Reference capture missing: {path}')
        require(receipt.get(evidence_path(path)) == fingerprint(path),
                f'Reference capture not bound by pinned receipt: {path}')
    return official, patched


def probe(previous_snapshot=None):
    from detect import detect
    snapshot = detect()
    game = Path(snapshot['game'])
    evidence = {'schema': 'MTGA_CN_BASE_CAPTURE_PROBE_V2',
                'at': utc_now().isoformat(),
                'client_version': snapshot['client_version'],
                'steam_build': snapshot['steam_build'],
                'steam': steam_state(game),
                'layers': {}}
    for layer in OPAQUE:
        path = game / snapshot['files'][layer]['relative']
        row = {'relative': snapshot['files'][layer]['relative'],
               'fingerprint': fingerprint(path),
               'mtime_utc': datetime.datetime.fromtimestamp(
                   path.stat().st_mtime, datetime.timezone.utc).isoformat()}
        if layer == 'resources':
            row['tmp_fallback_pointers'] = tmp_fallback_pointers(path)
            row['objects'] = len(object_digests(path))
        else:
            row['fonts'] = font_payload(path)
        evidence['layers'][layer] = row
    evidence['snapshot'] = snapshot
    evidence['oracle'] = {'path': str(game / snapshot['files']['client']['relative']),
                          'fingerprint': snapshot['files']['client']}
    return evidence


def verdict(evidence, references, subject=None):
    """subject: an alternate root holding MTGA.app/...; defaults to the live game.

    The oracle always comes from the live manifest-verified client database,
    so an old capture placed in `subject` is judged against the new release.
    """
    official_ref, patched_ref = references
    snapshot = evidence['snapshot']
    live = Path(snapshot['game'])
    game = live if subject is None else Path(subject)
    ui_path = game / ASSET_RELATIVE['resources']
    boot_path = game / ASSET_RELATIVE['bootstrap_fonts']
    oracle = ui_oracle(snapshot, live)
    ui = ui_text(ui_path)
    checks = []

    def add(claim, method, result, passed):
        checks.append({'claim': claim, 'method': method, 'result': result, 'pass': bool(passed)})

    missing = sorted(k for k in ui if k not in oracle)
    differing = sorted(k for k in ui if k in oracle and oracle[k] != ui[k])
    add('UI en-US text equals the manifest-verified official client text',
        'compare every LocLibraryData en-US slot against Raw_ClientLocalization.Loc.enUS',
        {'ui_keys': len(ui), 'oracle_keys': len(oracle), 'missing': missing[:8],
         'missing_count': len(missing), 'differing': differing[:8],
         'differing_count': len(differing)},
        not missing and not differing)

    old_ui = ui_text(official_ref['resources'])
    add('UI asset belongs to the new release, not to the pinned old capture',
        'key-set difference against the pinned official capture; an uncovered '
        'old file would be byte-identical instead',
        {'old_keys': len(old_ui), 'new_keys': len(ui),
         'added': sorted(set(ui) - set(old_ui))[:8],
         'removed': sorted(set(old_ui) - set(ui))[:8]},
        set(ui) != set(old_ui))

    pointers = evidence['layers']['resources']['tmp_fallback_pointers']
    patched_pointers = tmp_fallback_pointers(patched_ref['resources'])
    add('UI asset carries no TMP fallback-font pointer from the Chinese patch',
        'the reviewed patch appends two pointers into sharedassets0; official has none',
        {'current': pointers, 'official': [], 'patched': patched_pointers},
        pointers == [] and patched_pointers != [])

    current_objects = object_digests(ui_path)
    patched_objects = object_digests(patched_ref['resources'])
    official_objects = object_digests(official_ref['resources'])
    deviating_ui, like_patch_ui = patched_deviation(current_objects, official_objects, patched_objects)
    add('No UI object deviates from official in the shape of the Chinese patch',
        'of the objects that differ from the pinned official capture, none may '
        'match the patched capture (untouched objects match both by definition)',
        {'current_objects': len(current_objects),
         'deviating_from_official': len(deviating_ui),
         'deviating_and_like_patch': sorted(like_patch_ui)[:8],
         'deviating_and_like_patch_count': len(like_patch_ui)},
        not like_patch_ui)

    fonts = evidence['layers']['bootstrap_fonts']['fonts']
    official_fonts = font_payload(official_ref['bootstrap_fonts'])
    add('Both bootstrap font slots are the official payload, not Heiti SC',
        'compare m_FontNames and m_FontData sha256, not the name alone',
        {'current': fonts, 'official': official_fonts},
        all(fonts[n]['m_FontNames'] == official_fonts[n]['m_FontNames'] and
            fonts[n]['sha256'] == official_fonts[n]['sha256'] for n in FONT_SLOTS))

    current_fonts = object_digests(boot_path)
    patched_fonts = object_digests(patched_ref['bootstrap_fonts'])
    current_official_fonts = object_digests(official_ref['bootstrap_fonts'])
    identical_to_old = [k for k in current_fonts if k in current_official_fonts
                        and current_fonts[k] == current_official_fonts[k]]
    add('Bootstrap asset belongs to the new release',
        'an uncovered old file would be identical object-for-object to the pinned capture',
        {'current_objects': len(current_fonts),
         'identical_to_old_capture': len(identical_to_old),
         'total_old_objects': len(current_official_fonts)},
        len(identical_to_old) < len(current_official_fonts))

    patched_identity = [k for k in current_fonts if k in patched_fonts
                        and current_fonts[k] == patched_fonts[k]
                        and k in current_official_fonts
                        and current_fonts[k] != current_official_fonts[k]]
    add('No bootstrap object deviates from official in the shape of the Chinese patch',
        'deviating objects that match the patched capture would carry patch residue',
        {'deviating_and_like_patch': len(patched_identity)},
        not patched_identity)

    steam = evidence['steam']
    add('Steam reports a completed install of the same build',
        'parse appmanifest_2141910.acf',
        {'StateFlags': steam['StateFlags'], 'UpdateResult': steam['UpdateResult'],
         'buildid': steam['buildid'], 'TargetBuildID': steam['TargetBuildID'],
         'expected_build': snapshot['steam_build']},
        steam['StateFlags'] == '4' and steam['UpdateResult'] in (None, '0')
        and steam['buildid'] == snapshot['steam_build']
        and steam['TargetBuildID'] in (None, snapshot['steam_build']))

    window = []
    for layer in OPAQUE:
        mtime = datetime.datetime.fromisoformat(evidence['layers'][layer]['mtime_utc'])
        updated = datetime.datetime.fromtimestamp(int(steam['LastUpdated']), datetime.timezone.utc)
        # Steam stamps LastUpdated when the update completed; the payload is
        # written shortly before. LastPlayed is deliberately not used: launching
        # the game afterwards (in-client content update) moves it past mtime.
        within = (updated - datetime.timedelta(hours=1)) <= mtime <= (updated + datetime.timedelta(minutes=5))
        window.append({'layer': layer, 'mtime_utc': mtime.isoformat(),
                       'steam_last_updated_utc': updated.isoformat(),
                       'inside_update_window': within})
    add('Both opaque files were written inside the Steam update window',
        'mtime falls within one hour before appmanifest LastUpdated',
        window, all(w['inside_update_window'] for w in window))

    return {'schema': 'MTGA_CN_BASE_CAPTURE_VERDICT_V2',
            'checks': checks, 'all_pass': all(c['pass'] for c in checks)}


def main():
    parser = argparse.ArgumentParser(description='Capture a new official opaque base')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--probe', action='store_true', help='collect evidence + verdict')
    group.add_argument('--stage', action='store_true', help='also write the run directory')
    parser.add_argument('--out', type=Path, help='evidence directory')
    parser.add_argument('--previous', help='previous PIN_RUN used as reference')
    parser.add_argument('--previous-receipt', type=Path,
                        help='receipt sealing the previous run (defaults to the current pin)')
    args = parser.parse_args()

    from official_base import PIN_RUN
    previous = read_json(STATE / 'runs' / (args.previous or PIN_RUN) / 'snapshot.json')
    evidence = probe(previous)
    references = pinned_references(args.previous or PIN_RUN, args.previous_receipt)
    evidence['verdict'] = verdict(evidence, references)

    out = managed_path(args.out or (STATE / 'base-captures' / evidence['at'].replace(':', '').replace('-', '')[:20]))
    out.mkdir(parents=True, exist_ok=True)
    save_json(out / 'probe.json', evidence)

    print(f"游戏版本：{evidence['client_version']}  build {evidence['steam_build']}")
    print(f"Steam：StateFlags={evidence['steam']['StateFlags']}  buildid={evidence['steam']['buildid']}")
    for check in evidence['verdict']['checks']:
        print(f"  [{'PASS' if check['pass'] else 'FAIL'}] {check['claim']}")
        if not check['pass']:
            print(f"        {json.dumps(check['result'], ensure_ascii=False)[:400]}")
    print('证据目录：', out)
    if not evidence['verdict']['all_pass']:
        print('结果：取证判据未全部通过，拒绝登记为官方底稿。')
        return 2

    if args.stage:
        from common import copy_checked
        run = STATE / 'runs' / f"{evidence['client_version']}_{utc_stamp()}"
        run.mkdir(parents=True)
        save_json(run / 'snapshot.json', evidence['snapshot'])
        save_json(run / 'base-capture.json', {'verdict': evidence['verdict'],
                                              'probe': str(out / 'probe.json'),
                                              'review': str(out / 'independent-review.json')})
        game = Path(evidence['snapshot']['game'])
        for layer in OPAQUE:
            record = evidence['snapshot']['files'][layer]
            target = run / 'official' / record['relative']
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_checked(game / record['relative'], target,
                         {'sha256': record['sha256'], 'bytes': record['bytes']})
        for relative, expected in evidence['snapshot']['manifests'].items():
            target = run / 'official' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_checked(game / relative, target, expected)
        save_json(run / 'capture.json', {'run': str(run)})
        print('底稿 run 已建立：', run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
