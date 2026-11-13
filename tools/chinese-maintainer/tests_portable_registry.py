"""Portable community-context selection tests; synthetic data, no real key or game."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import baseline
from common import fingerprint, digest
import portable_registry as portable
import registry
import raw
REAL_VERIFY_SOURCE = portable.verify_source


class PortableRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.release = {'id': '2026.64.50-25919252-r4', 'client_version': '2026.64.50',
                        'steam_build': '25919252', 'acceptance': {'status': 'accepted'},
                        'resources': [{'layer': layer, 'input': {'sha256': 'a' * 64, 'bytes': 1},
                                       'output': {'sha256': 'b' * 64, 'bytes': 2}} for layer in
                                      ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')]}
        self.card = {'GrpId': 100, 'TitleId': 900, 'ExpansionCode': 'SYN',
                     'CollectorNumber': '1', 'IsToken': 0, 'IsPrimaryCard': 1,
                     'IsDigitalOnly': 0, 'IsRebalanced': 0, 'LinkedFaceType': 0, 'LinkedFaceGrpIds': ''}
        self.records = {
            'card': [self.row('card', '900', 'Opt', '抉择', ['Cards.TitleId'],
                              {'cards': [self.card]})],
            'client': [self.row('client', 'Menu/Play', 'Play', '对战', ['Loc.enUS'],
                                {'client_key': 'Menu/Play'}),
                       self.row('client', 'Retained', 'Text', 'Text', ['Loc.enUS'],
                                {'client_key': 'Retained'}, 'keep-english')],
            'ui': [self.row('ui', 'Menu/Play', 'Play', '开始', ['LocLibraryData.en-US'],
                            {'ui_key': 'Menu/Play'})],
            'titles': [{'grp_id': 100, 'title_id': 900, 'english': 'Opt', 'chinese': '抉择',
                        'identity': self.card}],
        }
        (self.root / 'source-release.json').write_text(json.dumps(self.release))
        self.rewrite()
        verifier = patch.object(portable, 'verify_source', return_value=self.release)
        verifier.start()
        self.addCleanup(verifier.stop)

    def row(self, layer, key, en, zh, use, identity, disposition='translated'):
        value = {'layer': layer, 'key': key, 'english': en, 'chinese': zh,
                 'field_use': use, 'identity': identity, 'disposition': disposition,
                 'human_acceptance': False, 'runtime_acceptance': False,
                 'source': {'kind': 'community-localization', 'release_id': self.release['id'],
                            'resource_layer': 'resources' if layer == 'ui' else layer,
                            'output_sha256': 'b' * 64}}
        value.update(english_sha256=digest(en.encode()), chinese_sha256=digest(zh.encode()),
                     context_sha256=baseline.context_hash(value))
        return value

    def rewrite(self):
        files = {}
        for layer, rows in self.records.items():
            path = self.root / (layer + '.jsonl')
            path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
            files[layer] = {'name': path.name, 'records': len(rows), 'fingerprint': fingerprint(path)}
        manifest = {'schema': portable.SCHEMA, 'version': '2026.64.50', 'steam_build': '25919252',
                    'source_release_id': self.release['id'], 'source_kind': 'community-localization',
                    'public_key': 'synthetic', 'human_acceptance': False, 'runtime_acceptance': False,
                    'files': files, 'source_manifest': {'name': 'source-release.json',
                                                       'fingerprint': fingerprint(self.root / 'source-release.json')},
                    'source_output_fingerprints': {r['layer']: r['output'] for r in self.release['resources']},
                    'source_official_raw_fingerprints': {r['layer']: r['input'] for r in self.release['resources']
                                                         if r['layer'] in {'card', 'client'}}}
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        self.seal = fingerprint(self.root / 'manifest.json')['sha256']

    def load(self):
        return portable.PortableBaseline(self.root, self.seal)

    def connection(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript(registry.SCHEMA)
        return connection

    def test_identical_context_adopts_only_as_community_and_never_verified(self):
        data = self.load()
        connection = self.connection()
        data.register(connection)
        context = self.records['card'][0]
        baseline.activate(connection, data, {('card', '900'): context})
        text, decision = registry.decide(connection, 'fixture', 'card', '900', 'Opt', ['Cards.TitleId'])
        self.assertEqual(text, '抉择')
        self.assertEqual(decision['status'], 'baseline-adopted')
        self.assertFalse(decision['client_acceptance'])
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM localization').fetchone()[0], 0)
        self.assertTrue(all(json.loads(row[0])['human_acceptance'] is False for row in
                            connection.execute('SELECT evidence FROM sources')))

    def test_english_identity_use_and_format_changes_are_not_adopted(self):
        data = self.load()
        original = self.records['card'][0]
        for changed in (original | {'english': 'Opt!'},
                        original | {'field_use': ['Cards.ReminderTextId']},
                        original | {'identity': {'cards': [self.card | {'IsRebalanced': 1}]}},
                        original | {'field_use': []}):
            self.assertIsNone(data.check('card', '900', changed)[0])
        self.records['card'][0] = self.row('card', '900', 'Draw {0}', '抓牌',
                                           ['Abilities.TextId'], {'cards': [self.card]})
        self.rewrite()
        self.assertIn('format-unsafe', self.load().check('card', '900', self.records['card'][0])[1])

    def test_ui_uses_its_own_context_not_same_client_key(self):
        data = self.load()
        row = self.records['ui'][0]
        self.assertEqual(data.check('ui', 'Menu/Play', row)[0], '开始')
        self.assertIsNone(data.check('ui', 'Menu/Play', self.records['client'][0])[0])
        self.assertIsNone(data.check('ui', 'new-ui-key', row)[0])

    def test_unchanged_english_remains_explicit_retention_without_review_upgrade(self):
        data = self.load()
        connection = self.connection()
        data.register(connection)
        baseline.activate(connection, data, {('client', 'Retained'): self.records['client'][1]})
        text, row = registry.decide(connection, 'fixture', 'client', 'Retained', 'Text', ['Loc.enUS'])
        self.assertEqual(text, 'Text')
        self.assertEqual(row['status'], 'keep-english')
        self.assertEqual(row['reason'], 'portable-existing-English-retained')

    def test_existing_reviewed_override_and_conflicts_are_not_erased(self):
        data = self.load()
        connection = self.connection()
        connection.execute("INSERT INTO sources VALUES ('review','unused','hash',0,'old','{}')")
        registry.add(connection, 'card', '900', 'Opt', '既有已审', 'review', 'verified', 300,
                     ['Cards.TitleId'], 'old')
        baseline.activate(connection, data, {('card', '900'): self.records['card'][0]})
        text, row = registry.decide(connection, 'fixture', 'card', '900', 'Opt', ['Cards.TitleId'])
        self.assertEqual((text, row['status']), ('既有已审', 'verified'))

    def test_dirty_data_or_rewritten_manifest_cannot_pass_pinned_hashes(self):
        data = self.load()
        (self.root / 'ui.jsonl').write_text('{}\n')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            data.guard()
        self.rewrite()
        with self.assertRaisesRegex(RuntimeError, 'pinned'):
            portable.PortableBaseline(self.root, '0' * 64)
        self.records['card'][0]['context_sha256'] = '0' * 64
        self.rewrite()
        with self.assertRaisesRegex(RuntimeError, 'context hash'):
            self.load()

    def test_legacy_registry_is_rejected_without_mutating_original_tables(self):
        data = self.load()
        connection = self.connection()
        connection.execute("INSERT INTO sources VALUES ('legacy','missing','hash',0,'old','{}')")
        before = connection.execute('SELECT * FROM sqlite_master').fetchall()
        with self.assertRaisesRegex(RuntimeError, 'isolated Registry'):
            data.register(connection)
        self.assertEqual(connection.execute('SELECT * FROM sqlite_master').fetchall(), before)

    def test_delta_and_name_history_are_derived_from_prior_text_not_old_binaries(self):
        data = self.load()
        current = {('card', '900'): self.records['card'][0] | {'english': 'Changed'},
                   ('client', 'new'): {'english': 'New', 'field_use': ['Loc.enUS'],
                                       'identity': {'client_key': 'new'}}}
        delta = data.diff(current)
        self.assertEqual(delta['card']['changed_english'][0]['before'], 'Opt')
        self.assertEqual(delta['client']['added_keys'], ['new'])
        self.assertIn('Menu/Play', delta['client']['removed_keys'])
        history = data.name_history()
        self.assertEqual(history['cards'][100], tuple(self.card[key] for key in portable.IDENTITY_FIELDS))
        self.assertEqual(history['titles'][900], 'Opt')

    def test_portable_configured_requires_external_manifest_lock(self):
        with patch.dict(os.environ, {'MTGA_ONLINE': '1', 'MTGA_PORTABLE_ROOT': str(self.root)}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'pinned'):
                portable.configured()

    def test_fixed_trust_key_and_native_signature_failures_are_not_bypassed(self):
        public = self.root / 'public'
        public.write_text('wrong-key')
        expected = json.loads((portable.ROOT.parent / 'subscriber/profile.json').read_text())['public_key']
        manifest = {'public_key': expected}
        environment = {'MTGA_PUBLISHER_CLIENT': '/unused/synthetic-client',
                       'MTGA_PUBLIC_KEY_FILE': str(public)}
        with patch.dict(os.environ, environment), \
             patch.object(portable.subprocess, 'run') as process:
            with self.assertRaisesRegex(RuntimeError, 'public key'):
                REAL_VERIFY_SOURCE(self.root / 'source-release.json', manifest)
            self.assertEqual(process.call_count, 0)
            public.write_text(expected)
            process.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, 'signature'):
                REAL_VERIFY_SOURCE(self.root / 'source-release.json', manifest)

    def test_current_input_copies_are_disclosed_not_labelled_historical_originals(self):
        data = self.load()
        official = {}
        for layer in ('card', 'client'):
            path = self.root / ('current-' + layer)
            path.write_text('current-' + layer)
            official[layer] = path
        run = self.root / 'new-run'
        copies = portable.comparison_inputs(official, run, data)
        report = json.loads((run / 'comparison-source.json').read_text())
        self.assertEqual(report['comparison_kind'], 'portable-text-contexts')
        self.assertIn('not these current-input copies', report['scope'])
        self.assertEqual(report['origin'], self.release['id'])
        for layer in ('card', 'client'):
            self.assertEqual(fingerprint(copies[layer]), fingerprint(official[layer]))


class PortableFixturesTests(unittest.TestCase):
    def test_current_fixtures_need_no_historical_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            official, candidate = root / 'official.sqlite', root / 'candidate.sqlite'
            with sqlite3.connect(official) as connection:
                connection.executescript('CREATE TABLE Localizations_enUS(LocId,Formatted,Loc);'
                                        'CREATE TABLE Cards(GrpId,TitleId,ExpansionCode,CollectorNumber,'
                                        'IsPrimaryCard,IsToken);')
                connection.executemany('INSERT INTO Localizations_enUS VALUES(?,?,?)',
                                       [(1, 1, 'Mountain'), (2, 1, 'Opt')])
                connection.executemany('INSERT INTO Cards VALUES(?,?,?,?,?,?)',
                                       [(11, 1, 'SYN', '1', 1, 0), (12, 2, 'SYN', '2', 1, 0)])
            shutil.copyfile(official, candidate)
            with sqlite3.connect(candidate) as connection:
                connection.executemany('INSERT INTO Localizations_enUS VALUES(?,?,?)',
                                       [(1, 0, 'Mountain'), (2, 0, 'Opt')])
                connection.execute("UPDATE Localizations_enUS SET Loc='山脉' WHERE LocId=1 AND Formatted=1")
                connection.execute("UPDATE Localizations_enUS SET Loc='抉择' WHERE LocId=2 AND Formatted=1")
            with patch.object(raw, 'HISTORY', root / 'does-not-exist'):
                result = raw.regressions(official, candidate, root / 'fixtures',
                                         {'new_card_groups': [12],
                                          'special_structures': {'by_linked_face_type': {}}},
                                         portable=True)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(len(result['fixtures']), 5)
            self.assertTrue(all(row['cards'] == 60 for row in result['fixtures'].values()))
            self.assertFalse((root / 'does-not-exist').exists())


if __name__ == '__main__':
    unittest.main()
