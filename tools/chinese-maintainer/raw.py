"""New-official-base Raw localization and the historical F1 + F0 rule."""
import json
import re
import sqlite3
from collections import Counter, defaultdict
from common import (database, fingerprint, require, quoted, save_json, safe_format,
                    has_chinese, HISTORY, ROOT)
from registry import roles, decide

SCHEMA_QUERY = 'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
TITLE_QUERY = 'SELECT DISTINCT TitleId FROM Cards WHERE TitleId>0 AND IsToken=0 AND IsPrimaryCard=1'


def validate_db(baseline, candidate, layer, updates, additions=()):
    """Independent exact expected-row comparison including every other table."""
    with database(baseline) as a, database(candidate) as b:
        require(b.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'SQLite integrity failure')
        require(a.execute(SCHEMA_QUERY).fetchall() == b.execute(SCHEMA_QUERY).fetchall(), 'Schema/index drift')
        tables = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        checks = {}
        for table in tables:
            if layer == 'card' and table == 'Localizations_enUS':
                expected = {(i, f): v for i, f, v in a.execute('SELECT * FROM Localizations_enUS')}
                for key, value in updates.items():
                    require((int(key), 1) in expected, 'Localization update cannot invent keys')
                    require(not safe_format(expected[(int(key), 1)], value), f'Unsafe text: {key}')
                    expected[(int(key), 1)] = value
                for key, value in additions:
                    require((key, 0) not in expected, 'Name variant overwrites existing F0')
                    expected[(key, 0)] = value
                actual = {(i, f): v for i, f, v in b.execute('SELECT * FROM Localizations_enUS')}
                require(expected == actual, 'Unexpected localization/variant row change')
            elif layer == 'client' and table == 'Loc':
                cols = [r[1] for r in a.execute('PRAGMA table_info(Loc)')]
                ki, ei = cols.index('Key'), cols.index('enUS')
                expected = {r[ki]: list(r) for r in a.execute('SELECT * FROM Loc')}
                for key, value in updates.items():
                    require(key in expected, 'Localization update cannot invent keys')
                    require(not safe_format(expected[key][ei], value), f'Unsafe text: {key}')
                    expected[key][ei] = value
                actual = {r[ki]: list(r) for r in b.execute('SELECT * FROM Loc')}
                require(expected == actual, 'Unrelated client fields/languages changed')
            else:
                info = a.execute(f'PRAGMA table_info({quoted(table)})').fetchall()
                keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
                require(keys, f'No deterministic table key: {table}')
                query = f'SELECT * FROM {quoted(table)} ORDER BY ' + ','.join(map(quoted, keys))
                ia, ib = iter(a.execute(query)), iter(b.execute(query))
                from itertools import zip_longest
                require(all(x == y for x, y in zip_longest(ia, ib, fillvalue=object())), f'Non-target table changed: {table}')
            checks[table] = True
        return {'status': 'PASS', 'integrity': 'ok', 'schema_and_indexes_preserved': True,
                'all_tables_expected_rows': checks, 'changed_F1_or_enUS': len(updates), 'added_F0': len(additions)}


def build_localization(source, output, layer, registry, run):
    require(not output.exists(), 'Use a new run directory')
    updates, decisions = {}, []
    with database(source) as original:
        use = roles(original) if layer == 'card' else {}
        values = (original.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1') if layer == 'card'
                  else original.execute('SELECT Key,enUS FROM Loc'))
        for key, english in values:
            translation, record = decide(registry, run, layer, key, english,
                                         use.get(str(key), []) if layer == 'card' else ['Loc.enUS'])
            decisions.append(record)
            if translation != english:
                updates[str(key)] = translation
        with sqlite3.connect(output) as dst:
            original.backup(dst)
            if layer == 'card':
                dst.executemany('UPDATE Localizations_enUS SET Loc=? WHERE LocId=? AND Formatted=1',
                                [(v, int(k)) for k, v in updates.items()])
            else:
                dst.executemany('UPDATE Loc SET enUS=? WHERE Key=?', [(v, k) for k, v in updates.items()])
    validation = validate_db(source, output, layer, updates)
    save_json(output.parent / (layer + '-decisions.json'), decisions)
    stats = dict(Counter(r['status'] for r in decisions))
    stats.update({'applied': len(updates), 'conflicts': sum(bool(r['conflicts']) for r in decisions),
                  'by_source': dict(Counter(r['source'] for r in decisions if r['status'] == 'verified')),
                  'reasons': dict(Counter(r['reason'] for r in decisions if r['status'] != 'verified'))})
    return {'validation': validation, 'counts': stats, 'updates': updates}


def canonical(variants):
    # Existing official F0 is authoritative (including accents). The historical
    # F2 -> F1 fallback applies only when a missing F0 must be generated.
    for fmt in (0, 2, 1):
        text = variants.get(fmt)
        if isinstance(text, str) and text.strip() and not any(char in text for char in '<>\r\n\x00'):
            return text, fmt
    return None, None


def validate_names(official, localized, candidate, additions):
    """Re-derive the variant plan from official titles, not from the build report."""
    with database(official) as o, database(localized) as z, database(candidate) as c:
        original, before, after = defaultdict(dict), defaultdict(dict), defaultdict(dict)
        for i, f, text in o.execute('SELECT * FROM Localizations_enUS'):
            original[i][f] = text
        for i, f, text in z.execute('SELECT * FROM Localizations_enUS'):
            before[i][f] = text
        for i, f, text in c.execute('SELECT * FROM Localizations_enUS'):
            after[i][f] = text
        eligible = {r[0] for r in o.execute(TITLE_QUERY)}
        derived = []
        english_titles = defaultdict(set)
        for title in sorted(eligible):
            english, _ = canonical(original[title])
            require(english and not has_chinese(english), f'No trustworthy plain official English title: {title}')
            require(1 in before[title] and after[title].get(1) == before[title][1], 'Name compatibility changed display F1')
            require(after[title].get(0) == english, f'Wrong/missing canonical F0 binding: {title}')
            english_titles[english].add(title)
            if 0 not in before[title]:
                derived.append((title, english))
        require(derived == [tuple(r) for r in additions], 'Variant plan includes unrelated/token-only/secondary-only/missing titles')
        require(all(len(ids) == 1 for ids in english_titles.values()), 'Cross-title canonical-name collision')
        return {'status': 'PASS', 'all_primary_non_token_F0_bound_to_official_titles': True,
                'all_display_F1_preserved': True, 'token_only_secondary_only_titles_not_added': True,
                'canonical_exact_cross_title_collisions': 0, 'derived_generated_F0': len(derived)}


def build_names(official, localized, output, registry, run, old_official):
    added, uncovered, conflicts, names = [], [], [], []
    with database(official) as o, database(localized) as z, database(old_official) as old:
        original = defaultdict(dict)
        current = defaultdict(dict)
        for i, f, text in o.execute('SELECT * FROM Localizations_enUS'):
            original[i][f] = text
        for i, f, text in z.execute('SELECT * FROM Localizations_enUS'):
            current[i][f] = text
        titles = {r[0] for r in o.execute(TITLE_QUERY)}
        cards = [dict(zip([col[0] for col in cur.description], row))
                 for cur in [o.execute('SELECT * FROM Cards')] for row in cur]
        old_cards = {r[0]: r[1:] for r in old.execute('SELECT GrpId,TitleId,ExpansionCode,CollectorNumber,IsToken,IsPrimaryCard,LinkedFaceType,LinkedFaceGrpIds FROM Cards')}
        old_names = defaultdict(dict)
        for i, f, text in old.execute('SELECT * FROM Localizations_enUS'):
            old_names[i][f] = text
        old_titles = {r[0] for r in old.execute(TITLE_QUERY)}
        changed_canonical = []
        new_groups, changed_identity = [], []
        for card in cards:
            identity = tuple(card[k] for k in ('TitleId', 'ExpansionCode', 'CollectorNumber', 'IsToken', 'IsPrimaryCard', 'LinkedFaceType', 'LinkedFaceGrpIds'))
            if card['GrpId'] not in old_cards:
                new_groups.append(card['GrpId'])
            elif old_cards[card['GrpId']] != identity:
                changed_identity.append(card['GrpId'])
        for title in sorted(titles):
            en, source_fmt = canonical(original[title])
            if title in old_titles and canonical(old_names[title])[0] != en:
                changed_canonical.append({'title_id': title, 'before': canonical(old_names[title])[0], 'after': en})
            if en is None or 1 not in current[title]:
                uncovered.append({'title_id': title, 'reason': 'no-official-plain-F2-F1-or-display-F1'})
                continue
            if 0 in current[title] and current[title][0] != en:
                conflicts.append({'title_id': title, 'existing_F0': current[title][0], 'official_plain': en})
                continue
            if 0 not in current[title]:
                added.append((title, en))
                current[title][0] = en
            names.append({'title_id': title, 'canonical': en, 'display': current[title][1],
                          'canonical_source_format': source_fmt})
        with sqlite3.connect(output) as dst:
            z.backup(dst)
            dst.executemany('INSERT INTO Localizations_enUS(LocId,Formatted,Loc) VALUES (?,0,?)', added)
        counts = Counter(row['canonical'] for row in names)
        collisions = {name: count for name, count in counts.items() if count > 1}
        rows_by_title = {r['title_id']: r for r in names}
        for card in cards:
            title = card['TitleId']
            r = rows_by_title.get(title)
            variants = current[title]
            english = r['canonical'] if r else canonical(original[title])[0]
            enabled = bool(card['IsPrimaryCard'] and not card['IsToken'])
            identity = {k: card[k] for k in ('GrpId', 'TitleId', 'ExpansionCode', 'CollectorNumber', 'IsToken', 'IsPrimaryCard', 'LinkedFaceType', 'LinkedFaceGrpIds')}
            status = 'static-verified-client-pending' if (not enabled or r) else 'needs-review'
            registry.execute('INSERT OR REPLACE INTO card_names VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (run, card['GrpId'], title, json.dumps(identity), english,
                              variants.get(1), json.dumps(variants, ensure_ascii=False),
                              json.dumps(sorted({s for s in variants.values() if isinstance(s, str)}), ensure_ascii=False),
                              1, 0, 1, int(enabled), 'new-official-card-identities+registry', None, status))
        save_json(output.parent / 'name-plan.json', {'names': names, 'added': added, 'uncovered': uncovered,
                                                   'conflicts': conflicts, 'new_groups': new_groups, 'changed_identity': changed_identity,
                                                   'changed_canonical_names': changed_canonical})
        validation = validate_db(localized, output, 'card', {}, added)
        validation['status'] = 'PASS' if not uncovered and not conflicts and not collisions else 'FAIL'
        if validation['status'] == 'PASS':
            validation['canonical_binding_validation'] = validate_names(official, localized, output, added)
        # Samples include actual structures, not just ordinary card names.
        structures = {}
        for kind, predicate in {
            'linked_faces': lambda c: c['IsPrimaryCard'] and c['LinkedFaceGrpIds'],
            'adventure': lambda c: c['IsPrimaryCard'] and rows_by_title.get(c['TitleId'], {}).get('canonical') == 'Hearth Elemental',
            'token': lambda c: c['IsToken'],
            'basic_land': lambda c: rows_by_title.get(c['TitleId'], {}).get('canonical') in {'Mountain', 'Island', 'Swamp', 'Forest', 'Plains'},
            'reprints': lambda c: sum(1 for x in cards if x['TitleId'] == c['TitleId']) > 1,
        }.items():
            # The reprint predicate is evaluated only until three samples are found.
            structures[kind] = []
            for card in cards:
                if predicate(card):
                    structures[kind].append({k: card[k] for k in ('GrpId', 'TitleId', 'ExpansionCode', 'CollectorNumber', 'LinkedFaceType', 'LinkedFaceGrpIds', 'IsToken', 'IsPrimaryCard')})
                    if len(structures[kind]) == 3:
                        break
        structures['by_linked_face_type'] = {}
        for card in cards:
            kind = card['LinkedFaceType']
            if not kind or not card['IsPrimaryCard'] or card['IsToken']:
                continue
            key = str(kind)
            if key not in structures['by_linked_face_type']:
                structures['by_linked_face_type'][key] = {
                    **{k: card[k] for k in ('GrpId', 'TitleId', 'ExpansionCode', 'CollectorNumber',
                                           'LinkedFaceType', 'LinkedFaceGrpIds', 'IsToken', 'IsPrimaryCard')},
                    'canonical': rows_by_title[card['TitleId']]['canonical'],
                    'display': rows_by_title[card['TitleId']]['display']}
        return {'validation': validation, 'primary_title_ids': len(titles), 'generated_F0': len(added),
                'existing_F0_preserved': len(titles) - len(added) - len(uncovered) - len(conflicts),
                'chinese_F1': sum(has_chinese(r['display']) for r in names),
                'intentional_or_pending_english_F1': sum(not has_chinese(r['display']) for r in names),
                'uncovered': uncovered, 'canonical_conflicts': conflicts, 'cross_title_collisions': collisions,
                'new_card_groups': new_groups, 'changed_card_identities': changed_identity,
                'changed_canonical_names': changed_canonical,
                'special_structures': structures, 'token_only_titles_added': 0,
                'shared_token_rows': sum(c['IsToken'] and c['TitleId'] in {i for i, _ in added} for c in cards),
                'shared_secondary_rows': sum(not c['IsPrimaryCard'] and c['TitleId'] in {i for i, _ in added} for c in cards)}


def deck_lines(text):
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == 'Deck':
            continue
        match = re.fullmatch(r'(\d+) (.+?)(?: \(([A-Za-z0-9]+)\) (\S+))?', line)
        require(match is not None, f'Unsupported fixture line: {line}')
        n, name, expansion, collector = match.groups()
        require(0 < int(n) <= 250, 'Fixture quantity out of range')
        result.append((int(n), name, expansion, collector))
    return result


def deck_quantities(pairs):
    result = Counter()
    for quantity, title in pairs:
        result[title] += quantity
    return result


def regressions(official, candidate, out, name_result):
    """Checks registry matching, explicitly not a substitute for native import."""
    out.mkdir()
    original = (HISTORY / 'original-60.txt').read_text()
    lines = deck_lines(original)
    require(sum(r[0] for r in lines) == 60, 'Historical deck no longer 60 cards')
    with database(official) as o, database(candidate) as c:
        cards = list(o.execute('SELECT GrpId,TitleId,ExpansionCode,CollectorNumber,IsPrimaryCard,IsToken FROM Cards'))
        en = defaultdict(dict)
        zh = defaultdict(dict)
        for i, f, s in o.execute('SELECT * FROM Localizations_enUS'):
            en[i][f] = s
        for i, f, s in c.execute('SELECT * FROM Localizations_enUS'):
            zh[i][f] = s
        eligible = {r[1] for r in cards if r[4] and not r[5]}
        english_map = defaultdict(set)
        chinese_map = defaultdict(set)
        for title in eligible:
            name = canonical(en[title])[0]
            english_map[name].add(title)
            chinese_map[zh[title].get(1)].add(title)
        fixed = {'english-60': original, 'qualified-60': (HISTORY / 'full-qualified-60.txt').read_text()}
        chinese, mixed, expected = ['Deck'], ['Deck'], []
        for index, (quantity, name, _, _) in enumerate(lines):
            ids = english_map[name]
            require(len(ids) == 1, f'Historical fixture ambiguous/missing: {name}')
            title = next(iter(ids))
            display = zh[title].get(1)
            require(has_chinese(display), f'Historical Chinese fixture lacks Chinese F1: {name}')
            chinese.append(f'{quantity} {display}')
            mixed.append(f'{quantity} {display if index % 2 else name}')
            expected.append({'quantity': quantity, 'title_id': title, 'canonical': name, 'chinese': display})
        fixed['chinese-60'] = '\n'.join(chinese) + '\n'
        fixed['mixed-60'] = '\n'.join(mixed) + '\n'
        expected_fixtures = {kind: expected for kind in fixed}
        special = list(name_result['special_structures']['by_linked_face_type'].values())
        require(len(special) < 60, 'Too many special structures for a single 60-card regression fixture')
        copies = min(4, 59 // max(len(special), 1))
        special_lines, special_expected = ['Deck'], []
        for sample in special:
            special_lines.append(f'{copies} {sample["canonical"]}')
            special_expected.append({'quantity': copies, 'title_id': sample['TitleId'],
                                     'canonical': sample['canonical'], 'chinese': sample['display']})
        mountain = next(iter(english_map['Mountain']))
        remainder = 60 - copies * len(special)
        special_lines.append(f'{remainder} Mountain')
        special_expected.append({'quantity': remainder, 'title_id': mountain,
                                 'canonical': 'Mountain', 'chinese': zh[mountain][1]})
        fixed['special-english-60'] = '\n'.join(special_lines) + '\n'
        expected_fixtures['special-english-60'] = special_expected
        results = {}
        for kind, text in fixed.items():
            (out / (kind + '.txt')).write_text(text)
            matched = []
            errors = []
            for quantity, name, expansion, collector in deck_lines(text):
                ids = {i for i in eligible if name in {zh[i].get(0), zh[i].get(1)}}
                if expansion:
                    ids &= {r[1] for r in cards if r[2] == expansion and str(r[3]) == collector and r[4] and not r[5]}
                if len(ids) != 1:
                    errors.append({'name': name, 'title_ids': sorted(ids)})
                else:
                    matched.append((quantity, next(iter(ids))))
            expected_pairs = [(r['quantity'], r['title_id']) for r in expected_fixtures[kind]]
            results[kind] = {'static_status': 'PASS' if deck_quantities(matched) == deck_quantities(expected_pairs) and not errors else 'FAIL',
                             'native_client_status': '未验证', 'cards': sum(n for n, _ in matched), 'errors': errors}
        save_json(out / 'expected-60.json', expected)
        save_json(out / 'expected-fixtures.json', expected_fixtures)
        save_json(out / 'special-structures.json', name_result['special_structures'])
        new = [r for r in cards if r[0] in set(name_result['new_card_groups']) and r[4] and not r[5]]
        save_json(out / 'new-cards.json', [{'arena_grp_id': r[0], 'title_id': r[1], 'english': canonical(en[r[1]])[0], 'display': zh[r[1]].get(1)} for r in new])
        return {'status': 'PASS' if all(r['static_status'] == 'PASS' for r in results.values()) else 'FAIL',
                'fixtures': results, 'new_importable_cards': len(new),
                'new_card_client_status': 'N/A: no new importable cards' if not new else '未验证',
                'special_client_status': '未验证', 'scope': 'static exact-name/identity/quantity matching; native MTGA not invoked'}
