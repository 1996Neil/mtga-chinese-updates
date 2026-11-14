"""Routine text policy is structural and machine-bound, never runtime approval."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

import publisher
import text_release_policy as policy
REAL_NATIVE_BASELINE = policy.native_baseline


class RoutineTextReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.policy = {'schema': 'MTGA_CN_RELEASE_POLICY_V1',
                       'owner_authorization': 'User authorized routine text publication without new game tests',
                       'routine_text_updates': dict(policy.POLICY_FLAGS)}
        self.policy_path = self.root / 'release-policy.json'
        self.policy_path.write_text(json.dumps(self.policy))
        self.manifest = self.root / 'baseline.json'
        self.manifest.write_text('immutable signed envelope fixture')
        self.public_key = json.loads((Path(policy.__file__).parent / 'profile.json').read_text())['public_key']
        self.public = self.root / 'public'
        self.public.write_text(self.public_key)
        self.client = self.root / 'native-client'
        self.client.write_text('fixture not executed')
        inputs = {layer: {'relative': layer + '.bin', 'sha256': 'a' * 64, 'bytes': 10}
                  for layer in publisher.LAYERS}
        outputs = {layer: {'relative': layer + '.bin', 'sha256': 'b' * 64, 'bytes': 20}
                   for layer in publisher.LAYERS}
        self.subject = {'client_version': '2026.65.0', 'steam_build': '26149138',
                        'platform': 'macOS-Steam', 'input_files': inputs, 'output_files': outputs,
                        'protected_files': {'executable': {'relative': 'app/executable',
                                                          'sha256': 'c' * 64, 'bytes': 30}},
                        'manifests': {'Manifest.mtga': {'sha256': 'd' * 64, 'bytes': 40}},
                        'builder_code': {}}
        raw_validation = {'status': 'PASS', 'integrity': 'ok', 'schema_and_indexes_preserved': True,
                          'all_tables_expected_rows': {'table': True}}
        self.report = {'localization': {'static_status': 'PASS',
                          'card': {'validation': copy.deepcopy(raw_validation)},
                          'client': {'validation': copy.deepcopy(raw_validation)},
                          'resources': {'validation': {
                              'status': 'PASS', 'UI_keys_order_other_languages_preserved': True,
                              'other_TMP_fields_preserved': True}},
                          'fonts': {'status': 'PASS', 'official_manifest_modified': False,
                                    'missing_glyphs': {'Light': [], 'Medium': []},
                                    'bootstrap_validation': {'status': 'PASS', 'all_other_objects_preserved': True},
                                    'bundle_validation': {'status': 'PASS', 'all_other_objects_preserved': True}}},
                       'import_compatibility': {'static_status': 'PASS', 'names': {
                           'new_card_groups': [], 'changed_card_identities': [],
                           'changed_canonical_names': [], 'canonical_conflicts': [], 'uncovered': [],
                           'validation': {'status': 'PASS', 'canonical_binding_validation': {
                               'status': 'PASS', 'all_primary_non_token_F0_bound_to_official_titles': True,
                               'all_display_F1_preserved': True, 'token_only_secondary_only_titles_not_added': True,
                               'canonical_exact_cross_title_collisions': 0}}},
                           'fixtures': {'status': 'PASS', 'new_importable_cards': 0,
                                        'fixtures': {name: {'static_status': 'PASS', 'cards': 60, 'errors': []}
                                                     for name in ('english-60', 'chinese-60', 'mixed-60',
                                                                  'qualified-60', 'special-english-60')}}},
                       'static_safe_to_trial': True,
                       'high_risk_unknown': [], 'candidate_files': outputs, 'code_fingerprints': {}}
        self.review = {'status': 'PASS', 'candidate_files': outputs, 'code_fingerprints': {}}
        self.baseline = {'schema': 'MTGA_CN_RELEASE_V1', 'id': '2026.65.0-26149138-r1',
                         'client_version': '2026.65.0', 'steam_build': '26149138',
                         'platform': 'macOS-Steam', 'acceptance': {'status': 'accepted'},
                         'architectures': ['arm64'], 'resources': [],
                         'protected_files': [
                             {'relative': 'app/executable', 'fingerprint': {'sha256': 'c' * 64, 'bytes': 30}},
                             {'relative': 'Manifest.mtga', 'fingerprint': {'sha256': 'd' * 64, 'bytes': 40}}]}
        for layer in publisher.LAYERS:
            self.baseline['resources'].append({
                'layer': layer, 'relative': layer + '.bin',
                'input': {'sha256': 'a' * 64, 'bytes': 10},
                'output': {'sha256': ('b' if layer in {'bootstrap_fonts', 'font_bundle'} else 'e') * 64,
                           'bytes': 20},
            })
        verifier = patch.object(policy, 'native_baseline', side_effect=lambda *args: copy.deepcopy(self.baseline))
        verifier.start()
        self.addCleanup(verifier.stop)

    def blockers(self):
        return policy.evaluate(self.subject, self.report, self.review, self.baseline, self.policy,
                               policy.toolchain_for(self.subject, self.client))

    def create(self):
        return policy.create(self.subject, self.report, self.review, self.manifest,
                             self.client, self.public, self.policy_path)

    def validate(self, evidence):
        return policy.validate(evidence, self.subject, self.report, self.review,
                               self.client, self.public_key, self.policy_path)

    def test_text_only_changes_allow_policy_scope_without_any_runtime_or_human_pass(self):
        self.assertEqual(self.blockers(), [])
        evidence = self.create()
        self.assertEqual(self.validate(evidence), ['arm64'])
        self.assertFalse(evidence['human_acceptance'])
        self.assertFalse(evidence['runtime_acceptance'])
        self.assertEqual(evidence['runtime_validation'], 'NOT_RUN')
        self.assertNotIn('checks', evidence)
        self.assertNotIn('tester_quote', evidence)
        self.assertIn('未执行新的真实游戏验收', evidence['scope'])

    def test_game_or_build_mismatch_blocks_the_new_initial_game_release(self):
        self.baseline.update(client_version='2026.64.50', steam_build='25919252')
        blockers = self.blockers()
        self.assertIn('baseline-client_version-mismatch', blockers)
        self.assertIn('baseline-steam_build-mismatch', blockers)
        with self.assertRaisesRegex(ValueError, '不适用'):
            self.create()

    def test_changed_official_paths_inputs_protection_and_fonts_all_block(self):
        self.subject['input_files']['card']['sha256'] = 'f' * 64
        self.subject['output_files']['client']['relative'] = 'different-resource.bin'
        self.subject['protected_files']['executable']['bytes'] += 1
        self.subject['output_files']['bootstrap_fonts']['sha256'] = 'f' * 64
        self.subject['output_files']['font_bundle']['bytes'] += 1
        blockers = self.blockers()
        for expected in ('official-input-or-resource-path-changed:card',
                         'official-input-or-resource-path-changed:client',
                         'protected-file-or-manifest-changed', 'font-output-changed:bootstrap_fonts',
                         'font-output-changed:font_bundle'):
            self.assertIn(expected, blockers)

    def test_new_cards_identity_canonical_or_unknown_import_structure_are_not_text_only(self):
        names = self.report['import_compatibility']['names']
        for field in ('changed_card_identities', 'changed_canonical_names',
                      'canonical_conflicts', 'uncovered'):
            before = names[field]
            names[field] = [1]
            self.assertIn('name-import-structure-changed-or-unknown:' + field, self.blockers())
            names[field] = before
        del names['changed_card_identities']
        self.assertIn('name-import-structure-changed-or-unknown:changed_card_identities', self.blockers())

    def test_high_risk_static_failure_and_mismatched_independent_review_block(self):
        self.report['high_risk_unknown'] = ['unknown']
        self.report['localization']['static_status'] = 'FAIL'
        self.review['status'] = 'PENDING'
        blockers = self.blockers()
        self.assertIn('candidate-high-risk-unknown', blockers)
        self.assertIn('candidate-static-validation-not-pass', blockers)
        self.assertIn('independent-engineering-review-mismatch', blockers)

    def test_evidence_cannot_invent_pass_or_bypass_recomputed_machine_decision(self):
        original = self.create()
        for field, value in (('runtime_acceptance', True), ('human_acceptance', True),
                             ('runtime_validation', 'PASS'), ('subject_sha256', '0' * 64)):
            with self.assertRaises(ValueError):
                self.validate(original | {field: value})
        self.subject['output_files']['font_bundle']['sha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, '不适用'):
            self.validate(original)

    def test_mutated_policy_baseline_report_or_review_invalidates_evidence(self):
        evidence = self.create()
        self.report['additional_machine_input'] = 'changed'
        with self.assertRaisesRegex(ValueError, '过期'):
            self.validate(evidence)
        del self.report['additional_machine_input']
        self.manifest.write_text('different signed envelope fixture')
        with self.assertRaisesRegex(ValueError, '过期'):
            self.validate(evidence)
        self.policy['routine_text_updates']['requires_unchanged_fonts'] = False
        self.policy_path.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError, '政策'):
            self.create()

    def test_manual_acceptance_validator_still_requires_actual_complete_original_checks(self):
        subject_hash = hashlib.sha256(publisher.canonical(self.subject)).hexdigest()
        manual = {'schema': 'MTGA_CN_PUBLISHER_ACCEPTANCE_V1', 'subject_sha256': subject_hash,
                  'checks': dict.fromkeys(publisher.CHECKS, True), 'tester_quote': 'actual fixture test',
                  'scope': 'actual fixture scope', 'architectures': ['arm64']}
        self.assertEqual(publisher.validate_acceptance(manual, self.subject), ['arm64'])
        manual['checks']['restart_repeat'] = False
        with self.assertRaisesRegex(ValueError, '全部通过'):
            publisher.validate_acceptance(manual, self.subject)
        with self.assertRaisesRegex(ValueError, '格式'):
            publisher.validate_acceptance(self.create(), self.subject)

    def test_native_baseline_uses_fixed_key_and_requires_successful_signature(self):
        with patch.object(policy.subprocess, 'run') as process:
            self.public.write_text('different-key')
            with self.assertRaisesRegex(ValueError, '公钥'):
                REAL_NATIVE_BASELINE(self.manifest, self.client, self.public)
            self.assertEqual(process.call_count, 0)
            self.public.write_text(self.public_key)
            process.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, '签名'):
                REAL_NATIVE_BASELINE(self.manifest, self.client, self.public)
        with self.assertRaisesRegex(ValueError, '公钥'):
            policy.validate(self.create(), self.subject, self.report, self.review,
                            self.client, 'different-key', self.policy_path)

    def test_missing_nested_machine_checks_and_unknown_risk_are_rejected(self):
        for mutator in (
                lambda r: r.pop('static_safe_to_trial'),
                lambda r: r.pop('high_risk_unknown'),
                lambda r: r['localization']['card'].pop('validation'),
                lambda r: r['localization']['resources']['validation'].pop('other_TMP_fields_preserved'),
                lambda r: r['localization']['fonts']['missing_glyphs'].update(Light=[123]),
                lambda r: r['import_compatibility']['names'].pop('validation'),
                lambda r: r['import_compatibility']['fixtures'].update(new_importable_cards=-1)):
            original = copy.deepcopy(self.report)
            mutator(self.report)
            self.assertTrue(self.blockers())
            self.report = original

    def test_old_portable_dataset_new_card_counts_do_not_mean_new_against_signed_stable(self):
        self.report['import_compatibility']['names']['new_card_groups'] = list(range(544))
        self.report['import_compatibility']['fixtures']['new_importable_cards'] = 408
        self.assertEqual(self.blockers(), [])
        self.subject['input_files']['card']['sha256'] = 'f' * 64
        self.assertIn('official-input-or-resource-path-changed:card', self.blockers())

    def lightweight(self):
        self.policy['routine_text_updates'] = dict(policy.LIGHTWEIGHT_POLICY_FLAGS)
        self.policy_path.write_text(json.dumps(self.policy))
        code = {'cli.py': {'sha256': 'f' * 64, 'bytes': 100}}
        self.subject['builder_code'] = copy.deepcopy(code)
        self.report.update(code_fingerprints=code, static_checks_passed=True,
                           static_safe_to_trial=False, safe_to_install=False)
        self.review = {}
        self.baseline['toolchain'] = policy.toolchain_for(self.subject, self.client)
        self.baseline['acceptance']['toolchain_review'] = 'PASS'

    def test_lightweight_reuses_only_signed_reviewed_toolchain_without_faking_review(self):
        self.lightweight()
        self.assertEqual(self.blockers(), [])
        evidence = self.create()
        self.assertEqual(self.validate(evidence), ['arm64'])
        self.assertEqual(evidence['independent_review'], 'REUSED_SIGNED_TOOLCHAIN')
        self.assertEqual(evidence['toolchain_review'], 'REUSED_SIGNED_TOOLCHAIN')
        self.assertFalse(evidence['independent_review_rerun'])
        self.assertFalse(evidence['runtime_acceptance'])
        self.assertFalse(self.report['safe_to_install'])
        self.assertEqual(self.review, {})
        self.assertIn('未重跑独立工程审查', evidence['scope'])
        self.baseline['acceptance']['toolchain_review'] = 'REUSED_SIGNED_TOOLCHAIN'
        self.assertEqual(self.blockers(), [])

    def test_lightweight_legacy_or_unreviewed_baseline_needs_full_toolchain_review(self):
        self.lightweight()
        for status in ('NOT_RECORDED', 'NOT_RERUN', None):
            self.baseline['acceptance']['toolchain_review'] = status
            self.assertIn('independent-engineering-review-mismatch', self.blockers())
        self.baseline.pop('toolchain')
        self.review = {'status': 'PASS', 'candidate_files': self.report['candidate_files'],
                       'code_fingerprints': self.report['code_fingerprints']}
        # A legacy builder-only review cannot attest new publisher/client code.
        self.assertIn('independent-engineering-review-mismatch', self.blockers())
        self.review['toolchain'] = policy.toolchain_for(self.subject, self.client)
        evidence = self.create()
        self.assertEqual(evidence['independent_review'], 'PASS')
        self.assertEqual(evidence['toolchain_review'], 'PASS')
        self.assertTrue(evidence['independent_review_rerun'])

    def test_lightweight_toolchain_changes_and_empty_builder_never_reuse_review(self):
        self.lightweight()
        original = copy.deepcopy(self.subject['builder_code'])
        for code in ({}, {'cli.py': {'sha256': '0' * 64, 'bytes': 100}}):
            self.subject['builder_code'] = code
            self.report['code_fingerprints'] = code
            self.assertIn('independent-engineering-review-mismatch', self.blockers())
        self.subject['builder_code'] = original
        self.report['code_fingerprints'] = original
        self.client.write_text('different client bytes')
        self.assertIn('independent-engineering-review-mismatch', self.blockers())
        self.baseline['toolchain'] = policy.toolchain_for(self.subject, self.client)
        self.baseline['toolchain']['publisher_code']['publisher.py']['sha256'] = '0' * 64
        self.assertIn('independent-engineering-review-mismatch', self.blockers())

    def test_lightweight_cannot_skip_structural_identity_font_or_static_checks(self):
        self.lightweight()
        self.report['static_checks_passed'] = False
        self.assertIn('candidate-not-statically-safe', self.blockers())
        self.report['static_checks_passed'] = True
        self.subject['input_files']['card']['bytes'] += 1
        self.subject['output_files']['font_bundle']['bytes'] += 1
        self.report['import_compatibility']['names']['changed_canonical_names'] = [123]
        blockers = self.blockers()
        self.assertIn('official-input-or-resource-path-changed:card', blockers)
        self.assertIn('font-output-changed:font_bundle', blockers)
        self.assertIn('name-import-structure-changed-or-unknown:changed_canonical_names', blockers)

    def test_lightweight_evidence_cannot_claim_fresh_review(self):
        self.lightweight()
        evidence = self.create()
        for changes in ({'independent_review_rerun': True}, {'toolchain_review': 'PASS'},
                        {'independent_review': 'PASS'}):
            with self.assertRaisesRegex(ValueError, '过期'):
                self.validate(evidence | changes)

    def test_portable_toolchain_does_not_export_absolute_checkout_paths(self):
        root = Path(policy.__file__).resolve().parents[2]
        self.subject['builder_code'] = {
            str(root / 'bin/mtga-cn'): {'sha256': 'a' * 64, 'bytes': 10},
            'cli.py': {'sha256': 'b' * 64, 'bytes': 20}}
        result = policy.toolchain_for(self.subject, self.client)
        self.assertNotIn(str(root), json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(result['builder_sha256'], hashlib.sha256(publisher.canonical({
            'bin/mtga-cn': {'sha256': 'a' * 64, 'bytes': 10},
            'tools/chinese-maintainer/cli.py': {'sha256': 'b' * 64, 'bytes': 20},
        })).hexdigest())

    def test_owner_explicitly_allows_text_updates_without_engineering_review(self):
        self.policy['routine_text_updates']['requires_independent_review'] = False
        self.policy_path.write_text(json.dumps(self.policy))
        self.report['static_checks_passed'] = True
        self.review = {}
        evidence = self.create()
        self.assertEqual(evidence['independent_review'], 'NOT_REQUIRED_BY_OWNER_POLICY')
        self.assertFalse(evidence['independent_review_rerun'])
        self.assertEqual(evidence['toolchain_review'], 'NOT_RECORDED')
        self.assertFalse(evidence['runtime_acceptance'])

    def publisher_fixture(self):
        run = self.root / 'candidate'
        run.mkdir()
        report = copy.deepcopy(self.report)
        snapshot = {'client_version': '2026.65.0', 'steam_build': '26149138',
                    'data_versions': {}, 'files': {}, 'manifests': {}}
        meta = {'layers': []}
        for layer in publisher.LAYERS:
            original = run / 'official' / (layer + '.bin')
            original.parent.mkdir(exist_ok=True)
            original.write_text('original-' + layer)
            output = run / (layer + '.bin')
            output.write_text('candidate-' + layer)
            snapshot['files'][layer] = {'relative': original.name, **publisher.fingerprint(original)}
            report['candidate_files'][layer] = {
                'relative': original.name, 'path': str(output), **publisher.fingerprint(output)}
            meta['layers'].append({'layer': layer, 'relative': original.name,
                                   'fingerprint': publisher.fingerprint(original)})
        report['schema'] = 'MTGA_CN_MAINTAINER_V1'
        for name, value in [('snapshot.json', snapshot), ('report.json', report),
                            ('official-base.json', meta),
                            ('acceptance.json', {'schema': policy.SCHEMA})]:
            (run / name).write_text(json.dumps(value))
        return run, snapshot, report, meta

    def test_direct_automatic_packaging_runs_actual_structural_guard_before_signing(self):
        run, _, _, _ = self.publisher_fixture()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'chinese-maintainer'))
        import cli
        destination = self.root / 'must-not-sign'
        with patch.object(cli, 'validate_run', side_effect=RuntimeError('structural guard rejected')) as guard, \
             patch.object(publisher, 'sign', side_effect=AssertionError('signer invoked')):
            with self.assertRaisesRegex(RuntimeError, 'structural guard'):
                publisher.package(run, destination, self.client, self.root / 'never-read-key',
                                  self.public_key, 2, 'stable', run / 'acceptance.json')
            self.assertEqual(guard.call_count, 1)
            self.assertFalse(destination.exists())

    def lightweight_package_fixture(self):
        self.lightweight()
        builder = Path(policy.__file__).resolve().parents[1] / 'chinese-maintainer/cli.py'
        self.report['code_fingerprints'] = {'cli.py': publisher.fingerprint(builder)}
        run, snapshot, report, meta = self.publisher_fixture()
        self.report = report
        self.subject = publisher.subject_for(snapshot, report, meta)
        self.baseline['protected_files'] = []
        self.baseline['resources'] = [
            {'layer': layer, 'relative': row['relative'],
             'input': {k: row[k] for k in ('sha256', 'bytes')},
             'output': {k: self.subject['output_files'][layer][k] for k in ('sha256', 'bytes')}}
            for layer, row in self.subject['input_files'].items()]
        self.baseline['toolchain'] = policy.toolchain_for(self.subject, self.client)
        evidence = self.create()
        (run / 'acceptance.json').write_text(json.dumps(evidence))
        return run

    def test_direct_lightweight_package_preserves_review_reuse_in_manifest(self):
        run = self.lightweight_package_fixture()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'chinese-maintainer'))
        import cli

        def synthetic_sign(client, key, value, destination, scratch):
            publisher.dump(destination, value)

        with patch.object(cli, 'validate_run'), \
             patch.object(policy, 'POLICY_PATH', self.policy_path), \
             patch.object(publisher, 'sign', side_effect=synthetic_sign):
            result = publisher.package(run, self.root / 'package', self.client,
                                       self.root / 'never-read-key', self.public_key,
                                       2, 'stable', run / 'acceptance.json')
        self.assertEqual(result['acceptance']['independent_review'], 'REUSED_SIGNED_TOOLCHAIN')
        self.assertEqual(result['acceptance']['toolchain_review'], 'REUSED_SIGNED_TOOLCHAIN')
        self.assertFalse(result['acceptance']['independent_review_rerun'])
        self.assertEqual(result['acceptance']['runtime_validation'], 'NOT_RUN')
        self.assertEqual(result['toolchain'], self.baseline['toolchain'])
        self.assertFalse((run / 'independent-review.json').exists())

    def test_toolchain_drift_during_delta_build_stops_before_any_signing(self):
        run = self.lightweight_package_fixture()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'chinese-maintainer'))
        import cli
        make_delta = publisher.make_delta

        def changed_client(*args):
            result = make_delta(*args)
            self.client.write_text('client changed while building deltas')
            return result

        with patch.object(cli, 'validate_run'), \
             patch.object(policy, 'POLICY_PATH', self.policy_path), \
             patch.object(publisher, 'make_delta', side_effect=changed_client), \
             patch.object(publisher, 'sign') as sign:
            with self.assertRaisesRegex(ValueError, '工具链发生变化'):
                publisher.package(run, self.root / 'changed-package', self.client,
                                  self.root / 'never-read-key', self.public_key,
                                  2, 'stable', run / 'acceptance.json')
        sign.assert_not_called()


if __name__ == '__main__':
    unittest.main()
