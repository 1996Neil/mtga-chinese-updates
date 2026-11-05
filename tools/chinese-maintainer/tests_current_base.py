"""Current capture approvals cannot replace or weaken historical pin checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import official_base as base
from common import fingerprint, save_json
from steam_capture import CODE_RESOURCES


class CurrentCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve()
        self.run = self.state / 'runs/current'
        self.run.mkdir(parents=True)
        self.snapshot = {'client_version': 'fixture', 'steam_build': '1',
                         'data_versions': {}, 'official_entries': {},
                         'manifests': {}, 'files': {}}
        checks = {}
        for layer in base.LAYERS:
            path = self.run / 'official' / layer
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(layer.encode())
            row = {'relative': layer, **fingerprint(path)}
            self.snapshot['files'][layer] = row
            checks[layer] = {'fingerprint': fingerprint(path)}
        seal = self.run / 'official' / CODE_RESOURCES
        seal.parent.mkdir(parents=True)
        seal.write_bytes(b'fixture-seal')
        depot = self.run / 'evidence/depot.manifest'
        depot.parent.mkdir()
        depot.write_bytes(b'fixture-depot')
        save_json(self.run / 'snapshot.json', self.snapshot)
        self.capture = {'schema': 'MTGA_CN_STEAM_BASE_CAPTURE_V1', 'all_pass': True,
                        'runtime_acceptance': False, 'vendor_authentication': False,
                        'snapshot': self.snapshot, 'depot_manifest': fingerprint(depot),
                        'code_resources': {'fingerprint': fingerprint(seal)},
                        'depot_files': checks}
        save_json(self.run / 'base-capture.json', self.capture)
        self.review = {'schema': 'MTGA_CN_STEAM_BASE_REVIEW_V1', 'status': 'PASS',
                       'blocking_findings': [], 'reviewer': 'independent synthetic reviewer',
                       'scope': 'synthetic input binding only',
                       'capture_fingerprint': fingerprint(self.run / 'base-capture.json'),
                       'snapshot_fingerprint': fingerprint(self.run / 'snapshot.json'),
                       'capture_code_fingerprint': fingerprint(base.ROOT / 'steam_capture.py')}
        self.registration = self.state / 'base-approvals/fixture-1.json'

    def approve(self):
        save_json(self.run / 'independent-base-review.json', self.review)
        save_json(self.registration, {'schema': 'MTGA_CN_STEAM_BASE_APPROVAL_V1',
                  'run': str(self.run), 'capture_fingerprint': fingerprint(self.run / 'base-capture.json'),
                  'review_fingerprint': fingerprint(self.run / 'independent-base-review.json')})

    def test_absent_approval_does_not_approve_capture(self):
        self.assertIsNone(base.approved_opaque(self.snapshot, self.state))

    def test_separate_bound_review_accepts_opaque_only(self):
        self.approve()
        rows = base.approved_opaque(self.snapshot, self.state)
        self.assertEqual(set(rows), set(base.OPAQUE))
        self.assertEqual(rows['resources']['fingerprint'],
                         fingerprint(self.run / 'official/resources'))

    def test_missing_or_false_review_blocks_even_with_registration(self):
        self.review['status'] = 'PENDING'
        self.approve()
        with self.assertRaisesRegex(RuntimeError, 'review'):
            base.approved_opaque(self.snapshot, self.state)

    def test_resource_or_depot_mutation_rejects(self):
        self.approve()
        (self.run / 'official/resources').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'opaque'):
            base.approved_opaque(self.snapshot, self.state)
        (self.run / 'official/resources').write_bytes(b'resources')
        (self.run / 'evidence/depot.manifest').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'Steam manifest'):
            base.approved_opaque(self.snapshot, self.state)

    def test_other_release_or_code_change_rejects(self):
        self.approve()
        self.snapshot['data_versions']['Data'] = 'new'
        with self.assertRaisesRegex(RuntimeError, 'release'):
            base.approved_opaque(self.snapshot, self.state)
        self.snapshot['data_versions'].clear()
        self.review['capture_code_fingerprint'] = {'sha256': '0' * 64, 'bytes': 1}
        self.approve()
        with self.assertRaisesRegex(RuntimeError, 'review'):
            base.approved_opaque(self.snapshot, self.state)

    def test_approval_traversal_and_historical_fallback_are_not_allowed(self):
        self.approve()
        data = json.loads(self.registration.read_text())
        data['run'] = str(self.run / '..' / 'current')
        save_json(self.registration, data)
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            base.approved_opaque(self.snapshot, self.state)
        with patch.object(base, 'PIN_REVIEW_PATH', self.state / 'missing-old-pin'):
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                base.opaque_baseline(self.snapshot, self.state)


if __name__ == '__main__':
    unittest.main()
