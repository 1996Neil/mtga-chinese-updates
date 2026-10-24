"""Restore configured English event names in an isolated client candidate."""
import argparse
import json
from pathlib import Path
import sqlite3
from common import database, fingerprint, require
from raw import validate_db
from registry import is_english_event_name, english_event_text


def build(official, baseline, output):
    official, baseline, output = [Path(p).resolve() for p in (official, baseline, output)]
    require(not output.exists(), 'Use a fresh isolated candidate directory')
    with database(official) as source, database(baseline) as previous:
        en = dict(source.execute('SELECT Key,enUS FROM Loc'))
        before = dict(previous.execute('SELECT Key,enUS FROM Loc'))
    require(en.keys() == before.keys(), 'Official and candidate client keys differ')
    expected_names = {k: english_event_text(v, en.get) for k, v in en.items() if is_english_event_name(k)}
    updates = {k: v for k, v in expected_names.items() if v != before[k]}
    inputs = {'official': fingerprint(official), 'baseline': fingerprint(baseline)}
    output.mkdir(parents=True)
    candidate = output / 'client-localized.mtga'
    with database(baseline) as source, sqlite3.connect(candidate) as target:
        source.backup(target)
        target.executemany('UPDATE Loc SET enUS=? WHERE Key=?', [(v, k) for k, v in updates.items()])
        target.commit()
    # References may embed sprites/formatting. Validate against the resolved
    # official English in those exact name slots, then compare every actual row.
    from common import safe_format
    for key, value in updates.items():
        require(not safe_format(expected_names[key], value), 'Unsafe resolved event name')
    import tempfile
    with tempfile.TemporaryDirectory() as temporary:
        aligned = Path(temporary).resolve() / 'aligned.sqlite'
        with database(baseline) as a, sqlite3.connect(aligned) as b:
            a.backup(b)
            b.executemany('UPDATE Loc SET enUS=? WHERE Key=?', [(v, k) for k, v in updates.items()])
            b.commit()
        validation = validate_db(aligned, candidate, 'client', {})
        validation['scope'] = 'Exact all-table comparison against baseline plus explicitly restored name fields'
    with database(candidate) as target:
        actual = dict(target.execute('SELECT Key,enUS FROM Loc'))
    require(all(actual[k] == v for k, v in expected_names.items()), 'Event name not English')
    require(all(actual[k] == before[k] for k in en if not is_english_event_name(k)),
            'Introduction or other non-name text changed')
    require(inputs == {'official': fingerprint(official), 'baseline': fingerprint(baseline)},
            'Input changed while restoring names')
    report = {
        'status': 'PASS', 'inputs': inputs, 'candidate': fingerprint(candidate),
        'name_fields_checked': sum(is_english_event_name(k) for k in en),
        'restored_name_count': len(updates), 'all_other_text_unchanged': True,
        'official_reference_names_resolved': sum(bool(__import__('re').search(r'\$[^$]+\$', en[k]))
                                                 for k in expected_names),
        'restored_names': [{'key': k, 'before': before[k], 'after': v} for k, v in sorted(updates.items())],
        'validation': validation, 'runtime_acceptance': False, 'installed': False,
    }
    (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.official, args.baseline, args.output)
    print(json.dumps({k: result[k] for k in ('status', 'name_fields_checked', 'restored_name_count',
                                          'all_other_text_unchanged', 'runtime_acceptance')}, ensure_ascii=False))
