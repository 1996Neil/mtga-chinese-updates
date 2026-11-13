"""Frozen reuse checks use small synthetic inputs; never real keys or game writes."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reuse_package as reuse
from publisher import LAYERS, CHECKS, canonical, dump, fingerprint, make_delta, subject_for


class FrozenReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        self.run = self.root / 'candidate'; self.run.mkdir()
        self.client = self.root / 'client'; self.client.write_bytes(b'fixture-client')
        self.public = self.root / 'public'; self.public.write_text('fixture-public')
        self.acceptance = self.root / 'acceptance.json'
        snapshot = {'client_version': 'fixture', 'steam_build': '1', 'data_versions': {},
                    'files': {}, 'manifests': {}}
        report = {'schema': 'MTGA_CN_MAINTAINER_V1', 'candidate_files': {},
                  'code_fingerprints': {}, 'localization': {'static_status': 'PASS'},
                  'import_compatibility': {'static_status': 'PASS'}, 'high_risk_unknown': []}
        bases = {'layers': []}
        resources = []
        for layer in LAYERS:
            rel = layer + '.bin'
            base = self.run / 'official' / rel; base.parent.mkdir(exist_ok=True)
            base.write_bytes(b'old ' + layer.encode())
            output = self.run / rel; output.write_bytes(b'new ' + layer.encode())
            snapshot['files'][layer] = {'relative': rel, **fingerprint(base)}
            report['candidate_files'][layer] = {'relative': rel, 'path': str(output), **fingerprint(output)}
            bases['layers'].append({'layer': layer, 'relative': rel, 'fingerprint': fingerprint(base)})
            patch_file = self.source / (layer + '.delta'); make_delta(base, output, patch_file)
            full = self.source / (layer + '.full'); full.write_bytes(output.read_bytes())
            resources.append({'layer': layer, 'relative': rel, 'input': fingerprint(base),
                              'output': fingerprint(output),
                              'patch': {'name': patch_file.name, 'fingerprint': fingerprint(patch_file)},
                              'full': {'name': full.name, 'fingerprint': fingerprint(full)}})
        subject = subject_for(snapshot, report, bases)
        acceptance = {'schema': 'MTGA_CN_PUBLISHER_ACCEPTANCE_V1',
                      'subject_sha256': hashlib.sha256(canonical(subject)).hexdigest(),
                      'checks': dict.fromkeys(CHECKS, True), 'tester_quote': 'fixture quote',
                      'scope': 'fixture scope', 'architectures': ['arm64']}
        dump(self.acceptance, acceptance)
        self.release = {'schema': 'MTGA_CN_RELEASE_V1', 'id': 'fixture-1-r4', 'revision': 4,
                        'client_version': 'fixture', 'steam_build': '1', 'platform': 'macOS-Steam',
                        'architectures': ['arm64'], 'protected_files': [], 'resources': resources,
                        'acceptance': {'status': 'accepted', 'scope': acceptance['scope'],
                                       'evidence_sha256': fingerprint(self.acceptance)['sha256']}}
        self.manifest = self.source / 'fixture-1-r4.json'; dump(self.manifest, self.release)
        dump(self.source / 'profile.json', {'channel': 'stable'})
        index = {'releases': [{'id': self.release['id']}]}
        dump(self.source / 'index-stable.json', {'payload': base64.b64encode(canonical(index)).decode()})
        dump(self.run / 'snapshot.json', snapshot); dump(self.run / 'report.json', report)
        dump(self.run / 'official-base.json', bases)
        dump(self.run / 'independent-review.json', {'stable_publication_approved': False})
        self.plan_patch = patch.object(reuse, 'publication_plan', side_effect=lambda *a: {
            'release': copy.deepcopy(self.release), 'manifest': str(self.manifest)})
        self.plan_patch.start(); self.addCleanup(self.plan_patch.stop)

    def validate(self, review=None):
        return reuse.validate(self.source, self.run, self.acceptance, 'fixture/repo',
                              self.client, self.public, review)

    def approval(self):
        review = self.root / 'new-review.json'
        dump(review, {'schema': 'MTGA_CN_STABLE_REUSE_REVIEW_V1', 'status': 'PASS',
                      'stable_republish_approved': True, 'blocking_findings': [],
                      'reviewer': 'independent-fixture', 'scope': 'byte-identical reuse',
                      'binding': self.validate()[1]})
        return review

    def test_old_testing_review_is_preserved_and_new_review_is_separate(self):
        old = (self.run / 'independent-review.json').read_bytes()
        self.validate(self.approval())
        self.assertEqual((self.run / 'independent-review.json').read_bytes(), old)

    def test_changed_resource_or_delta_is_rejected(self):
        (self.source / 'card.full').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '变化'):
            self.validate()

    def test_changed_candidate_rejected(self):
        (self.run / 'card.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '变化'):
            self.validate()

    def test_source_must_be_stable_accepted(self):
        self.release['acceptance']['status'] = 'testing'
        with self.assertRaisesRegex(ValueError, 'stable accepted'):
            self.validate()

    def test_acceptance_hash_and_subject_remain_bound(self):
        evidence = json.loads(self.acceptance.read_text()); evidence['scope'] = 'changed'
        dump(self.acceptance, evidence)
        with self.assertRaisesRegex(ValueError, 'Signed acceptance'):
            self.validate()

    def test_stale_review_or_nonapproval_rejected(self):
        review = self.approval()
        value = json.loads(review.read_text()); value['stable_republish_approved'] = False
        dump(review, value)
        with self.assertRaisesRegex(ValueError, 'independent'):
            self.validate(review)
        review = self.approval()
        (self.run / 'independent-review.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'independent'):
            self.validate(review)

    def test_signature_failure_propagates(self):
        with patch.object(reuse, 'publication_plan', side_effect=ValueError('bad signature')):
            with self.assertRaisesRegex(ValueError, 'signature'):
                self.validate()

    def test_symlink_output_rejected(self):
        output = self.run / 'card.bin'; target = self.run / 'other'
        output.rename(target); output.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'canonical'):
            self.validate()

    def test_package_without_review_never_reaches_signer(self):
        with patch.object(reuse, 'sign', side_effect=AssertionError('signer called')):
            with self.assertRaisesRegex(ValueError, 'mandatory'):
                reuse.package(self.source, self.run, self.acceptance, None,
                              self.root / 'output', 'fixture/repo', 'mtga-fixture-1-r5',
                              self.client, self.public, self.root / 'missing-key', 5)

    def test_package_copies_identical_assets_without_rebuilding(self):
        review = self.approval(); destination = self.root / 'output'
        def fake_sign(client, key, value, output, scratch):
            dump(output, value)
        with patch.object(reuse, 'sign', side_effect=fake_sign):
            release = reuse.package(self.source, self.run, self.acceptance, review,
                                    destination, 'fixture/repo', 'mtga-fixture-1-r5',
                                    self.client, self.public, self.root / 'missing-key', 5)
        self.assertEqual(release['revision'], 5)
        self.assertEqual(release['acceptance'], self.release['acceptance'])
        for row in release['resources']:
            for kind in ('patch', 'full'):
                name = row[kind]['name']
                self.assertEqual((destination / name).read_bytes(), (self.source / name).read_bytes())
                self.assertIn('/mtga-fixture-1-r5/', row[kind]['url'])

    def test_validly_hashed_but_invalid_delta_is_rejected(self):
        delta = self.source / 'card.delta'; delta.write_bytes(b'not-a-delta')
        self.release['resources'][0]['patch']['fingerprint'] = fingerprint(delta)
        with self.assertRaisesRegex(ValueError, '差分'):
            self.validate()

    def test_changed_acceptance_subject_rejected(self):
        evidence = json.loads(self.acceptance.read_text()); evidence['subject_sha256'] = '0' * 64
        dump(self.acceptance, evidence)
        with self.assertRaisesRegex(ValueError, '发布验收不属于'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
