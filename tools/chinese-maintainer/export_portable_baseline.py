"""Export text/context from a signed release into a fresh private Git dataset.

The signed outputs establish exactly which bytes shipped, not the authority of
their Chinese wording. No source database, local evidence path, account data,
runtime approval or private key is included in the exported dataset.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from baseline import context_hash
from common import database, fingerprint, has_chinese, read_json, require, regular
from provenance import contexts


SCHEMA = 'MTGA_CN_PORTABLE_BASELINE_V1'
IDENTITY_FIELDS = ('GrpId', 'TitleId', 'ExpansionCode', 'CollectorNumber', 'IsToken',
                   'IsPrimaryCard', 'IsDigitalOnly', 'IsRebalanced', 'LinkedFaceType',
                   'LinkedFaceGrpIds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def export(destination, release_file, official_card, official_client, outputs,
           client, public_key_file, public_data=None):
    destination = Path(destination).expanduser().resolve()
    require(not destination.exists(), 'Portable export destination must be new')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'subscriber'))
    from github_publish import signed
    release_file = regular(Path(release_file).expanduser().resolve())
    public_key_file = regular(Path(public_key_file).expanduser().resolve())
    client = regular(Path(client).expanduser().resolve())
    public_key = public_key_file.read_text().strip()
    release_fp = fingerprint(release_file)
    release = signed(release_file, client, public_key_file)
    require(release['schema'] == 'MTGA_CN_RELEASE_V1' and
            release['acceptance']['status'] == 'accepted', 'Expected signed accepted source release')
    resources = {row['layer']: row for row in release['resources']}
    require(len(release['resources']) == 5 and len(resources) == 5 and
            set(resources) == {'card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle'},
            'Incomplete source release resources')
    official = {'card': regular(Path(official_card).expanduser().resolve()),
                'client': regular(Path(official_client).expanduser().resolve())}
    output_files = {key: regular(Path(value).expanduser().resolve()) for key, value in outputs.items()}
    require(set(output_files) == set(resources), 'All five signed output files are required')
    inputs = {str(release_file): release_fp, str(client): fingerprint(client),
              str(public_key_file): fingerprint(public_key_file)}
    for layer, path in output_files.items():
        require(fingerprint(path) == resources[layer]['output'], 'Signed output mismatch: ' + layer)
        inputs[str(path)] = fingerprint(path)
    for layer, path in official.items():
        require(fingerprint(path) == resources[layer]['input'], 'Signed original input mismatch: ' + layer)
        inputs[str(path)] = fingerprint(path)
    current = contexts(official)
    donor = contexts({'card': output_files['card'], 'client': output_files['client']})
    require(current.keys() == donor.keys(), 'Signed source output changed text key set')
    for key, context in current.items():
        require(context['field_use'] == donor[key]['field_use'] and
                context['identity'] == donor[key]['identity'], 'Signed source changed identity/use')
    translations = {}
    with database(output_files['card']) as db:
        translations['card'] = {str(key): value or '' for key, value in db.execute(
            'SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1')}
    with database(output_files['client']) as db:
        translations['client'] = {str(key): value or '' for key, value in db.execute('SELECT Key,enUS FROM Loc')}
    # UI source English is bound to the signed-input official client oracle.
    # The absent old original Unity file is not reconstructed or claimed found.
    from capture_base import ui_text
    translations['ui'] = ui_text(output_files['resources'])
    oracle = {key: context['english'] for (layer, key), context in current.items() if layer == 'client'}
    require(all(key in oracle for key in translations['ui']), 'UI English absent from official client oracle')
    for key in translations['ui']:
        current['ui', key] = {'english': oracle[key], 'field_use': ['LocLibraryData.en-US'],
                              'identity': {'ui_key': key}}
    prior = {}
    source_metadata = None
    if public_data:
        public_data = Path(public_data).expanduser().resolve()
        index = read_json(public_data / 'manifest.json')
        for name in ('translations.jsonl', 'sources.json'):
            require(fingerprint(public_data / name) == index['files'][name],
                    'Public source projection changed: ' + name)
        # Preserve only already public evidence, and only if it exactly matches
        # this exported slot's English, Chinese, identity and field use.
        for line in (public_data / 'translations.jsonl').read_text().splitlines():
            row = json.loads(line)
            prior[(row['layer'], row['key'])] = row
        source_metadata = public_data / 'sources.json'
        for name in ('manifest.json', 'translations.jsonl', 'sources.json'):
            inputs[str(public_data / name)] = fingerprint(public_data / name)
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for layer in ('card', 'client', 'ui'):
        name = layer + '.jsonl'
        count = 0
        with (destination / name).open('x', encoding='utf-8') as stream:
            for (kind, key), context in sorted(current.items()):
                if kind != layer:
                    continue
                chinese = translations[layer][key] or ''
                disposition = ('translated' if has_chinese(chinese) else
                               ('empty-source-preserved' if not context['english'] and not chinese
                                else 'keep-english'))
                resource_layer = 'resources' if layer == 'ui' else layer
                row = {'layer': layer, 'key': key, **context, 'chinese': chinese,
                       'english_sha256': text_hash(context['english']), 'chinese_sha256': text_hash(chinese),
                       'context_sha256': context_hash(context), 'disposition': disposition,
                       'source': {'kind': 'community-localization', 'release_id': release['id'],
                                  'resource_layer': resource_layer,
                                  'output_sha256': resources[resource_layer]['output']['sha256']},
                       'human_acceptance': False, 'runtime_acceptance': False}
                old = prior.get((layer, key))
                if old and all(old.get(k) == row[k] for k in
                               ('english', 'chinese', 'identity', 'field_use')):
                    row['prior_source_record'] = old
                stream.write(canonical(row) + '\n')
                count += 1
        files[layer] = {'name': name, 'records': count,
                        'fingerprint': fingerprint(destination / name)}
    titles = 'card-titles.jsonl'
    count = 0
    with database(official['card']) as db, (destination / titles).open('x', encoding='utf-8') as stream:
        db.row_factory = __import__('sqlite3').Row
        from collections import defaultdict
        from raw import canonical as canonical_title
        variants = defaultdict(dict)
        for key, formatted, text in db.execute('SELECT LocId,Formatted,Loc FROM Localizations_enUS'):
            variants[key][formatted] = text
        for card in db.execute('SELECT * FROM Cards WHERE IsPrimaryCard=1 AND IsToken=0 ORDER BY GrpId'):
            identity = {key: card[key] for key in IDENTITY_FIELDS}
            title_id = card['TitleId']
            english, _ = canonical_title(variants[title_id])
            require(english and not has_chinese(english), 'Missing trustworthy plain source title')
            row = {'grp_id': card['GrpId'], 'title_id': title_id, 'english': english,
                   'chinese': translations['card'].get(str(title_id), ''), 'identity': identity}
            stream.write(canonical(row) + '\n')
            count += 1
    files['titles'] = {'name': titles, 'records': count, 'fingerprint': fingerprint(destination / titles)}
    shutil.copyfile(release_file, destination / 'source-release.json')
    require(fingerprint(destination / 'source-release.json') == release_fp, 'Source envelope copy drift')
    if source_metadata:
        shutil.copyfile(source_metadata, destination / 'prior-sources.json')
        files['prior_sources'] = {'name': 'prior-sources.json',
                                  'fingerprint': fingerprint(destination / 'prior-sources.json')}
    manifest = {'schema': SCHEMA, 'version': release['client_version'],
                'source_release_id': release['id'], 'steam_build': release['steam_build'],
                'source_kind': 'community-localization', 'public_key': public_key,
                'source_manifest': {'name': 'source-release.json', 'fingerprint': release_fp},
                'files': files,
                'source_output_fingerprints': {key: value['output'] for key, value in resources.items()},
                'source_official_raw_fingerprints': {key: resources[key]['input'] for key in official},
                'human_acceptance': False, 'runtime_acceptance': False,
                'contains_ai_authored_or_unknown': True,
                'semantic_review_performed': False,
                'binding_basis': 'signed-shipped-output-with-exact-official-raw-context',
                'ui_binding_basis': 'signed-ui-Chinese-with-signed-input-client-English-oracle',
                'provenance_note': 'Exact previously shipped text; mixed community, historical and AI wording. '
                                   'The signature authenticates shipped bytes, not authoritative Chinese translation. '
                                   'Existing public source details are retained only for exact matching slots; '
                                   'all new adoption remains context/format checked community baseline.'}
    for path, expected in inputs.items():
        require(fingerprint(Path(path)) == expected, 'Input drifted during portable export')
    require(signed(release_file, client, public_key_file) == release, 'Source signature drifted')
    # The completion marker appears only after all source inputs are rechecked.
    (destination / 'manifest.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'release', 'official-card', 'official-client', 'client', 'public-key-file'):
        parser.add_argument('--' + name, required=True, type=Path)
    for name in ('card', 'client', 'resources', 'bootstrap-fonts', 'font-bundle'):
        parser.add_argument('--signed-' + name, required=True, type=Path)
    parser.add_argument('--public-data', type=Path)
    args = parser.parse_args()
    result = export(args.output, args.release, args.official_card, args.official_client,
                    {name: getattr(args, 'signed_' + name) for name in
                     ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')},
                    args.client, args.public_key_file, args.public_data)
    print(canonical(result))


if __name__ == '__main__':
    main()
