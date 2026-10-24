"""Scoped AI-authored, independently reviewed resolution of pending translations.

This is a separate provenance channel from human attestations. It never claims
human approval, client acceptance, or authority to install a game resource.
"""
from pathlib import Path
import json
from common import (ROOT, database, fingerprint, read_json, require, safe_format,
                    has_chinese, save_json)

# Historical AI-only pack, retained solely for reproducible old evidence/tests.
# New resolutions must use V2 with per-slot source checks.
LEGACY_V1_SHA256 = {'591e1400442e8b9ace12112013564699feb2c450a5c28167f37ef740f1783ab6'}


def official_contexts(official):
    from provenance import contexts
    current = contexts(official)
    if 'resources' in official:
        import UnityPy
        from unity_layers import localization, objects, row_map
        ui = row_map(json.loads(localization(objects(UnityPy.load(str(official['resources'])))).read().m_Script))
        for key, langs in ui.items():
            current['ui', key] = {'english': langs['en-US'], 'field_use': ['LocLibraryData.en-US'],
                                 'identity': {'ui_key': key}}
    return current


def import_resolutions(c, official, version, directory=None):
    """Revalidate exact English, uses and card identity before every reuse.

    Empty usage is permitted ONLY for individually reviewed localization slots
    without a discovered table reference. It does not establish runtime use.
    Changed/missing rows remain visible blockers on next maintenance run.
    """
    from registry import source, add, bind_contexts
    from provenance import append
    directory = directory or ROOT / 'reviewed-resolutions'
    packs = sorted(directory.glob('*/pack.json'))
    if not packs:
        return 0
    current = official_contexts(official)
    bind_contexts(c, current)
    count = 0
    for path in packs:
        review_path = path.with_name('review.json')
        review = read_json(review_path)
        require(review.get('review_kind') == 'independent-AI-resolution-review' and
                review.get('status') == 'PASS' and not review.get('blocking_findings') and
                review.get('pack_fingerprint') == fingerprint(path), 'Resolution pack lacks hash-bound independent review')
        pack = read_json(path)
        require(pack.get('schema') in ('MTGA_REVIEWED_RESOLUTION_V1', 'MTGA_SOURCE_CHECKED_RESOLUTION_V2') and
                pack.get('human_acceptance') is False and pack.get('runtime_acceptance') is False,
                'Resolution must not fabricate human/client acceptance')
        if pack['schema'] == 'MTGA_REVIEWED_RESOLUTION_V1':
            require(fingerprint(path)['sha256'] in LEGACY_V1_SHA256,
                    'New AI resolutions require per-slot source-checked V2 evidence')
        if pack['schema'] == 'MTGA_SOURCE_CHECKED_RESOLUTION_V2':
            from source_check import validate_pack
            validate_pack(pack, base_dir=path.parent)
            require(review.get('source_checks_reviewed') is True,
                    'Source-checked resolution needs independent source/adaptation review')
        require(review.get('format_pairs_fingerprint') == fingerprint(ROOT/'source-format-reviewed-pairs.json'),
                'Reviewed source format pairs changed')
        origin = 'independent-resolution-' + fingerprint(path)['sha256'][:20]
        source(c, origin + '-review', review_path, pack['source_version'],
               {'kind': 'independent-AI-resolution-review-record', 'human_acceptance': False})
        source(c, origin, path, pack['source_version'],
               {'kind': 'independent-AI-resolution-review', 'review': str(review_path),
                'review_fingerprint': fingerprint(review_path), 'human_acceptance': False, 'runtime_acceptance': False})
        if pack['schema'] == 'MTGA_SOURCE_CHECKED_RESOLUTION_V2':
            snapshots = {s['snapshot']['path'] for row in pack['records']
                         for s in row['source_check']['sources']}
            for relative in sorted(snapshots):
                snapshot_path = path.parent / relative
                source(c, origin + '-source-' + fingerprint(snapshot_path)['sha256'][:20],
                       snapshot_path, pack['source_version'],
                       {'kind': 'source-check-snapshot', 'official_status': 'See per-slot source_kind; a cached source is not blanket official approval'})
        seen = set()
        for row in pack['records']:
            layer, key = row['layer'], str(row['key'])
            require((layer, key) not in seen, 'Duplicate resolution key')
            seen.add((layer, key))
            ctx = current.get((layer, key))
            if ctx != {'english': row['english'], 'field_use': row['field_use'], 'identity': row['identity']}:
                append(c, 'provenance_reviews', {'review_kind': 'automatic-revalidation',
                       'status': 'needs-review', 'reason': 'reviewed-resolution-context-changed',
                       'source_id': origin, 'layer': layer, 'key': key, 'current_context': ctx,
                       'reviewed_context': row}, translation_id=origin)
                continue
            require(not safe_format(row['english'], row['chinese']), 'Unsafe resolution format')
            intentional = row.get('disposition') in ('empty-source-preserved', 'technical-identifier-preserved')
            require((intentional and row['chinese'] == row['english']) or has_chinese(row['chinese']),
                    'Untranslated resolution must explicitly name a technical/empty exception')
            require(row['field_use'] or row.get('disposition') == 'reviewed-unreferenced-localization',
                    'Unknown use requires explicit localization-only scope')
            add(c, layer, key, row['english'], row['chinese'], origin,
                'keep-english' if intentional else 'verified', 275, row['field_use'], version)
            count += 1
    c.commit()
    return count
