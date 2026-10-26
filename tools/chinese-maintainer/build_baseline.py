"""Generate a private baseline plus exact official-context bindings.

The output directory is a private checkout. No source database is redistributed.
Import mode preserves historical JSONL bytes; extraction mode reads jaJP Chinese
from explicitly supplied community databases. Neither mode implies approval.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

from baseline import SCHEMA, context_hash
from common import database, fingerprint, require, save_json
from provenance import contexts
from workload import official_for_run


def generate(root, version, run, client_jsonl=None, cards_jsonl=None,
             community_client=None, community_card=None):
    import re
    run = Path(run).expanduser().resolve()
    client_jsonl, cards_jsonl, community_client, community_card = [
        Path(path).expanduser().resolve() if path is not None else None
        for path in (client_jsonl, cards_jsonl, community_client, community_card)]
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', version)), 'Invalid version')
    snapshot, official = official_for_run(run)
    require(snapshot['client_version'] == version, 'Baseline and official snapshot versions differ')
    imported = client_jsonl is not None and cards_jsonl is not None
    extracted = community_client is not None and community_card is not None
    require(imported != extracted, 'Choose one complete input pair')
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / 'baseline'
    require(not destination.exists(), 'Use a fresh output: baseline directory already exists')
    input_paths = {'client': client_jsonl if imported else community_client,
                   'card': cards_jsonl if imported else community_card}
    origins = {layer: fingerprint(path) for layer, path in input_paths.items()}
    current = contexts(official)
    donor_contexts = (contexts({'card': community_card, 'client': community_client})
                      if extracted else None)
    with tempfile.TemporaryDirectory(prefix='.baseline-', dir=root) as temporary:
        staging_root = Path(temporary)
        temp = staging_root / 'baseline'
        temp.mkdir()
        files = {}
        for layer, path in input_paths.items():
            output = temp / f'zhcn-{layer}-{version}.jsonl'
            if imported:
                shutil.copyfile(path, output)
                require(fingerprint(output) == origins[layer], 'Imported baseline copy changed')
            else:
                with database(Path(path)) as db:
                    query = ('SELECT Key,jaJP FROM Loc' if layer == 'client' else
                             'SELECT LocId,Loc FROM Localizations_jaJP WHERE Formatted=1')
                    if layer == 'card':
                        english = dict(db.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1'))
                    else:
                        # The English must describe the community translation's
                        # own source version. Substituting current official English
                        # here would falsely bless an outdated translation.
                        english = dict(db.execute('SELECT Key,enUS FROM Loc'))
                    rows = [({'key' if layer == 'client' else 'locid': key,
                              'en': english.get(key) or '', 'zh': text})
                            for key, text in db.execute(query) if text and text.strip()]
                with output.open('x', encoding='utf-8') as handle:
                    for row in rows:
                        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
            files[layer] = {'name': output.name, 'records': len(rows), 'fingerprint': fingerprint(output)}
        bindings = temp / f'bindings-{version}.jsonl'
        count = 0
        with bindings.open('x', encoding='utf-8') as handle:
            for layer in ('client', 'card'):
                path = temp / files[layer]['name']
                for line in path.read_text(encoding='utf-8').splitlines():
                    row = json.loads(line)
                    key = str(row['key'] if layer == 'client' else row['locid'])
                    context = current.get((layer, key))
                    donor = donor_contexts.get((layer, key)) if donor_contexts is not None else None
                    # Never manufacture a binding for mismatched source English.
                    if (context and row['en'] == context['english'] and
                            (donor_contexts is None or
                             (donor is not None and context_hash(donor) == context_hash(context)))):
                        handle.write(json.dumps({'layer': layer, 'key': key,
                                                 'context_sha256': context_hash(context)}) + '\n')
                        count += 1
        files['bindings'] = {'name': bindings.name, 'records': count, 'fingerprint': fingerprint(bindings)}
        manifest = {'schema': SCHEMA, 'version': version, 'source_kind': 'community-localization',
                    'binding_basis': ('imported-text-current-official-context' if imported else
                                      'donor-and-official-context-match'),
                    'human_acceptance': False, 'runtime_acceptance': False,
                    'files': files, 'origin_fingerprints': origins,
                    'official_raw_fingerprints': {k: fingerprint(v) for k, v in official.items()},
                    'official_snapshot_fingerprint': fingerprint(Path(run) / 'snapshot.json')}
        save_json(temp / f'manifest-{version}.json', manifest)
        from baseline import Baseline
        Baseline(staging_root, version)
        require(official_for_run(run) == (snapshot, official), 'Official inputs changed during generation')
        for layer, path in input_paths.items():
            require(fingerprint(Path(path)) == origins[layer], 'Community inputs changed during generation')
        shutil.move(str(temp), str(destination))
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--version', required=True)
    p.add_argument('--official-run', required=True, type=Path)
    p.add_argument('--client-jsonl', type=Path)
    p.add_argument('--cards-jsonl', type=Path)
    p.add_argument('--community-client', type=Path)
    p.add_argument('--community-card', type=Path)
    a = p.parse_args()
    print(json.dumps(generate(a.output, a.version, a.official_run, a.client_jsonl, a.cards_jsonl,
                              a.community_client, a.community_card), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
