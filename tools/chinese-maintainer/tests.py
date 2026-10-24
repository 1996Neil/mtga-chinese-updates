"""Regression tests for data preservation, evidence gates and transaction recovery."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from common import safe_format, save_json, fingerprint, read_json
from registry import SCHEMA, source, add, decide
from raw import validate_db, validate_names, canonical, deck_quantities
from installer import create_transaction, apply_transaction, restore_transaction


class FormatTests(unittest.TestCase):
    def test_named_and_numbered_placeholders(self):
        self.assertTrue(safe_format('Draw {0} cards, {cardName}', '抓{1}张牌，{cardName}'))
        self.assertTrue(safe_format('Card {cardName}', '卡牌'))
        self.assertFalse(safe_format('Card {cardName} {1}', '卡牌{1} {cardName}'))

    def test_mana_symbols_cannot_drift(self):
        self.assertTrue(safe_format('Pay {o(B/G)}', '支付{G/U}'))
        self.assertFalse(safe_format('Pay {o(B/G)}', '支付{o(B/G)}'))

    def test_icu_bodies_are_translatable_selectors_preserved(self):
        en = '{1, plural, =1 {a card} other {{1, Number} cards}}'
        zh = '{1, plural, =1 {一张牌} other {{1, Number}张牌}}'
        self.assertFalse(safe_format(en, zh))
        self.assertTrue(safe_format(en, zh.replace('=1', '=2')))

    def test_named_icu_type_style_and_selector_guards(self):
        en = '{count, plural, offset:1 one {one card} other {{value, number, currency} cards}}'
        zh = '{count, plural, offset:1 one {一张牌} other {{value, number, currency}张牌}}'
        self.assertFalse(safe_format(en, zh))
        for replacement in (('count', 'wrong'), ('currency', 'integer'), ('offset:1', 'offset:2'), ('one {', 'few {')):
            self.assertTrue(safe_format(en, zh.replace(*replacement)))
        self.assertTrue(safe_format('{value, number, currency}', '{value, date, short}'))
        self.assertTrue(safe_format('{value, date, short}', '{value, date, long}'))
        self.assertFalse(safe_format('{gender, select, male {A} female {B} other {C}}',
                                     '{gender, select, male {甲} female {乙} other {丙}}'))
        self.assertTrue(safe_format('{gender, select, male {A} female {B} other {C}}',
                                    '{gender, select, male {甲} unknown {乙} other {丙}}'))
        self.assertTrue(safe_format('{count, plural, other {x}}', '{count, plural, nonsense}'))
        self.assertFalse(safe_format('{0:N2}', '{0:N2}'))
        self.assertTrue(safe_format('{n, plural, one {{oR}} other {{oG}}}',
                                    '{n, plural, one {{oG}} other {{oR}}}'))

    def test_dynamic_link_and_sprite_are_preserved(self):
        self.assertTrue(safe_format('<link=https://a/>A</link>', '<link=https://b/>甲</link>'))
        self.assertFalse(safe_format('<link=https://a/>A</link>', '<link=https://a/>甲</link>'))
        self.assertTrue(safe_format('<sprite name="a">Card', '卡牌'))
        self.assertFalse(safe_format('<#FF9C01>{0}</color>', '<#FF9C01>{0}</color>'))

    def test_observed_arena_argument_grammar(self):
        for en, zh in [('Pay {o{2, Number}oR}?', '支付{o{2, Number}oR}？'),
                       ('Move {2, Enum:CounterType} counters', '移动{2, Enum:CounterType}指示物'),
                       ('Confirm {[InputInteraction]}?', '确认{[InputInteraction]}？'),
                       ('Honor {card title}', '祝耀{card title}')]:
            self.assertFalse(safe_format(en, zh))
        self.assertTrue(safe_format('{2, Enum:CounterType}', '{2, Enum:OtherType}'))
        self.assertTrue(safe_format('{o{2, Number}oR}', '{o{3, Number}oR}'))
        self.assertTrue(safe_format('{[InputInteraction]}', '{[Wrong]}'))

    def test_official_existing_f0_accents_win(self):
        self.assertEqual(canonical({0: 'Barad-dûr', 1: 'Barad-dûr', 2: 'Barad-dur'}), ('Barad-dûr', 0))
        self.assertEqual(canonical({1: '<i>Title</i>', 2: 'Title'}), ('Title', 2))

    def test_qualified_reprint_lines_aggregate_by_identity(self):
        self.assertEqual(deck_quantities([(3, 6330), (1, 6330)]), deck_quantities([(4, 6330)]))
        self.assertNotEqual(deck_quantities([(3, 6330), (1, 7000)]), deck_quantities([(4, 6330)]))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.executescript(SCHEMA)
        self.c.execute("INSERT INTO sources VALUES ('old','unused','hash',0,'old','{}')")

    def tearDown(self):
        self.c.close()

    def test_source_change_and_unknown_use_keep_official_english(self):
        add(self.c, 'card', '1', 'Old text', '旧文', 'old', 'verified', 200, ['Abilities.TextId'], 'old')
        value, result = decide(self.c, 'new_run', 'card', '1', 'New text', ['Abilities.TextId'])
        self.assertEqual(value, 'New text')
        self.assertEqual(result['status'], 'needs-review')
        value, result = decide(self.c, 'new_run', 'card', '1', 'Old text', [])
        self.assertEqual(value, 'Old text')
        self.assertEqual(result['reason'], 'unknown-field-use')

    def test_verified_conflict_is_held(self):
        add(self.c, 'card', '1', 'Title', '牌甲', 'old', 'verified', 200, ['Cards.TitleId'], 'old')
        add(self.c, 'card', '1', 'Title', '牌乙', 'other', 'verified', 200, ['Cards.TitleId'], 'old')
        value, result = decide(self.c, 'new_run', 'card', '1', 'Title', ['Cards.TitleId'])
        self.assertEqual(value, 'Title')
        self.assertEqual(result['reason'], 'conflicting-verified-translations')

    def test_donor_conflict_does_not_silently_override_verified(self):
        add(self.c, 'card', '1', 'Title', '牌甲', 'old', 'verified', 200, ['Cards.TitleId'], 'old')
        add(self.c, 'card', '1', 'Title', '牌乙', 'donor', 'candidate', 100, ['Cards.TitleId'])
        value, result = decide(self.c, 'new_run', 'card', '1', 'Title', ['Cards.TitleId'])
        self.assertEqual(value, '牌甲')
        self.assertTrue(result['conflicts'])

    def test_title_icon_restored_without_guessing_name(self):
        en = '<sprite name="arena_a">Title'
        add(self.c, 'card', '1', en, '牌名', 'old', 'verified', 200, ['Cards.TitleId'], 'old')
        value, result = decide(self.c, 'new_run', 'card', '1', en, ['Cards.TitleId'])
        self.assertEqual(value, '<sprite name="arena_a">牌名')
        self.assertEqual(result['status'], 'verified')

    def test_explicit_english_mode_policy_overrides_normal_donor(self):
        add(self.c, 'client', 'mode', 'Standard', 'Standard', 'policy', 'keep-english', 250, ['Loc.enUS'], 'old')
        add(self.c, 'client', 'mode', 'Standard', '标准', 'donor', 'verified', 100, ['Loc.enUS'], 'old')
        value, result = decide(self.c, 'new_run', 'client', 'mode', 'Standard', ['Loc.enUS'])
        self.assertEqual(value, 'Standard')
        self.assertEqual(result['status'], 'keep-english')

    def test_legacy_mode_name_policy_does_not_cover_description_under_title(self):
        key = 'Events/Event_Title_MWM_Cascade_Alchemy_Desc'
        en = 'Welcome to this event!'
        add(self.c, 'client', key, en, en, 'historical-mode-name-policy',
            'keep-english', 250, ['Loc.enUS'], 'old')
        add(self.c, 'client', key, en, '欢迎参加此赛事！', 'reviewed-donor',
            'verified', 150, ['Loc.enUS'], 'old')
        value, result = decide(self.c, 'new_run', 'client', key, en, ['Loc.enUS'])
        self.assertEqual(value, '欢迎参加此赛事！')
        self.assertEqual(result['status'], 'verified')

    def test_description_without_verified_translation_is_not_auto_approved(self):
        key = 'Events/Event_Desc_Untranslated'
        en = 'Build a deck and play.'
        add(self.c, 'client', key, en, en, 'historical-mode-name-policy',
            'keep-english', 250, ['Loc.enUS'], 'old')
        value, result = decide(self.c, 'new_run', 'client', key, en, ['Loc.enUS'])
        self.assertEqual(value, en)
        self.assertNotEqual(result['status'], 'verified')
        self.assertNotEqual(result['reason'], 'explicit-reviewed-English-name-policy')

    def test_legacy_policy_still_keeps_real_mode_title_english(self):
        key = 'Events/Event_Title_FRA_Quick_Draft'
        add(self.c, 'client', key, 'Quick Draft', 'Quick Draft',
            'historical-mode-name-policy', 'keep-english', 250, ['Loc.enUS'], 'old')
        add(self.c, 'client', key, 'Quick Draft', '快速轮抽', 'reviewed-donor',
            'verified', 150, ['Loc.enUS'], 'old')
        value, result = decide(self.c, 'new_run', 'client', key, 'Quick Draft', ['Loc.enUS'])
        self.assertEqual(value, 'Quick Draft')
        self.assertIn('configured-English-event-name-policy', result['reason'])

    def test_description_classifier_does_not_match_a_similar_title_word(self):
        from registry import is_mode_description
        self.assertTrue(is_mode_description('Events/Event_Title_MWM_Cascade_Alchemy_Desc'))
        self.assertTrue(is_mode_description('Events/Event_Desc_FRA_Quick_Draft'))
        self.assertFalse(is_mode_description('Events/Event_Title_Descend_Into_Adventure'))
        self.assertFalse(is_mode_description('Events/Event_Title_FRA_Quick_Draft'))

    def test_new_event_names_stay_english_even_without_historical_policy(self):
        for key in ('Events/Event_Title_FRA_Premier_Draft', 'Events/Event_Cat_Future_Draft',
                    'Events/Events/Event_Title_Future_Draft', 'Formats/Format_Title_Future'):
            with self.subTest(key=key):
                add(self.c, 'client', key, 'Future Draft', '未来轮抽', 'new-review',
                    'verified', 500, ['Loc.enUS'], 'new')
                text, result = decide(self.c, 'new_run', 'client', key, 'Future Draft', ['Loc.enUS'])
                self.assertEqual(text, 'Future Draft')
                self.assertEqual(result['status'], 'keep-english')
                self.assertIn('configured-English-event-name-policy', result['reason'])
                self.assertTrue(result['conflicts'])

    def test_event_description_and_navigation_titles_are_not_event_names(self):
        from registry import is_english_event_name
        for key in ('Events/Event_Title_MWM_Cascade_Alchemy_Desc',
                    'Events/Event_Desc_FRA_Premier_Draft', 'PlayBlade/Tabs/Event_Title',
                    'AbilityHanger/Keyword/Flying_Title'):
            with self.subTest(key=key):
                self.assertFalse(is_english_event_name(key))
                add(self.c, 'client', key, 'Read this text', '阅读说明', 'normal-review',
                    'verified', 500, ['Loc.enUS'], 'new')
                value, result = decide(self.c, 'new_run', 'client', key, 'Read this text', ['Loc.enUS'])
                self.assertEqual(value, '阅读说明')
                self.assertEqual(result['status'], 'verified')

    def test_new_event_names_without_sources_still_use_current_official_english(self):
        text, result = decide(self.c, 'new_run', 'client', 'Events/Event_Title_Upcoming',
                              'Upcoming Event', ['Loc.enUS'])
        self.assertEqual(text, 'Upcoming Event')
        self.assertEqual(result['status'], 'keep-english')
        self.assertFalse(result['migration_eligible'])

    def test_event_names_expand_official_references_without_changing_chinese_targets(self):
        from registry import bind_contexts, english_event_text
        bind_contexts(self.c, {
            ('client', 'General/Sets/HOB'): {'english': 'The Hobbit', 'field_use': ['Loc.enUS'],
                                           'identity': {'client_key': 'General/Sets/HOB'}},
            ('client', 'Events/Promo/Aang'): {'english': '<sprite name="xW"> Aang', 'field_use': ['Loc.enUS'],
                                            'identity': {'client_key': 'Events/Promo/Aang'}},
        })
        add(self.c, 'client', 'General/Sets/HOB', 'The Hobbit', '霍比特人', 'review',
            'verified', 500, ['Loc.enUS'], 'new')
        text, _ = decide(self.c, 'new', 'client', 'Events/Event_Title_HOB_Quick_Draft',
                         'Quick Draft $/General/Sets/HOB$', ['Loc.enUS'])
        self.assertEqual(text, 'Quick Draft The Hobbit')
        text, _ = decide(self.c, 'new', 'client', 'Events/Event_Title_Sealed_Aang',
                         'Sealed - $/Events/Promo/Aang$', ['Loc.enUS'])
        self.assertEqual(text, 'Sealed - <sprite name="xW"> Aang')
        value, _ = decide(self.c, 'new', 'client', 'General/Sets/HOB', 'The Hobbit', ['Loc.enUS'])
        self.assertEqual(value, '霍比特人')
        for mapping in ({'a': '$/a$'}, {}):
            with self.assertRaises(RuntimeError):
                english_event_text('$/a$', mapping.get)


class DatabaseTests(unittest.TestCase):
    def test_event_name_candidate_keeps_description_and_other_languages(self):
        from event_names import build
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            official, previous = root / 'official.sqlite', root / 'previous.sqlite'
            with sqlite3.connect(official) as db:
                db.executescript('CREATE TABLE Loc(Key TEXT PRIMARY KEY,enUS TEXT,jaJP TEXT);')
                db.executemany('INSERT INTO Loc VALUES (?,?,?)', [
                    ('Events/Event_Title_New', 'New Draft', 'name-other-language'),
                    ('Events/Event_Desc_New', 'Build a deck.', 'description-other-language'),
                    ('Events/Event_Title_MWM_Cascade_Alchemy_Desc', 'Welcome!', 'body-other-language'),
                    ('PlayBlade/Tabs/Event_Title', 'Events', 'navigation-other-language'),
                ])
                db.commit()
                with sqlite3.connect(previous) as dst:
                    db.backup(dst)
                    dst.execute("UPDATE Loc SET enUS='新轮抽' WHERE Key='Events/Event_Title_New'")
                    dst.execute("UPDATE Loc SET enUS='构筑套牌。' WHERE Key='Events/Event_Desc_New'")
                    dst.execute("UPDATE Loc SET enUS='欢迎！' WHERE Key='Events/Event_Title_MWM_Cascade_Alchemy_Desc'")
                    dst.execute("UPDATE Loc SET enUS='赛事' WHERE Key='PlayBlade/Tabs/Event_Title'")
            result = build(official, previous, root / 'candidate')
            self.assertEqual(result['restored_name_count'], 1)
            with sqlite3.connect(root / 'candidate/client-localized.mtga') as db:
                rows = {r[0]: r[1:] for r in db.execute('SELECT * FROM Loc')}
                self.assertEqual(rows['Events/Event_Title_New'][0], 'New Draft')
                self.assertEqual(rows['Events/Event_Desc_New'], ('构筑套牌。', 'description-other-language'))
                self.assertEqual(rows['Events/Event_Title_MWM_Cascade_Alchemy_Desc'][0], '欢迎！')
                self.assertEqual(rows['PlayBlade/Tabs/Event_Title'][0], '赛事')

    def test_client_validator_requires_exact_official_name_reference_expansion(self):
        from raw import validate_db
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, target = root / 'source.sqlite', root / 'target.sqlite'
            key = 'Events/Event_Title_Fixture'
            with sqlite3.connect(source) as db:
                db.executescript('CREATE TABLE Loc(Key TEXT PRIMARY KEY,enUS TEXT);')
                db.executemany('INSERT INTO Loc VALUES (?,?)',
                               [(key, 'Draft $/Sets/Fixture$'), ('Sets/Fixture', '<sprite name="a"> Fiction')])
                db.commit()
                with sqlite3.connect(target) as dst:
                    db.backup(dst)
                    dst.execute('UPDATE Loc SET enUS=? WHERE Key=?', ('Draft <sprite name="a"> Fiction', key))
            self.assertEqual(validate_db(source, target, 'client',
                                        {key: 'Draft <sprite name="a"> Fiction'})['status'], 'PASS')
            with self.assertRaises(RuntimeError):
                validate_db(source, target, 'client', {key: 'Draft <sprite name="a"> Invented'})

    def test_wrong_card_binding_and_token_only_variant_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            official, localized, candidate = [root / name for name in ('official', 'localized', 'candidate')]
            with sqlite3.connect(official) as c:
                c.executescript('CREATE TABLE Localizations_enUS(LocId INTEGER,Formatted INTEGER,Loc TEXT,PRIMARY KEY(LocId,Formatted)); CREATE TABLE Cards(GrpId INTEGER PRIMARY KEY,TitleId INTEGER,IsToken INTEGER,IsPrimaryCard INTEGER);')
                c.executemany('INSERT INTO Localizations_enUS VALUES (?,?,?)', [(1, 1, 'Opt'), (2, 1, 'Treasure')])
                c.executemany('INSERT INTO Cards VALUES (?,?,?,?)', [(100, 1, 0, 1), (200, 2, 1, 0)])
                c.commit()
                for p in (localized, candidate):
                    with sqlite3.connect(p) as d:
                        c.backup(d)
                        d.execute("UPDATE Localizations_enUS SET Loc='抉择' WHERE LocId=1")
            with sqlite3.connect(candidate) as c:
                c.execute("INSERT INTO Localizations_enUS VALUES (1,0,'Mountain')")
            with self.assertRaises(RuntimeError):
                validate_names(official, localized, candidate, [(1, 'Mountain')])
            with sqlite3.connect(candidate) as c:
                c.execute("UPDATE Localizations_enUS SET Loc='Opt' WHERE LocId=1 AND Formatted=0")
                c.execute("INSERT INTO Localizations_enUS VALUES (2,0,'Treasure')")
            with self.assertRaises(RuntimeError):
                validate_names(official, localized, candidate, [(1, 'Opt'), (2, 'Treasure')])

    def test_other_tables_and_f0_preserved_and_guarded(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp).resolve() / 'official.mtga', Path(temp).resolve() / 'candidate.mtga'
            c = sqlite3.connect(a)
            c.executescript('CREATE TABLE Localizations_enUS(LocId INTEGER,Formatted INTEGER,Loc TEXT,PRIMARY KEY(LocId,Formatted)); CREATE TABLE Cards(GrpId INTEGER PRIMARY KEY,TitleId INTEGER);')
            c.executemany('INSERT INTO Localizations_enUS VALUES (?,?,?)', [(1, 0, 'Opt'), (1, 1, 'Opt')])
            c.execute('INSERT INTO Cards VALUES (100,1)')
            c.commit()
            with sqlite3.connect(b) as z:
                c.backup(z)
                z.execute("UPDATE Localizations_enUS SET Loc='抉择' WHERE Formatted=1")
            self.assertEqual(validate_db(a, b, 'card', {'1': '抉择'})['status'], 'PASS')
            with sqlite3.connect(b) as z:
                z.execute('UPDATE Cards SET TitleId=2')
            with self.assertRaises(RuntimeError):
                validate_db(a, b, 'card', {'1': '抉择'})
            c.close()


class VersionBaselineTests(unittest.TestCase):
    def test_next_release_compares_previous_official_not_fixed_history(self):
        from detect import comparison_official
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            previous, run = root / 'runs/previous_20261001', root / 'runs/current_20261009'
            previous.mkdir(parents=True)
            run.mkdir()
            historical = {}
            snapshot = {'client_version': 'old', 'steam_build': '1', 'data_versions': {},
                        'official_entries': {}, 'files': {}}
            for layer in ('card', 'client'):
                path = previous / 'official' / (layer + '.bin')
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(('previous-official-' + layer).encode())
                h = root / (layer + '-historical')
                h.write_bytes(b'fixed-old-history')
                historical[layer] = h
                snapshot['files'][layer] = {'relative': path.name}
                snapshot['official_entries'][path.name] = {'fingerprint': fingerprint(path)}
            save_json(previous / 'snapshot.json', snapshot)
            save_json(previous / 'report.json', {'schema': 'MTGA_CN_MAINTAINER_V1'})
            current = {**snapshot, 'client_version': 'new', 'steam_build': '2'}
            with patch('detect.STATE', root):
                result = comparison_official(current, run, historical)
            self.assertEqual(result['card'].read_bytes(), b'previous-official-card')
            (previous / 'official/card.bin').write_bytes(b'corrupted')
            with patch('detect.STATE', root), self.assertRaises(RuntimeError):
                comparison_official(current, root / 'runs/future', historical)

    def test_new_card_proof_cannot_be_emptied_to_bypass_runtime(self):
        from cli import validate_run, review_input_fingerprints
        from reporting import code_fingerprints
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            file = root / 'new-cards.json'
            save_json(file, [{'arena_grp_id': 1}])
            report = {'schema': 'MTGA_CN_MAINTAINER_V1', 'code_fingerprints': code_fingerprints(),
                      'review_inputs': review_input_fingerprints(), 'proof_files': {'new-cards.json': fingerprint(file)}}
            save_json(file, [])
            with self.assertRaisesRegex(RuntimeError, 'proof/fixture changed'):
                validate_run(root, report)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.game, self.run = self.root / 'game', self.root / 'runs/run'
        self.game.mkdir()
        self.run.mkdir(parents=True)
        self.layers = ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')
        self.snapshot = {'client_version': 'new', 'steam_build': '42', 'data_versions': {'Data': 'new'},
                         'files': {}, 'manifests': {}, 'official_entries': {}}
        self.report = {'run_id': 'new_run', 'candidate_files': {}}
        for layer in self.layers:
            file = self.game / (layer + '.bin')
            file.write_bytes(('official-' + layer).encode())
            self.snapshot['files'][layer] = {'relative': file.name, **fingerprint(file)}
            candidate = self.run / (layer + '.bin')
            candidate.write_bytes(('candidate-' + layer).encode())
            self.report['candidate_files'][layer] = {'relative': file.name, 'path': str(candidate), **fingerprint(candidate)}
        protected = self.game / 'executable'
        protected.write_bytes(b'protected-official-binary')
        self.snapshot['files']['executable'] = {'relative': 'executable', **fingerprint(protected)}
        manifest = self.game / 'official-manifest.json'
        manifest.write_bytes(b'official-manifest-never-modified')
        self.snapshot['manifests'][manifest.name] = fingerprint(manifest)
        official = self.run / 'official'; official.mkdir()
        import shutil
        for file in self.game.iterdir(): shutil.copyfile(file, official / file.name)
        for layer in ('card','client','font_bundle'):
            self.snapshot['official_entries'][layer+'.bin'] = {'fingerprint': fingerprint(official/(layer+'.bin'))}
        self.pins = {layer: {'fingerprint': fingerprint(official/(layer+'.bin')), 'authority':'synthetic-reviewed-pin', 'evidence':{}}
                     for layer in ('resources','bootstrap_fonts')}
        self.pin_patch = patch('official_base.opaque_baseline', return_value=self.pins)
        self.pin_patch.start(); self.addCleanup(self.pin_patch.stop)
        save_json(self.run / 'snapshot.json', self.snapshot)
        self.journal = self.root / 'journal.json'

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self, game):
        result = copy.deepcopy(self.snapshot)
        for row in result['files'].values():
            row.update(fingerprint(game / row['relative']))
        for key in result['manifests']:
            result['manifests'][key] = fingerprint(game / key)
        return result

    def transaction(self, stopped=lambda: None):
        return create_transaction(self.report, self.run, self.game, self.root / 'backups', self.inspect, stopped, self.root)

    def test_exact_current_state_rollback(self):
        tx = self.transaction()
        apply_transaction(tx, self.journal, 'trial', self.game, self.inspect, lambda: None)
        restore_transaction(tx, self.journal, self.game, self.inspect, lambda: None)
        self.assertEqual(self.inspect(self.game), self.snapshot)
        self.assertEqual(read_json(self.journal)['state'], 'rolled-back')

    def test_mid_install_exception_restores_all_changed_files(self):
        tx = self.transaction()
        def failure(row):
            if row['layer'] == 'client':
                raise OSError('injected disk failure')
        with self.assertRaises(OSError):
            apply_transaction(tx, self.journal, 'installed', self.game, self.inspect, lambda: None, failure)
        self.assertEqual(self.inspect(self.game), self.snapshot)

    def test_crash_intent_can_be_recovered(self):
        tx = self.transaction()
        with self.assertRaises(SystemExit):
            apply_transaction(tx, self.journal, 'trial', self.game, self.inspect, lambda: None,
                              lambda row: (_ for _ in ()).throw(SystemExit('simulated crash')))
        tx = read_json(self.journal)
        self.assertEqual(tx['state'], 'installing')
        restore_transaction(tx, self.journal, self.game, self.inspect, lambda: None)
        self.assertEqual(self.inspect(self.game), self.snapshot)

    def test_unknown_later_update_blocks_all_rollback_writes(self):
        tx = self.transaction()
        apply_transaction(tx, self.journal, 'installed', self.game, self.inspect, lambda: None)
        (self.game / 'executable').write_bytes(b'new-official-update')
        before = self.inspect(self.game)
        with self.assertRaises(RuntimeError):
            restore_transaction(tx, self.journal, self.game, self.inspect, lambda: None)
        self.assertEqual(self.inspect(self.game), before)

    def test_corrupted_backup_blocks_before_any_restore(self):
        tx = self.transaction()
        apply_transaction(tx, self.journal, 'trial', self.game, self.inspect, lambda: None)
        Path(tx['files'][-1]['before_path']).write_bytes(b'corrupted')
        before = self.inspect(self.game)
        with self.assertRaises(RuntimeError):
            restore_transaction(tx, self.journal, self.game, self.inspect, lambda: None)
        self.assertEqual(self.inspect(self.game), before)

    def test_running_game_rejected_before_backup(self):
        def running():
            raise RuntimeError('MTGA running')
        with self.assertRaises(RuntimeError):
            self.transaction(running)
        self.assertFalse((self.root / 'backups').exists())


class InstallGateTests(unittest.TestCase):
    def test_static_green_cannot_install_without_client_evidence(self):
        import cli
        report = {'safe_to_install': False, 'blockers': ['current client evidence pending']}
        with patch.object(cli, 'latest', return_value=(Path('/unused'), report)), patch.object(cli, 'validate_run'), patch.object(cli, 'game_stopped') as stopped:
            with self.assertRaises(RuntimeError):
                cli.install()
            stopped.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
