"""Translation and card-name provenance registry; SQLite is local, not a server."""
import json
import sqlite3
import re
from collections import defaultdict
from common import (STATE, OLD, BACKUPS, HISTORY, DONORS, database, require,
                    fingerprint, read_json, one, format_metadata, has_chinese, safe_format)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
 source_version TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS localization(
 layer TEXT NOT NULL, key TEXT NOT NULL, english TEXT, chinese TEXT,
 source_id TEXT NOT NULL REFERENCES sources(id), status TEXT NOT NULL,
 priority INTEGER NOT NULL, field_use TEXT NOT NULL, formatting TEXT NOT NULL,
 first_verified_version TEXT, last_verified_version TEXT,
 PRIMARY KEY(layer,key,source_id));
CREATE TABLE IF NOT EXISTS decisions(
 run_id TEXT NOT NULL, layer TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
 source_id TEXT, reason TEXT NOT NULL, english TEXT, translation TEXT, field_use TEXT,
 PRIMARY KEY(run_id,layer,key));
CREATE TABLE IF NOT EXISTS card_names(
 run_id TEXT NOT NULL, arena_grp_id INTEGER NOT NULL, title_id INTEGER NOT NULL,
 identity_json TEXT NOT NULL, english_canonical TEXT, chinese_display TEXT,
 variants_json TEXT NOT NULL, aliases_json TEXT NOT NULL,
 display_format INTEGER NOT NULL, import_format INTEGER NOT NULL,
 display_enabled INTEGER NOT NULL, deck_import_enabled INTEGER NOT NULL,
 source TEXT NOT NULL, last_verified_version TEXT, compatibility_status TEXT NOT NULL,
 PRIMARY KEY(run_id,arena_grp_id));
'''


def open_registry():
    STATE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE / 'registry.sqlite')
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript(SCHEMA)
    from provenance import migrate
    migrate(c)
    return c


def roles(c):
    result = defaultdict(set)
    fields = ('TitleId', 'AltTitleId', 'InterchangeableTitleId', 'FlavorTextId',
              'ReminderTextId', 'TypeTextId', 'SubtypeTextId')
    for field in fields:
        for (loc,) in c.execute(f'SELECT DISTINCT {field} FROM Cards WHERE {field}>0'):
            result[loc].add('Cards.' + field)
    for (loc,) in c.execute('SELECT DISTINCT TextId FROM Abilities WHERE TextId>0'):
        result[loc].add('Abilities.TextId')
    for (loc,) in c.execute('SELECT DISTINCT LocId FROM Prompts WHERE LocId>0'):
        result[loc].add('Prompts.LocId')
    for kind, loc in c.execute('SELECT DISTINCT Type,LocId FROM Enums WHERE LocId>0'):
        result[loc].add('Enums.' + kind)
    # Cards also bind per-printing text overrides as AbilityId:TextId. This is
    # the same explicit relation followed by the historical FRA generator.
    for field in ('AbilityIds', 'HiddenAbilityIds'):
        for (value,) in c.execute(f'SELECT {field} FROM Cards'):
            for item in str(value or '').split(','):
                bits = item.split(':')
                if len(bits) == 2 and bits[1].isdigit() and int(bits[1]) > 0:
                    result[int(bits[1])].add('Cards.AbilityTextId')
    # Embedded/parameterized text IDs are explicit after ':' in ability lists.
    columns = {r[1] for r in c.execute('PRAGMA table_info(Abilities)')}
    for field in ('EmbeddedActiveAbilityIds', 'HiddenAbilityIds', 'ReferencedAbilityIds', 'ModalChildIds'):
        if field in columns:
            for (value,) in c.execute(f'SELECT {field} FROM Abilities'):
                for item in str(value or '').split(','):
                    bits = item.split(':')
                    if len(bits) == 2 and bits[1].isdigit() and int(bits[1]) > 0:
                        result[int(bits[1])].add('Abilities.ParameterTextId')
    return {str(k): sorted(v) for k, v in result.items()}


def source(c, name, path, version, evidence, expected=None):
    fp = fingerprint(path)
    require(expected is None or fp == expected, f'Historical source hash mismatch: {path}')
    old = c.execute('SELECT sha256,bytes FROM sources WHERE id=?', (name,)).fetchone()
    require(old is None or old == (fp['sha256'], fp['bytes']), f'Registered source changed: {name}')
    c.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?)',
              (name, str(path), fp['sha256'], fp['bytes'], version, json.dumps(evidence, ensure_ascii=False)))
    return fp


def add(c, layer, key, en, zh, origin, status, priority, use, verified_version=None):
    require(status in {'verified', 'candidate', 'needs-review', 'keep-english'}, 'Bad registry status')
    c.execute('INSERT INTO localization VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(layer,key,source_id) DO UPDATE SET field_use=excluded.field_use,formatting=excluded.formatting',
              (layer, str(key), en, zh, origin, status, priority, json.dumps(use),
               json.dumps({'english': format_metadata(en), 'translation': format_metadata(zh)}, ensure_ascii=False),
               verified_version, verified_version))


def is_mode_description(key):
    """A legacy Title path can contain a body; English-name policy stops there."""
    return bool(re.search(r'(?:^|[/_])(?:Desc(?:ription)?|Body|Instructions|Tooltip)(?:[/_]|$)',
                          str(key), re.IGNORECASE))


def is_english_event_name(key):
    """Preserve every event/category/format name, including future releases."""
    return (not is_mode_description(key) and bool(re.match(
        r'^(?:Events/(?:Events/)?Event_(?:Title|Cat)_|Formats/Format_Title_)', str(key))))


def english_event_text(english, lookup, seen=()):
    """Resolve names against official English without changing shared targets."""
    def replace(match):
        key = match[1]
        candidates = (key, key.lstrip('/'))
        found = next((lookup(k) for k in candidates if lookup(k) is not None), None)
        require(found is not None, 'Official event-name reference missing: ' + key)
        require(key not in seen and len(seen) < 32, 'Cyclic event-name reference: ' + key)
        return english_event_text(found, lookup, (*seen, key))
    return re.sub(r'\$([^$]+)\$', replace, english or '')


def bootstrap(c):
    """Import immutable, hash-guarded historical artifacts; never run old installers."""
    manifest = read_json(OLD / 'manifest.json')
    originals = {}
    for layer, fragment in [('card', 'Raw_CardDatabase_'), ('client', 'Raw_ClientLocalization_')]:
        entry = one((r for r in manifest['files'] if fragment in r['source']), layer)
        p = OLD / 'originals' / __import__('pathlib').Path(entry['source']).relative_to(manifest['game'])
        source(c, 'official-old-' + layer, p, '2026.63.10', {'manifest': str(OLD / 'manifest.json')},
               {'sha256': entry['source_sha256'], 'bytes': p.stat().st_size})
        originals[layer] = p
    nm = read_json(HISTORY / 'name-variants-manifest.json')
    fp = nm['candidates']['full']
    final_card = HISTORY / 'name-variants-full.mtga'
    source(c, 'historical-final-card', final_card, '2026.63.10',
           {'runtime': str(HISTORY / 'runtime-results.json'), 'scope': 'historical static full artifact and sampled client checks'},
           {'sha256': fp['sha256'], 'bytes': fp['bytes']})
    wpn = BACKUPS / '2026-10-02-085205-wpn-translations'
    wm = read_json(wpn / 'manifest.json')
    wp = one(wm['files'], 'WPN file')
    final_client = wpn / 'candidates' / __import__('pathlib').Path(wp['candidate']).name
    source(c, 'historical-final-client', final_client, '2026.63.10',
           {'runtime': str(wpn / 'evidence/runtime-verification.json')},
           {'sha256': wp['candidate_sha256'], 'bytes': final_client.stat().st_size})
    fra = BACKUPS / '2026-10-02-003712-fra-translations/evidence'
    evidence = {'FRA-card': fra / 'card-update.json', 'FRA-client': fra / 'client-update.json',
                'WPN-client': wpn / 'evidence/client-update.json'}
    changes = {}
    for label, path in evidence.items():
        source(c, label, path, '2026.63.10', {'kind': 'reviewed change provenance'})
        data = read_json(path)
        changes[label] = {str(r.get('locid', r.get('key'))) for r in data.get('changes', data.get('updates', []))}
    with database(originals['card']) as oc, database(final_card) as fc:
        old_roles = roles(oc)
        old_en = dict(oc.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1'))
        for loc, zh in fc.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1'):
            if loc not in old_en:
                continue
            origin = 'FRA-card' if str(loc) in changes['FRA-card'] else 'historical-final-card'
            add(c, 'card', loc, old_en[loc], zh, origin,
                'verified' if has_chinese(zh) else 'keep-english', 200, old_roles.get(str(loc), []), '2026.63.10')
    with database(originals['client']) as oc, database(final_client) as fc:
        old_en = dict(oc.execute('SELECT Key,enUS FROM Loc'))
        for key, zh in fc.execute('SELECT Key,enUS FROM Loc'):
            if key not in old_en:
                continue
            origin = ('WPN-client' if key in changes['WPN-client'] else
                      'FRA-client' if key in changes['FRA-client'] else 'historical-final-client')
            add(c, 'client', key, old_en[key], zh, origin,
                'verified' if has_chinese(zh) else 'keep-english', 200, ['Loc.enUS'], '2026.63.10')
    # The prior user-accepted patch deliberately kept mode/format/event names
    # English. A newer donor must not relabel those as untranslated candidates.
    policy_path = OLD / 'evidence/english-modes/verification.json'
    source(c, 'historical-mode-name-policy', policy_path, '2026.63.10', {'policy': 'preserve reviewed English mode/event/format names'})
    mode_keys = {r['key'] for r in read_json(policy_path)['changes']}
    retained = read_json(wpn / 'evidence/client-update.json').get('retained_English_event_titles', [])
    mode_keys.update(r['key'] for r in retained)
    for key in mode_keys:
        if key in old_en and not is_mode_description(key):
            add(c, 'client', key, old_en[key], old_en[key], 'historical-mode-name-policy',
                'keep-english', 250, ['Loc.enUS'], '2026.63.10')
    # Persist both donors. Unseen donor text remains a candidate until reviewed.
    for donor in DONORS:
        for layer, fragment in [('card', 'Raw_CardDatabase_'), ('client', 'Raw_ClientLocalization_')]:
            p = one((donor / 'Raw').glob(fragment + '*.mtga'), fragment)
            origin = donor.name + '-' + layer
            with database(p) as d:
                version = dict(d.execute('SELECT Type,Version FROM Versions')) if layer == 'card' else donor.name
                source(c, origin, p, json.dumps(version), {'donor': str(donor)})
                if layer == 'card':
                    en = dict(d.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1'))
                    zh = dict(d.execute('SELECT LocId,Loc FROM Localizations_jaJP WHERE Formatted=1'))
                    use = roles(d)
                else:
                    rows = list(d.execute('SELECT Key,enUS,jaJP FROM Loc'))
                    en = {k: e for k, e, z in rows}
                    zh = {k: z for k, e, z in rows}
                    use = {str(k): ['Loc.enUS'] for k in en}
                for key, english in en.items():
                    translated = zh.get(key)
                    equal = c.execute("SELECT 1 FROM localization WHERE layer=? AND key=? AND english IS ? AND chinese IS ? AND status='verified'",
                                      (layer, str(key), english, translated)).fetchone()
                    status = 'verified' if equal else ('candidate' if has_chinese(translated) else 'keep-english')
                    add(c, layer, key, english, translated, origin, status, 100,
                        use.get(str(key), []), '2026.63.10' if equal else None)
    # The historical UI migration has full per-key provenance; no old binary is needed.
    p = OLD / 'evidence/interface_translation_provenance.json'
    source(c, 'historical-ui', p, '2026.63.10', {'runtime': str(OLD / 'evidence/final_runtime_verification.json')})
    for row in read_json(p):
        add(c, 'ui', row['key'], row['original_english'], row['chinese_translation'],
            'historical-ui', 'verified', 200, ['LocLibraryData.en-US'], '2026.63.10')
    c.commit()
    return originals


def bind_contexts(c, contexts):
    """Ephemeral current input, shared by production and read-only replay."""
    c.execute('CREATE TEMP TABLE IF NOT EXISTS admission_context(layer TEXT,key TEXT,payload TEXT,PRIMARY KEY(layer,key))')
    c.execute('DELETE FROM admission_context')
    c.executemany('INSERT INTO admission_context VALUES (?,?,?)',
                  [(layer, str(key), json.dumps(value)) for (layer, key), value in contexts.items()])


def correction_scope(c, origin, layer, key, english, use):
    """A persisted human row cannot outlive the scope in its immutable approval."""
    if not origin.startswith(('manual-correction-', 'independent-resolution-')):
        return None
    from pathlib import Path
    source_row = c.execute('SELECT path,sha256,bytes FROM sources WHERE id=?', (origin,)).fetchone()
    if not source_row:
        return 'manual-approval-source-missing'
    path, sha, size = source_row
    require(fingerprint(Path(path)) == {'sha256': sha, 'bytes': size}, 'Manual approval source changed')
    records = [r for r in read_json(Path(path))['records'] if r['layer'] == layer and str(r['key']) == str(key)]
    require(len(records) == 1, 'Manual approval record missing or ambiguous')
    row = records[0]
    if row['english'] != english or row['field_use'] != use:
        return 'manual-correction-source-or-use-changed'
    if 'identity' in row:
        exists = c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='admission_context'").fetchone()
        current = c.execute('SELECT payload FROM admission_context WHERE layer=? AND key=?', (layer,str(key))).fetchone() if exists else None
        if not current:
            return 'manual-correction-identity-unavailable'
        context = json.loads(current[0])
        if (context['english'] != english or context['field_use'] != use or context['identity'] != row['identity']):
            return 'manual-correction-identity-or-context-changed'
    # Evidence declared by the approval is part of that approval, not optional research.
    def check(value):
        if isinstance(value, dict):
            if value.get('cache_path') and value.get('fingerprint'):
                require(fingerprint(Path(value['cache_path'])) == value['fingerprint'], 'Manual approval evidence changed')
            if value.get('path') and value.get('sha256') and 'bytes' in value:
                require(fingerprint(Path(value['path'])) == {'sha256': value['sha256'], 'bytes': value['bytes']}, 'Manual approval evidence changed')
            for item in value.values(): check(item)
        elif isinstance(value, list):
            for item in value: check(item)
    check(row.get('evidence', {}))
    return None


def decide(c, run, layer, key, english, use, *, persist=True):
    rows = c.execute('SELECT chinese,source_id,status,priority,english,field_use FROM localization WHERE layer=? AND key=? ORDER BY priority DESC,source_id',
                     (layer, str(key))).fetchall()
    scope_failures = {r[1]: correction_scope(c, r[1], layer, key, english, use) for r in rows if r[1].startswith(('manual-correction-', 'independent-resolution-'))}
    matches = [r for r in rows if r[4] == english and not scope_failures.get(r[1])]
    verified = [r for r in matches if r[2] == 'verified' and json.loads(r[5]) == use]
    if not use:
        # A reviewed text-only slot cannot lend its admission to another source.
        verified = [r for r in verified if r[1].startswith('independent-resolution-')]
    status, origin, value, reason = 'needs-review', None, english, 'new-key-or-locid' if not rows else 'english-changed-or-no-verified-translation'
    conflicts = []
    intentional = [r for r in matches if r[2] == 'keep-english' and r[3] >= 250
                   and not (layer == 'client' and r[1] == 'historical-mode-name-policy'
                            and is_mode_description(key))]
    if layer == 'client' and use == ['Loc.enUS'] and is_english_event_name(key):
        def official_reference(reference):
            exists = c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='admission_context'").fetchone()
            current = c.execute("SELECT payload FROM admission_context WHERE layer='client' AND key=?",
                                (reference,)).fetchone() if exists else None
            return json.loads(current[0])['english'] if current else None
        value = english_event_text(english, official_reference)
        status, origin, reason = 'keep-english', None, 'configured-English-event-name-policy'
        conflicts = [{'source': r[1], 'translation': r[0], 'status': r[2]}
                     for r in matches if r[0] != english and has_chinese(r[0])]
    elif intentional and (not verified or intentional[0][3] >= verified[0][3]):
        status, origin, value, reason = 'keep-english', intentional[0][1], english, 'explicit-reviewed-English-name-policy'
    elif not use and not any(r[1].startswith('independent-resolution-') for r in verified):
        reason = 'unknown-field-use'
    elif verified:
        top = [r for r in verified if r[3] == verified[0][3]]
        if len({r[0] for r in top}) > 1:
            reason = 'conflicting-verified-translations'
        else:
            row = top[0]
            conflicts = [{'source': r[1], 'translation': r[0], 'status': r[2]}
                         for r in matches if r[0] != row[0] and has_chinese(r[0])]
            value_candidate = row[0]
            repair = None
            # Preserve official leading digital/rebalanced-card icons around
            # an unchanged verified title; no name or rule text is invented.
            if layer == 'card' and any(v in use for v in ('Cards.TitleId', 'Cards.AltTitleId', 'Cards.InterchangeableTitleId')):
                prefix = re.match(r'^(?:<sprite\b[^>]*>)+', english or '')
                if prefix and '<sprite' not in (value_candidate or ''):
                    value_candidate = prefix[0] + (value_candidate or '')
                    repair = 'preserved-official-leading-title-sprite'
            from common import adapt_verified_format
            value_candidate, syntax_repair = adapt_verified_format(english, value_candidate)
            if syntax_repair:
                repair = ';'.join(x for x in (repair, syntax_repair) if x)
            failures = safe_format(english, value_candidate)
            if json.loads(row[5]) != use:
                reason = 'field-use-changed'
            elif failures:
                reason = 'format-unsafe:' + ','.join(failures)
            else:
                status, origin, value, reason = 'verified', row[1], value_candidate, 'exact-source-and-use-safe'
                if repair:
                    reason += ';' + repair
    elif any(r[2] == 'verified' for r in matches):
        reason = 'field-use-changed'
    elif any(r[2] == 'candidate' for r in matches):
        status, reason = 'candidate', 'donor-awaiting-review'
    elif matches and any(r[2] == 'keep-english' for r in matches):
        status, reason = 'keep-english', 'historical-intentional-or-no-chinese-source'
    from provenance import select
    provenance = select(c, layer, key, english, use)
    manual = status == 'verified' and any(r[1] == origin and (r[3] >= 300 or r[1].startswith('independent-resolution-')) for r in verified)
    if provenance and manual:
        reason += (';independent-AI-resolution-retained-over-source-candidate'
                   if origin.startswith('independent-resolution-')
                   else ';manual-correction-retained-over-source-candidate')
    if provenance and not manual and status != 'keep-english':
        translated, source_id, source_status, source_reason = provenance
        if source_status == 'verified':
            if status == 'verified' and value != translated and top[0][3] >= 200:
                status, origin, value, reason = 'needs-review', None, english, 'conflicting-applicable-verified-translations'
            elif status != 'verified' and reason == 'conflicting-verified-translations':
                pass  # Another source cannot erase an existing equal-trust conflict.
            else:
                status, origin, value, reason = source_status, source_id, translated, source_reason
        elif source_reason == 'conflicting-applicable-official-translations':
            status, origin, value, reason = 'needs-review', None, english, source_reason
        elif status != 'verified':
            status, origin, value, reason = source_status, source_id, english, source_reason
        else:
            reason += ';nonadmitted-source-evidence-retained'
    if any(scope_failures.values()):
        reason += ';' + ';'.join(sorted(set(filter(None, scope_failures.values()))))
    if conflicts:
        reason += ';lower-priority-source-conflict-recorded'
    if persist:
        c.execute('INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?)',
                  (run, layer, str(key), status, origin, reason, english, value, json.dumps(use)))
    if persist and status == 'verified':
        c.execute('UPDATE localization SET last_verified_version=? WHERE layer=? AND key=? AND source_id=?',
                  (run.split('_')[0], layer, str(key), origin))
    return value, {'key': str(key), 'status': status, 'source': origin, 'reason': reason, 'conflicts': conflicts,
                   'admission_scope': {'english': english, 'field_use': use},
                   'inapplicable_sources': [{'source':r[1], 'reason':'english-or-field-use-mismatch'} for r in rows
                                            if r[4] != english or json.loads(r[5]) != use],
                   'migration_eligible': status == 'verified', 'client_acceptance': False}


def import_donor_review(c, packet_path, review_path, version):
    review = read_json(review_path)
    require(review.get('evidence_kind') == 'independent_review_of_existing_donor' and
            review.get('packet_fingerprint') == fingerprint(packet_path), 'Donor review does not bind the packet')
    require(review.get('no_blocking_findings') is True, 'Donor review contains blocking findings')
    packet = {str(r['key']): r for r in read_json(packet_path)}
    approved = review['approved_keys']
    require(len(approved) == len(set(approved)) and set(approved) <= packet.keys(), 'Invalid donor review key list')
    origin = 'independent-reviewed-26.9.30-' + fingerprint(review_path)['sha256'][:16]
    source(c, origin, review_path, version,
           {'kind': review['evidence_kind'], 'packet': str(packet_path), 'scope': review.get('reviewer_scope'),
            'client_acceptance': False, 'human_semantic_acceptance': False})
    for key in approved:
        row = packet[key]
        stored = c.execute('SELECT chinese,english,field_use FROM localization WHERE layer=? AND key=? AND source_id=?',
                           (row['layer'], key, row['source_id'])).fetchone()
        require(stored is not None and stored[:2] == (row['chinese'], row['english']) and
                set(row['field_use']) <= set(json.loads(stored[2])), 'Donor row changed since review')
        require(row['field_use'] and not safe_format(row['english'], row['chinese']), 'Approved donor row is unsafe')
        # A later analyzer can recognize additional explicit references in this
        # same hash-guarded donor. Keep the complete source usage in the registry.
        add(c, row['layer'], key, row['english'], row['chinese'], origin, 'verified', 150, json.loads(stored[2]), version)
    c.commit()
    return len(approved)


def import_corrections(c, directory, official, version):
    """Explicit human-reviewed files; conflicting sources must be named, never hidden."""
    from provenance import contexts
    bind_contexts(c, contexts(official))
    maps, usages = {}, {}
    for layer in ('card', 'client'):
        with database(official[layer]) as db:
            query = ('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1' if layer == 'card'
                     else 'SELECT Key,enUS FROM Loc')
            maps[layer] = {str(k): en for k, en in db.execute(query)}
            usages[layer] = roles(db) if layer == 'card' else {key: ['Loc.enUS'] for key in maps[layer]}
    count = 0
    for path in sorted(directory.glob('*.json')):
        data = read_json(path)
        require(data.get('review_kind') == 'human' and isinstance(data.get('reviewed_by'), str) and
                data['reviewed_by'].strip() and isinstance(data.get('source_version'), str), 'Correction needs explicit human/source-version provenance')
        origin = 'manual-correction-' + fingerprint(path)['sha256'][:16]
        source(c, origin, path, data['source_version'], {'reviewed_by': data['reviewed_by'], 'review_kind': 'human', 'note': data.get('note', '')})
        for row in data['records']:
            layer, key = row['layer'], str(row['key'])
            require(layer in maps, 'Unknown correction layer')
            use = usages[layer].get(key, [])
            scope_failure = correction_scope(c, origin, layer, key, maps[layer].get(key), use)
            if key not in maps[layer] or scope_failure:
                from provenance import append
                append(c, 'provenance_reviews', {'review_kind': 'automatic-revalidation', 'status': 'needs-review',
                    'reason': scope_failure or 'manual-correction-key-missing', 'source_id': origin, 'record': row,
                    'current_english': maps[layer].get(key), 'client_version': version}, translation_id=origin)
                continue
            require(use and row['field_use'] == use and has_chinese(row['chinese']) and
                    not safe_format(row['english'], row['chinese']), 'Correction usage/format/Chinese content unsafe')
            existing = c.execute("SELECT source_id,chinese,priority FROM localization WHERE layer=? AND key=? AND english=? AND status='verified' AND source_id<>?",
                                 (layer, key, row['english'], origin)).fetchall()
            conflicts = {source_id for source_id, zh, priority in existing if zh != row['chinese']}
            require(conflicts <= set(row.get('replaces_source_ids', [])), 'Correction conflicts require explicit replaces_source_ids')
            current = c.execute('SELECT priority FROM localization WHERE layer=? AND key=? AND source_id=?', (layer, key, origin)).fetchone()
            priority = current[0] if current else max([299] + [r[2] for r in existing]) + 1
            add(c, layer, key, row['english'], row['chinese'], origin, 'verified', priority, use, version)
            from provenance import append
            append(c, 'provenance_reviews', {'review_kind': 'imported-human-attestation',
                'reviewer': data['reviewed_by'], 'source_id': origin, 'source_version': data['source_version'],
                'record': row, 'last_verified_version': version, 'status': 'verified',
                'basis': 'Explicit reviewed-corrections file, hash-bound by legacy sources; importer does not invent approval'}, translation_id=origin)
            count += 1
    c.commit()
    from reviewed_resolution import import_resolutions
    count += import_resolutions(c, official, version, directory.parent / 'reviewed-resolutions')
    return count
