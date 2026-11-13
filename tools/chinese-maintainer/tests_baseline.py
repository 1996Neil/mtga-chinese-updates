"""Baseline reuse must not weaken reviewed overrides, identity or format gates."""
import copy
import json
import shutil
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import baseline as b
from build_baseline import generate
from common import fingerprint
from provenance import contexts
from registry import decide
from tests_admission import minimal_official, new_registry, seed_legacy
import workload


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.row = {'layer': 'card', 'key': '42', 'english': 'Draw a card.',
                    'field_use': ['Cards.AbilityTextId']}
        self.official = minimal_official(self.root / 'run/official', [self.row,
            {'layer': 'client', 'key': 'Fixture/Body', 'english': 'Draw a card.',
             'field_use': ['Loc.enUS']}])
        self.run = self.root / 'run'
        self.snapshot = {'client_version': 'fixture', 'files': {},
                         'official_entries': {}, 'steam_build': '1'}
        for layer, path in self.official.items():
            self.snapshot['files'][layer] = {'relative': path.name}
            self.snapshot['official_entries'][path.name] = {'fingerprint': fingerprint(path)}
        (self.run / 'snapshot.json').write_text(json.dumps(self.snapshot))
        self.client = self.root / 'client.jsonl'
        self.card = self.root / 'card.jsonl'
        self.client.write_text(json.dumps({'key': 'Fixture/Body', 'en': 'Draw a card.', 'zh': '抓一张牌。'}) + '\n')
        self.card.write_text(json.dumps({'locid': 42, 'en': 'Draw a card.', 'zh': '抓一张牌。'}) + '\n')
        self.repo = self.root / 'private-baseline'
        generate(self.repo, 'fixture', self.run, self.client, self.card)
        self.base = b.Baseline(self.repo, 'fixture')
        self.ctx = contexts(self.official)
        self.c = new_registry()

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def activate(self):
        b.activate(self.c, self.base, self.ctx)

    def decision(self, english='Draw a card.', use=None):
        return decide(self.c, 'fixture_run', 'card', '42', english,
                      use if use is not None else ['Cards.AbilityTextId'])

    def test_import_preserves_chinese_bytes_and_does_not_claim_acceptance(self):
        self.assertEqual(self.client.read_bytes(),
                         (self.repo / 'baseline/zhcn-client-fixture.jsonl').read_bytes())
        self.assertFalse(self.base.manifest['human_acceptance'])
        self.assertFalse(self.base.manifest['runtime_acceptance'])

    def test_opt_in_adopts_only_unreviewed_safe_fallback(self):
        self.assertEqual(self.decision()[0], 'Draw a card.')
        seed_legacy(self.c, status='candidate', priority=100)
        before = list(self.c.execute('SELECT * FROM localization'))
        self.activate()
        value, result = self.decision()
        self.assertEqual(value, '抓一张牌。')
        self.assertEqual(result['status'], 'baseline-adopted')
        self.assertEqual(before, list(self.c.execute('SELECT * FROM localization')))

    def test_reviewed_override_and_changed_trusted_english_stay_protected(self):
        seed_legacy(self.c, text='抓取一张牌。')
        self.activate()
        self.assertEqual(self.decision()[0], '抓取一张牌。')
        # A prior reviewed record with changed English must not be silently
        # rescued by a separately matching baseline.
        self.c.execute("UPDATE localization SET english='Old rule.'")
        value, result = self.decision()
        self.assertEqual(value, 'Draw a card.')
        self.assertEqual(result['status'], 'needs-review')

    def test_english_identity_and_use_changes_are_rejected(self):
        ctx = self.ctx['card', '42']
        for field, value, reason in [
            ('english', 'Draw two cards.', 'english-changed'),
            ('identity', {'cards': []}, 'identity-or-use-changed'),
            ('field_use', ['Cards.TitleId'], 'identity-or-use-changed')]:
            changed = {**ctx, field: value}
            text, why = self.base.check('card', '42', changed)
            self.assertIsNone(text)
            self.assertIn(reason, why)

    def test_format_failure_and_ui_never_borrow_client_baseline(self):
        row = self.base.rows['card']['42']
        row['zh'] = '抓{wrong}张牌。'
        self.assertIn('format-unsafe', self.base.check('card', '42', self.ctx['card', '42'])[1])
        self.assertEqual(self.base.check('ui', 'Fixture/Body', self.ctx['client', 'Fixture/Body']),
                         (None, 'baseline-missing'))

    def test_explicit_english_policy_wins(self):
        seed_legacy(self.c, text='Draw a card.', status='keep-english', priority=300)
        self.activate()
        self.assertEqual(self.decision()[1]['status'], 'keep-english')

    def test_source_candidate_or_conflict_cannot_be_masked(self):
        self.c.execute('CREATE TEMP TABLE provenance_selection(layer TEXT,key TEXT,english TEXT,'
                       'field_use TEXT,translation TEXT,source TEXT,status TEXT,reason TEXT)')
        self.c.execute('INSERT INTO provenance_selection VALUES (?,?,?,?,?,?,?,?)',
                       ('card', '42', 'Draw a card.', json.dumps(['Cards.AbilityTextId'], separators=(',', ':')),
                        None, 'provenance:test', 'needs-review', 'conflicting-applicable-official-translations'))
        self.activate()
        self.assertEqual(self.decision()[0], 'Draw a card.')
        self.assertEqual(self.decision()[1]['status'], 'needs-review')

    def test_missing_hash_changed_duplicate_or_wrong_version_fail_closed(self):
        with self.assertRaises((OSError, RuntimeError)):
            b.Baseline(self.repo, 'different-version')
        file = self.repo / 'baseline/zhcn-client-fixture.jsonl'
        file.write_bytes(file.read_bytes() + b'\n')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.base.guard()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            b.Baseline(self.repo, 'fixture')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            generate(self.repo, 'fixture', self.run, self.client, self.card)
        self.card.write_bytes(self.card.read_bytes() * 2)
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            generate(self.root / 'duplicate', 'fixture', self.run, self.client, self.card)
        self.assertFalse((self.root / 'duplicate/baseline').exists())

    def test_workload_does_not_guess_new_keys_or_modify_registry(self):
        registry_path = self.root / 'registry.sqlite'
        with sqlite3.connect(registry_path) as dst:
            self.c.backup(dst)
        before = fingerprint(registry_path)
        report = workload.inspect(self.run, self.base, registry_path)
        self.assertIsNone(report['added'])
        self.assertIsNone(report['english_changed'])
        self.assertEqual(report['counts']['baseline_adopted'], 2)
        self.assertEqual(fingerprint(registry_path), before)
        compared = workload.inspect(self.run, self.base, registry_path, self.run)
        self.assertEqual(compared['added'], 0)
        self.assertEqual(compared['english_changed'], 0)

    def test_report_rejects_mutated_snapshot_raw_and_ignores_registry_coverage_by_key(self):
        seed_legacy(self.c, english='Draw two cards.')
        self.c.commit()
        path = self.root / 'registry.sqlite'
        with sqlite3.connect(path) as dst:
            self.c.backup(dst)
        result = workload.inspect(self.run, self.base, path)
        row = next(r for r in result['rows'] if (r['layer'], r['key']) == ('card', '42'))
        self.assertEqual(row['classification'], 'needs_review')
        with sqlite3.connect(self.official['client']) as db:
            db.execute("UPDATE Loc SET enUS='Changed'")
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            workload.inspect(self.run, self.base)

    def test_configured_missing_baseline_never_silently_disables(self):
        with patch.dict('os.environ', {'MTGA_BASELINE_ROOT': str(self.root / 'missing'),
                                      'MTGA_BASELINE_VERSION': 'fixture'}):
            with self.assertRaises(OSError):
                b.configured('fixture')

    def test_extract_never_rebinds_stale_community_chinese_to_new_official_english(self):
        donor = self.root / 'community-client.sqlite'
        with sqlite3.connect(donor) as db:
            db.execute('CREATE TABLE Loc(Key TEXT PRIMARY KEY,enUS TEXT,jaJP TEXT)')
            db.execute('INSERT INTO Loc VALUES (?,?,?)',
                       ('Fixture/Body', 'Draw two cards.', '抓两张牌。'))
        output = self.root / 'extracted'
        generate(output, 'fixture', self.run, community_client=donor,
                 community_card=self.official['card'])
        extracted = b.Baseline(output, 'fixture')
        self.assertEqual(extracted.rows['client']['Fixture/Body']['en'], 'Draw two cards.')
        self.assertEqual(extracted.check('client', 'Fixture/Body',
                                        self.ctx['client', 'Fixture/Body']),
                         (None, 'baseline-english-changed'))

    def test_report_cannot_count_verified_text_with_missing_source_as_valid_coverage(self):
        seed_legacy(self.c)
        self.c.commit()
        path = self.root / 'missing-evidence-registry.sqlite'
        with sqlite3.connect(path) as dst:
            self.c.backup(dst)
        result = workload.inspect(self.run, self.base, path)
        self.assertEqual(result['counts']['reviewed_override'], 0)
        self.assertEqual(result['source_input_failures']['accepted'], 'source-evidence-missing-or-changed')
        row = next(r for r in result['rows'] if (r['layer'], r['key']) == ('card', '42'))
        self.assertEqual(row['classification'], 'needs_review')
        self.assertEqual(row['reason'], 'source-evidence-missing-or-changed')

    def test_baseline_passes_actual_raw_writer_and_preserves_other_tables(self):
        from raw import build_localization
        self.activate()
        for layer in ('client', 'card'):
            result = build_localization(self.official[layer], self.root / f'{layer}-candidate.sqlite',
                                        layer, self.c, 'fixture_raw')
            self.assertEqual(result['validation']['status'], 'PASS')
            self.assertEqual(result['counts']['baseline-adopted'], 1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM localization').fetchone()[0], 0)

    def test_extraction_rejects_donor_card_identity_drift_even_when_english_matches(self):
        donor = self.root / 'community-card.sqlite'
        shutil.copyfile(self.official['card'], donor)
        with sqlite3.connect(donor) as db:
            db.execute("UPDATE Cards SET ExpansionCode='DIFFERENT'")
            db.execute('INSERT INTO Localizations_jaJP VALUES (42,1,?)', ('抓一张牌。',))
        output = self.root / 'changed-donor-identity'
        generate(output, 'fixture', self.run, community_client=self.official['client'],
                 community_card=donor)
        extracted = b.Baseline(output, 'fixture')
        self.assertEqual(extracted.manifest['binding_basis'], 'donor-and-official-context-match')
        self.assertEqual(extracted.check('card', '42', self.ctx['card', '42']),
                         (None, 'baseline-identity-or-use-changed'))

    def test_preflight_replays_same_baseline_selection_without_changing_historical_decisions(self):
        from preflight import replay
        self.decision()
        self.c.commit()
        historical = list(self.c.execute('SELECT * FROM decisions'))
        corrections = self.root / 'reviewed-corrections'
        corrections.mkdir()
        report = replay(self.c, self.official, 'fixture_run', 'fixture', corrections, self.base)
        row = next(r for r in report['records'] if (r['layer'], r['key']) == ('card', '42'))
        self.assertEqual(row['status'], 'baseline-adopted')
        self.assertEqual(list(self.c.execute('SELECT * FROM decisions')), historical)
        self.assertEqual(report['baseline_adopted'], 2)

    def test_donor_mutation_during_context_read_is_rejected(self):
        donor = self.root / 'donor.sqlite'
        shutil.copyfile(self.official['card'], donor)
        real_contexts = contexts
        calls = []
        def mutate_after_read(official):
            result = real_contexts(official)
            calls.append(official)
            if official['card'] == donor:
                with sqlite3.connect(donor) as db:
                    db.execute("UPDATE Cards SET ExpansionCode='CHANGED'")
            return result
        with patch('build_baseline.contexts', side_effect=mutate_after_read):
            with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
                generate(self.root / 'drifting-donor', 'fixture', self.run,
                         community_client=self.official['client'], community_card=donor)
        self.assertFalse((self.root / 'drifting-donor/baseline').exists())

    def test_preflight_rechecks_past_baseline_adoption_for_identity_only_drift_or_disabled_baseline(self):
        from preflight import replay
        self.activate()
        self.decision()
        self.c.commit()
        corrections = self.root / 'reviewed-corrections'
        corrections.mkdir()
        without_baseline = replay(self.c, self.official, 'fixture_run', 'fixture', corrections)
        row = next(r for r in without_baseline['records'] if (r['layer'], r['key']) == ('card', '42'))
        self.assertEqual(row['status'], 'needs-review')
        with sqlite3.connect(self.official['card']) as db:
            db.execute("UPDATE Cards SET ExpansionCode='NEW'")
        changed = replay(self.c, self.official, 'fixture_run', 'fixture', corrections, self.base)
        row = next(r for r in changed['records'] if (r['layer'], r['key']) == ('card', '42'))
        self.assertEqual(row['status'], 'needs-review')


if __name__ == '__main__':
    unittest.main()
