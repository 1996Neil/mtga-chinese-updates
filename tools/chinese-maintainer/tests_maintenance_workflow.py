"""One-key orchestration regression tests; no real game, key, network or publish."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import common
import maintenance_workflow as workflow
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'subscriber'))


class MaintenanceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.state_patch = patch.object(common, 'STATE', self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.snapshot = {'client_version': '2026.65.0', 'steam_build': '26149138',
                         'data_versions': {}, 'files': {}, 'manifests': {},
                         'official_entries': {}}
        self.config = {'schema': 'MTGA_CN_MAINTENANCE_CONFIG_V1',
                       'output_root': str(self.root / 'work')}
        self.patchers = [
            patch.object(workflow, 'read_snapshot', return_value=self.snapshot),
            patch.object(workflow, 'source_preflight', return_value=[]),
            patch.object(workflow, 'diagnose', return_value={'safe_to_rebuild': True}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, prepare=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return workflow.execute(self.config, prepare)

    def result(self):
        paths = list((self.root / 'work').glob('*/*/maintenance.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def ready(self):
        run = self.root / 'candidate'
        run.mkdir()
        self.report = {'client_version': self.snapshot['client_version'],
                       'safe_to_install': True, 'blockers': []}
        for name, value in [('snapshot.json', self.snapshot), ('report.json', self.report),
                            ('official-base.json', {})]:
            (run / name).write_text(json.dumps(value))
        self.config['run'] = str(run)
        for field in ('client', 'key_file', 'public_key_file', 'acceptance', 'installer'):
            path = self.root / field
            path.write_text('{}')
            self.config[field] = str(path)
        import cli
        import publisher
        patches = [
            patch.object(cli, 'validate_run', side_effect=lambda r, report: report),
            patch.object(publisher, 'validate_acceptance', return_value=['arm64']),
            patch.object(publisher, 'subject_for', return_value={'input_files': {}, 'output_files': {}}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return run

    def test_missing_base_collects_once_and_stops_without_build_sign_or_install(self):
        with patch.object(workflow, 'diagnose', return_value={'safe_to_rebuild': False}), \
             patch.object(workflow, 'stage_capture') as capture, \
             patch.object(workflow, 'invoke', side_effect=AssertionError('process invoked')):
            self.assertEqual(self.execute(), 2)
            self.assertEqual(capture.call_count, 1)
        result = self.result()
        self.assertEqual(result['stage'], 'base_review')
        self.assertFalse(result['completed'])

    def test_missing_sources_are_aggregated_before_build(self):
        failures = [{'source_id': 'a', 'path': '/gone/a', 'reason': 'missing'},
                    {'source_id': 'b', 'path': '/gone/b', 'reason': 'changed'}]
        with patch.object(workflow, 'source_preflight', return_value=failures), \
             patch.object(workflow, 'invoke', side_effect=AssertionError('process invoked')):
            self.assertEqual(self.execute(), 2)
        self.assertEqual(self.result()['stage'], 'source_inputs')
        paths = list((self.root / 'work').glob('*/*/blocked-inputs.json'))
        self.assertEqual(json.loads(paths[0].read_text())['sources'], failures)

    def test_verified_base_with_lost_rollback_inputs_is_not_recaptured(self):
        base = {'safe_to_rebuild': False, 'errors': ['Rollback backup verification failed'],
                'layers': [{'status': 'verified'} for _ in range(5)]}
        with patch.object(workflow, 'diagnose', return_value=base), \
             patch.object(workflow, 'stage_capture', side_effect=AssertionError('duplicate capture')):
            self.assertEqual(self.execute(), 2)
        self.assertEqual(self.result()['stage'], 'blocked_inputs')
        self.assertIn('Rollback backup verification failed', self.result()['blockers'])

    def test_unreviewed_candidate_never_reaches_network_or_signer(self):
        self.ready()
        report = self.report | {'safe_to_install': False, 'blockers': ['独立审查待完成']}
        (Path(self.config['run']) / 'report.json').write_text(json.dumps(report))
        with patch.object(workflow, 'invoke', side_effect=AssertionError('process invoked')):
            self.assertEqual(self.execute(), 2)
        self.assertEqual(self.result()['stage'], 'review_acceptance')

    def test_prepublication_native_network_evidence_cannot_be_skipped(self):
        self.ready()
        with patch.object(workflow, 'remote_state', side_effect=AssertionError('remote invoked')), \
             patch.object(workflow, 'invoke', side_effect=AssertionError('process invoked')):
            self.assertEqual(self.execute(), 2)
        self.assertIn('HTTPS', self.result()['blockers'][0])

    def test_prepare_only_never_reports_complete_or_publishes(self):
        self.ready()
        with patch.object(workflow, 'network_gate', return_value={'sha256': 'fixture'}), \
             patch.object(workflow, 'invoke', side_effect=AssertionError('process invoked')):
            self.assertEqual(self.execute(prepare=True), 0)
        self.assertEqual(self.result()['stage'], 'ready_for_release')
        self.assertFalse(self.result()['completed'])
        timing = json.loads(next((self.root / 'work').glob('*/*/timing.json')).read_text())
        self.assertEqual(timing['mode'], 'prepare-only')
        self.assertFalse(timing['completion_target_met'])
        self.assertFalse(timing['completed'])

    def test_fast_blocked_run_records_time_without_claiming_completion(self):
        with patch.object(workflow, 'source_preflight', return_value=[{'reason': 'missing'}]):
            self.assertEqual(self.execute(), 2)
        timing = json.loads(next((self.root / 'work').glob('*/*/timing.json')).read_text())
        self.assertTrue(timing['within_time_budget'])
        self.assertFalse(timing['completion_target_met'])
        self.assertFalse(timing['completed'])
        self.assertIn('input_checks', [row['stage'] for row in timing['stages']])

    def test_online_preparation_failure_has_timing_without_release_directory(self):
        config = self.root / 'configuration/maintenance.json'
        with patch('online_sources.prepare_online', side_effect=RuntimeError('network unavailable')), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'network unavailable'):
                workflow.main(['--config', str(config), '--online', '--prepare-only'])
        timing = json.loads(config.with_name('last-attempt-timing.json').read_text())
        self.assertEqual(timing['failed_stage'], 'online_preparation')
        self.assertFalse(timing['completed'])
        self.assertFalse(timing['completion_target_met'])
        self.assertFalse((self.root / 'work').exists())

    def test_early_snapshot_failure_has_timing_without_release_directory(self):
        config = self.root / 'configuration/maintenance.json'
        with patch('online_sources.prepare_online', return_value=self.config), \
             patch.object(workflow, 'read_snapshot', side_effect=RuntimeError('game missing')), \
             patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'game missing'):
                workflow.main(['--config', str(config), '--online', '--prepare-only'])
        timing = json.loads(config.with_name('last-attempt-timing.json').read_text())
        self.assertEqual(timing['failed_stage'], 'input_checks')
        self.assertFalse(timing['completion_target_met'])

    def test_lightweight_policy_can_run_from_structural_only_candidate_without_review_file(self):
        run = self.ready()
        Path(self.config['acceptance']).unlink()
        self.report.update(safe_to_install=False, static_safe_to_trial=False,
                           static_checks_passed=True, blockers=['独立工程审查未通过或证据已过期'])
        (run / 'report.json').write_text(json.dumps(self.report))
        evidence = self.root / 'automatic.json'
        evidence.write_text('{}')
        with patch.object(workflow, 'automatic_text_acceptance',
                          return_value=(evidence, {}, [])) as create, \
             patch('text_release_policy.validate', return_value=['arm64']) as validate, \
             patch.object(workflow, 'invoke', side_effect=AssertionError('unexpected publication')):
            self.assertEqual(self.execute(prepare=True), 0)
        create.assert_called_once()
        self.assertEqual(validate.call_args.args[3], {})
        self.assertFalse((run / 'independent-review.json').exists())

    def test_routine_policy_does_not_claim_runtime_pass_or_require_manual_file(self):
        run = self.ready()
        Path(self.config['acceptance']).unlink()
        self.report.update(safe_to_install=False, static_safe_to_trial=True,
                           blockers=['当前候选的中文显示与原生英/中/混合导入待验收'])
        (run / 'report.json').write_text(json.dumps(self.report))
        (run / 'independent-review.json').write_text('{}')
        evidence = self.root / 'automatic.json'
        evidence.write_text('{"schema":"MTGA_CN_AUTOMATIC_TEXT_RELEASE_V1"}')
        with patch.object(workflow, 'automatic_text_acceptance',
                          return_value=(evidence, {}, [])), \
             patch('text_release_policy.validate', return_value=['arm64']), \
             patch.object(workflow, 'network_gate', side_effect=AssertionError('manual evidence requested')), \
             patch.object(workflow, 'invoke', side_effect=AssertionError('unexpected publication')):
            self.assertEqual(self.execute(prepare=True), 0)
        self.assertEqual(self.result()['stage'], 'ready_for_release')
        self.assertFalse(self.result()['completed'])
        self.assertFalse(json.loads((run / 'report.json').read_text())['safe_to_install'])

    def test_routine_resume_retains_original_baseline_instead_of_remaking_approval(self):
        run = self.ready()
        work = self.root / 'routine'
        work.mkdir()
        original = '{"schema":"MTGA_CN_AUTOMATIC_TEXT_RELEASE_V1","baseline_manifest":{"path":"/old"}}'
        approval = work / 'automatic-text-acceptance.json'
        approval.write_text(original)
        (run / 'independent-review.json').write_text('{}')
        index = {'schema': 'MTGA_CN_INDEX_V1', 'channel': 'stable', 'releases': [],
                 'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()}
        subject = {'client_version': '2026.65.0', 'steam_build': '26149138'}
        with patch('text_release_policy.validate', return_value=['arm64']) as validate, \
             patch('text_release_policy.create', side_effect=AssertionError('approval remade')), \
             patch.object(workflow, 'remote_state', return_value=(index, [])):
            path, _, _ = workflow.automatic_text_acceptance(
                self.config, work, run, subject, self.report)
        self.assertEqual(path, approval)
        self.assertEqual(approval.read_text(), original)
        validate.assert_called_once()

    def test_new_revision_counts_drafts_and_existing_signed_refs(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        index = {'schema': 'MTGA_CN_INDEX_V1', 'channel': 'stable', 'expires_at': future,
                 'releases': [{'client_version': '2026.65.0', 'steam_build': '26149138',
                               'revision': 2}]}
        releases = [{'tag_name': 'mtga-2026.65.0-26149138-r5', 'draft': True},
                    {'tag_name': 'mtga-2026.64.50-25919252-r99'}]
        self.assertEqual(workflow.next_revision(index, releases, '2026.65.0', '26149138'), 6)
        index['expires_at'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(RuntimeError, '过期'):
            workflow.next_revision(index, releases, '2026.65.0', '26149138')

    def test_verified_pipeline_uses_target_repo_then_syncs_monitor_and_verifies(self):
        self.ready()
        self.config['repo'] = 'fixture/updates'
        release_id = '2026.65.0-26149138-r1'
        plan = {'release': {'id': release_id, 'resources': []}}
        index = {'schema': 'MTGA_CN_INDEX_V1', 'channel': 'stable', 'releases': [],
                 'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()}
        calls = []

        def process(argv, log=None):
            calls.append([str(v) for v in argv])
            output = Path(argv[argv.index('--output') + 1])
            output.mkdir()
            receipt = Path(argv[argv.index('--receipt') + 1])
            receipt.write_text('{}')
            return ''

        with patch.object(workflow, 'network_gate', return_value={'sha256': 'fixture'}), \
             patch.object(workflow, 'remote_state', return_value=(index, [])), \
             patch.object(workflow, 'invoke', side_effect=process), \
             patch('github_publish.publication_plan', return_value=plan), \
             patch.object(workflow, 'sync_monitor') as monitor, \
             patch.object(workflow, 'verify_publication', return_value={'status': 'PASS'}) as verify:
            self.assertEqual(self.execute(), 0)
        self.assertTrue(self.result()['completed'])
        self.assertEqual(self.result()['stage'], 'complete')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index('--repo') + 1], 'fixture/updates')
        self.assertEqual(calls[0][calls[0].index('--channel') + 1], 'stable')
        self.assertIn('--publish', calls[0])
        self.assertNotIn('install', calls[0])
        self.assertEqual(monitor.call_count, 1)
        self.assertEqual(verify.call_count, 1)

    def test_changed_candidate_evidence_cannot_resume_existing_frozen_package(self):
        self.ready()
        index = {'schema': 'MTGA_CN_INDEX_V1', 'channel': 'stable', 'releases': [],
                 'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()}
        plan = {'release': {'id': '2026.65.0-26149138-r1', 'resources': []}}

        def process(argv, log=None):
            Path(argv[argv.index('--output') + 1]).mkdir()
            Path(argv[argv.index('--receipt') + 1]).write_text('{}')
            return ''

        with patch.object(workflow, 'network_gate', return_value={'sha256': 'fixture'}), \
             patch.object(workflow, 'remote_state', return_value=(index, [])), \
             patch.object(workflow, 'invoke', side_effect=process), \
             patch('github_publish.publication_plan', return_value=plan), \
             patch.object(workflow, 'sync_monitor'), \
             patch.object(workflow, 'verify_publication', return_value={'status': 'PASS'}):
            self.assertEqual(self.execute(), 0)
        Path(self.config['acceptance']).write_text('{"changed":true}')
        with patch.object(workflow, 'network_gate', return_value={'sha256': 'fixture'}), \
             patch.object(workflow, 'remote_state', return_value=(index, [])), \
             patch.object(workflow, 'invoke', side_effect=AssertionError('stale package published')):
            self.assertEqual(self.execute(), 2)
        self.assertFalse(self.result()['completed'])
        self.assertIn('冻结发布不同', self.result()['blockers'][0])

    def test_config_paths_are_explicit_and_shell_override_is_preserved(self):
        path = self.root / 'maintenance.json'
        value = self.config | {'state_root': str(self.state)}
        path.write_text(json.dumps(value))
        with patch.dict(os.environ, {'MTGA_STATE_ROOT': '/explicit-shell'}, clear=True):
            workflow.configure(workflow.load_config(path))
            self.assertEqual(os.environ['MTGA_STATE_ROOT'], '/explicit-shell')
        value['state_root'] = 'relative-path'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, '绝对路径'):
            workflow.load_config(path)


class RunTimingTests(unittest.TestCase):
    def test_preparation_and_phase_durations_are_counted_once(self):
        with patch.object(workflow.time, 'perf_counter', side_effect=[10, 12, 15]):
            timing = workflow.RunTiming(preparation_seconds=4)
            timing.switch('candidate')
            summary = timing.summary(completed=True, prepare_only=False)
        self.assertEqual(summary['total_seconds'], 9)
        self.assertEqual(summary['stages'], [
            {'stage': 'online_preparation', 'seconds': 4},
            {'stage': 'input_checks', 'seconds': 2},
            {'stage': 'candidate', 'seconds': 3}])
        self.assertTrue(summary['completion_target_met'])

    def test_slow_completion_and_fast_preparation_are_not_target_success(self):
        with patch.object(workflow.time, 'perf_counter', side_effect=[0, 301]):
            summary = workflow.RunTiming().summary(completed=True, prepare_only=False)
        self.assertFalse(summary['within_time_budget'])
        self.assertFalse(summary['completion_target_met'])
        with patch.object(workflow.time, 'perf_counter', side_effect=[0, 1]):
            summary = workflow.RunTiming().summary(completed=True, prepare_only=True)
        self.assertTrue(summary['within_time_budget'])
        self.assertFalse(summary['completion_target_met'])


class RegisteredSourcePreflightTests(unittest.TestCase):
    def test_original_registry_is_read_only_and_missing_changed_inputs_are_returned(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp).resolve()
            valid = state / 'valid'
            changed = state / 'changed'
            valid.write_text('valid')
            changed.write_text('changed')
            database = state / 'registry.sqlite'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE sources(id TEXT,path TEXT,sha256 TEXT,bytes INTEGER)')
                fp = common.fingerprint(valid)
                connection.executemany('INSERT INTO sources VALUES(?,?,?,?)', [
                    ('valid', str(valid), fp['sha256'], fp['bytes']),
                    ('changed', str(changed), fp['sha256'], fp['bytes']),
                    ('missing', str(state / 'missing'), fp['sha256'], fp['bytes']),
                ])
            before = common.fingerprint(database)
            with patch.object(common, 'STATE', state):
                result = workflow.source_preflight()
            self.assertEqual({r['source_id'] for r in result}, {'changed', 'missing'})
            self.assertEqual(common.fingerprint(database), before)
            self.assertFalse((state / 'missing').exists())


class NativePreviewCacheTests(unittest.TestCase):
    def test_cached_preview_requires_all_signed_layer_outputs_and_canonical_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            run = root / 'run'
            live = root / 'live'
            run.mkdir()
            live.mkdir()
            (root / 'maintenance.json').write_text(json.dumps({'run': str(run)}))
            (run / 'snapshot.json').write_text(json.dumps({'game': str(live)}))
            client = root / 'client'
            manifest = root / 'manifest'
            client.write_text('client')
            manifest.write_text('signed fixture')
            config = {'client': str(client)}
            plan = {'manifest': str(manifest), 'release': {'resources': [], 'protected_files': []}}
            reconstructed = {}
            live_files = {}
            for layer in ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle'):
                source = live / (layer + '.bin')
                source.write_text('original-' + layer)
                output = root / 'preview-state/transactions/fixture' / ('after-' + layer)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text('verified-' + layer)
                fp = common.fingerprint(output)
                plan['release']['resources'].append(
                    {'layer': layer, 'relative': source.name, 'output': fp})
                reconstructed[layer] = {'path': str(output), 'fingerprint': fp}
                live_files[source.name] = common.fingerprint(source)
            good = {'status': 'PASS', 'binding': {'manifest': common.fingerprint(manifest),
                                                'client': common.fingerprint(client)},
                    'live_files': live_files, 'reconstructed': reconstructed}
            cache = root / 'native-preview.json'
            cache.write_text(json.dumps(good))
            with patch.object(workflow, 'invoke', side_effect=AssertionError('network invoked')):
                self.assertTrue(workflow.native_preview(config, root, plan)['reused_verified_preview'])
                for changed in (good | {'reconstructed': {}},
                                good | {'status': 'PENDING'},
                                good | {'reconstructed': reconstructed | {
                                    'card': reconstructed['card'] | {'path': str(manifest)}}}):
                    cache.write_text(json.dumps(changed))
                    with self.assertRaisesRegex(RuntimeError, '预演缓存'):
                        workflow.native_preview(config, root, plan)


if __name__ == '__main__':
    unittest.main()
