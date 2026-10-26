"""Exercise orchestration boundaries with synthetic metadata and no real keys."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cli
import release_workflow as workflow


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / 'candidate'
        self.run.mkdir()
        self.report = {'client_version': 'fixture', 'localization': {
            'static_status': 'PASS', 'card': {'counts': {'applied': 2}},
            'client': {'counts': {'applied': 3}}}, 'import_compatibility': {'static_status': 'PASS'},
            'static_safe_to_trial': True, 'safe_to_install': False}
        (self.run / 'report.json').write_text(json.dumps(self.report))
        (self.run / 'snapshot.json').write_text(json.dumps({'steam_build': '1'}))
        self.public = self.root / 'public-key'
        self.public.write_text('synthetic-public-key')
        self.args = SimpleNamespace(run=self.run, version='fixture', baseline_root=None,
            baseline_version=None, output=self.root / 'package', revision=1, tag=None,
            channel='testing', repo='1996Neil/mtga-chinese-updates', client=self.root / 'client',
            key=self.root / 'DO-NOT-READ-private-key', public_key_file=self.public,
            acceptance=None, dry_run=True, publish=True, minimum_client=None,
            include_full=False, installer=None, receipt=None)
        self.calls = []
        self.validate = patch.object(cli, 'validate_run', side_effect=lambda p, r: r)
        self.validate.start()
        self.addCleanup(self.validate.stop)
        state = patch.object(workflow, 'STATE', self.root / 'state')
        state.start()
        self.addCleanup(state.stop)

    def tearDown(self):
        self.temp.cleanup()

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return workflow.execute(self.args, self.calls.append)

    def test_dry_run_never_signs_reads_private_key_or_publishes(self):
        self.assertFalse(self.args.key.exists())
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.args.output.exists())

    def test_stable_missing_runtime_or_acceptance_stops_before_signing(self):
        self.args.dry_run = False
        self.args.channel = 'stable'
        with self.assertRaisesRegex(RuntimeError, '验收'):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_missing_engineering_review_blocks_testing_too(self):
        self.report['static_safe_to_trial'] = False
        (self.run / 'report.json').write_text(json.dumps(self.report))
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.calls, [])

    def test_validation_failure_cannot_reach_signer(self):
        with patch.object(cli, 'validate_run', side_effect=RuntimeError('changed baseline')):
            with self.assertRaisesRegex(RuntimeError, 'changed baseline'):
                self.execute()
        self.assertEqual(self.calls, [])

    def test_existing_candidate_and_version_mismatch_stop(self):
        self.args.version = 'wrong'
        with self.assertRaisesRegex(RuntimeError, 'version'):
            self.execute()
        self.args.version = 'fixture'
        self.args.dry_run = False
        self.args.output.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_real_flow_reuses_publishers_in_order_and_network_is_opt_in(self):
        self.args.dry_run = False
        self.args.publish = False
        self.assertEqual(self.execute(), 0)
        self.assertEqual(Path(self.calls[0][1]).name, 'publisher.py')
        self.assertEqual(self.calls[1][2], 'plan')
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.args.publish = True
        self.assertEqual(self.execute(), 0)
        self.assertEqual([c[2] for c in self.calls[1:]], ['plan', 'publish'])
        self.assertIn('--key', self.calls[2])

    def test_invalid_repo_never_reaches_signer(self):
        self.args.repo = '../not-a-repository'
        self.args.dry_run = False
        with self.assertRaises((ValueError, RuntimeError)):
            self.execute()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
