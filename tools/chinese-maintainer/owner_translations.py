"""Owner-authorized text inputs; no independent/human/runtime approval implied."""
import json
from pathlib import Path

from common import ROOT, fingerprint, has_chinese, read_json, require, safe_format


def paths():
    return sorted((ROOT.parents[1] / 'maintenance-data').glob('*/owner-translations-*.json'))


def import_translations(registry, contexts, version):
    from registry import add, bind_contexts, source
    bind_contexts(registry, contexts)
    count = 0
    for path in paths():
        pack = read_json(path)
        require(pack.get('schema') == 'MTGA_CN_OWNER_TRANSLATIONS_V1' and
                pack.get('human_acceptance') is False and pack.get('runtime_acceptance') is False and
                bool(pack.get('owner_authorization')), 'Invalid owner translation input')
        origin = 'owner-translation-' + fingerprint(path)['sha256'][:24]
        source(registry, origin, path, pack['source_version'], {
            'kind': 'owner-authorized-community-or-AI-translation',
            'independent_review': 'NOT_REQUIRED_BY_OWNER_POLICY',
            'human_acceptance': False, 'runtime_acceptance': False})
        seen = set()
        for row in pack['records']:
            key = row['layer'], str(row['key'])
            require(key not in seen, 'Duplicate owner translation slot')
            seen.add(key)
            expected = {name: row[name] for name in ('english', 'field_use', 'identity')}
            if contexts.get(key) != expected:
                continue  # Never lend an old translation to changed identity/use/English.
            require(key[0] == 'card' and row['field_use'] and row.get('sources') and
                    has_chinese(row['chinese']) and
                    not safe_format(row['english'], row['chinese']),
                    'Owner translation lacks source/use or has unsafe format: ' + key[1])
            add(registry, key[0], key[1], row['english'], row['chinese'],
                origin, 'verified', 350, row['field_use'], version)
            count += 1
    registry.commit()
    return count
