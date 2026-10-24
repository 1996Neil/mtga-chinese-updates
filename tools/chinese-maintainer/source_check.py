"""Export and validate per-slot source checks; never fetch or install resources.

Website lookup instructions live in SOURCE_LOOKUP.md. A concept match is not a
full-text translation. This validation checks evidence integrity and coverage,
not whether a quotation actually proves the translator's semantic judgment.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

LEVELS = {'exact-full-text', 'source-backed-concepts', 'source-unavailable',
          'intentional-preserved'}
KINDS = {'official-print', 'community-oracle', 'official-mechanics',
         'community-rules', 'community-localization', 'official-ui',
         'official-announcement', 'local-existing'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def excerpt_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _https(value):
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    return url.scheme == 'https' and url.hostname and not url.username and not url.password


def _utc(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0
    except (AttributeError, ValueError, TypeError):
        return False


def _capture_time(record):
    if record.get('retrieved_at') is not None:
        return _utc(record['retrieved_at'])
    return (record.get('timestamp_basis') in ('snapshot-write-time', 'audit-record-write-time')
            and _utc(record.get('recorded_at'))
            and isinstance(record.get('capture_time_basis'), str)
            and bool(record['capture_time_basis'].strip()))


def validate_record(row, *, base_dir=None, snapshot_cache=None):
    """Validate a source_check embedded in a hash-reviewed resolution row."""
    check = row.get('source_check')
    require(isinstance(check, dict), 'Every source-checked slot needs source_check')
    level = check.get('verification_level')
    require(level in LEVELS, 'Unknown source verification level')
    require(isinstance(check.get('audit_note'), str) and check['audit_note'].strip(),
            'Source check must describe scope, adaptation, or remaining gap')
    sources = check.get('sources')
    require(isinstance(sources, list), 'Source list required')
    for field in ('english_alignment', 'adaptation_note'):
        require(isinstance(check.get(field), str) and check[field].strip(), f'{field} required')
    if level in {'exact-full-text', 'source-backed-concepts'}:
        require(sources, 'A claimed source match needs evidence')
        usable = [s for s in sources if isinstance(s, dict) and s.get('adopted') is not False
                  and not re.search(r'gpt|claude|gemini|\bai\b',
                                    str(s.get('source_declared', '')).strip().lower())]
        require(usable, 'An unadopted or AI-generated website translation cannot establish a reliable source match')
    if level == 'exact-full-text':
        require(check.get('ai_remaining') == [], 'Full-text verification cannot hide remaining AI prose')
    if level == 'source-unavailable':
        attempts = check.get('lookup_attempts')
        require(isinstance(attempts, list) and attempts, 'Missing source needs recorded lookup attempts')
        for attempt in attempts:
            require(isinstance(attempt, dict) and _https(attempt.get('url', '')) and
                    all(isinstance(attempt.get(k), str) and attempt[k].strip() for k in ('query', 'outcome'))
                    and _capture_time(attempt),
                    'Lookup attempts need URL, query, observed outcome and UTC retrieval time')
    if level in {'source-backed-concepts', 'source-unavailable'}:
        segments = check.get('ai_remaining')
        require(isinstance(segments, list) and segments and
                all(isinstance(s, str) and s.strip() for s in segments), 'Disclose remaining AI expression')
    if level == 'intentional-preserved':
        require(row.get('disposition') in {'empty-source-preserved', 'technical-identifier-preserved'}
                and row['english'] == row['chinese'], 'Preservation is only for explicit empty/technical slots')
        if row['disposition'] == 'empty-source-preserved':
            require(row['english'] == '', 'An empty-source exception must be empty')
        else:
            require(isinstance(row['english'], str) and
                    re.fullmatch(r'[A-Za-z0-9_.:/-]+', row['english']) is not None,
                    'A technical exception must be a plain identifier, not prose')
    for source in sources:
        require(isinstance(source, dict), 'Source must be an object')
        require(_https(source.get('url', '')),
                'Sources need a public HTTPS URL without credentials')
        require(source.get('source_kind') in KINDS, 'Unknown source kind')
        require(isinstance(source.get('language'), str) and source['language'].strip(), 'Source language required')
        excerpt = source.get('excerpt')
        require(isinstance(excerpt, str) and excerpt.strip(), 'Source quotation required')
        require(source.get('excerpt_sha256') == excerpt_hash(excerpt), 'Source quotation hash mismatch')
        require(isinstance(source.get('match_note'), str) and source['match_note'].strip(),
                'Source-to-slot matching/adaptation note required')
        require(_capture_time(source), 'Source needs UTC retrieval or explicitly qualified recording time')
        snapshot = source.get('snapshot')
        require(isinstance(snapshot, dict) and base_dir is not None, 'Source snapshot and pack directory required')
        relative = Path(snapshot.get('path', ''))
        require(not relative.is_absolute() and relative.parts and '..' not in relative.parts,
                'Snapshot must be a relative path inside the pack')
        base = Path(base_dir).resolve()
        path = base / relative
        require(path.resolve() == path and path.is_relative_to(base) and path.is_file(),
                'Snapshot missing or uses a symbolic link')
        cache = snapshot_cache if snapshot_cache is not None else {}
        if path not in cache:
            data = path.read_bytes()
            cache[path] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        require(cache[path] == {k: snapshot.get(k) for k in ('sha256', 'bytes')},
                'Raw source snapshot fingerprint mismatch')
    return level


def validate_pack(pack, *, base_dir=None):
    require(pack.get('schema') == 'MTGA_SOURCE_CHECKED_RESOLUTION_V2', 'Expected source-checked V2 pack')
    counts, seen, snapshots = Counter(), set(), {}
    for row in pack['records']:
        key = row['layer'], str(row['key'])
        require(key not in seen, f'Duplicate localization slot: {key}')
        seen.add(key)
        counts[validate_record(row, base_dir=base_dir, snapshot_cache=snapshots)] += 1
    return {'records': len(seen), 'verification_levels': dict(counts),
            'semantic_source_review': 'Required separately; evidence shape does not prove accuracy',
            'runtime_acceptance': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export', help='Export a complete source lookup queue from a resolution pack')
    export.add_argument('pack', type=Path)
    export.add_argument('destination', type=Path)
    validate = sub.add_parser('validate', help='Check source coverage and quotation integrity')
    validate.add_argument('pack', type=Path)
    args = parser.parse_args()
    pack = json.loads(args.pack.read_text(encoding='utf-8'))
    if args.command == 'validate':
        print(json.dumps(validate_pack(pack, base_dir=args.pack.resolve().parent), ensure_ascii=False, indent=2))
    else:
        require(not args.destination.exists(), 'Export destination already exists')
        rows = [{k: row[k] for k in ('layer', 'key', 'english', 'chinese', 'field_use', 'identity', 'disposition')}
                for row in pack['records']]
        output = {'source_pack_sha256': hashlib.sha256(args.pack.read_bytes()).hexdigest(),
                  'instructions': str(Path(__file__).with_name('SOURCE_LOOKUP.md')),
                  'records': rows}
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        args.destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'Exported {len(rows)} source-check slots')


if __name__ == '__main__':
    main()
