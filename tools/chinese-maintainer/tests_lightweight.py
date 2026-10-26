"""Regression tests for incremental visual-artifact reuse."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cli
from common import fingerprint


class LightweightReuseTests(unittest.TestCase):
    def test_visuals_reuse_only_when_text_and_inputs_are_identical(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            previous = root / 'previous'
            previous.mkdir()
            for name, data in (('resources.assets', b'resources'),
                               ('sharedassets0.assets', b'fonts'),
                               ('Fonts.mtga', b'bundle')):
                (previous / name).write_bytes(data)
            (previous / 'fonts-validation.json').write_text(
                json.dumps({'bundle_path': str(previous / 'Fonts.mtga')}))
            (previous / 'migration-plan.json').write_text(json.dumps({'ui': {'A': '中文'}}))
            card = root / 'card.mtga'
            client = root / 'client.mtga'
            card.write_bytes(b'card')
            client.write_bytes(b'client')
            fp = lambda path: fingerprint(path)
            previous_report = {
                'schema': 'MTGA_CN_MAINTAINER_V1',
                'code_fingerprints': {'code': 'same'},
                'review_inputs': {'reviews': 'same'},
                'baseline_inputs': {},
                'candidate_files': {
                    'card': {'sha256': fp(card)['sha256'], 'bytes': fp(card)['bytes']},
                    'client': {'sha256': fp(client)['sha256'], 'bytes': fp(client)['bytes']},
                },
                'localization': {'resources': {'changed': 1}, 'fonts': {'status': 'PASS'}},
            }
            snapshot = {'files': {
                layer: {'sha256': 'same-' + layer, 'bytes': 1}
                for layer in ('resources', 'bootstrap_fonts', 'font_bundle')
            }}
            (previous / 'snapshot.json').write_text(json.dumps(snapshot))
            with patch.object(cli, 'review_input_fingerprints', return_value={'reviews': 'same'}):
                self.assertTrue(cli.reusable_visuals(
                    previous, previous_report, snapshot, {'code': 'same'}, {}, card, client))
                card.write_bytes(b'changed')
                self.assertFalse(cli.reusable_visuals(
                    previous, previous_report, snapshot, {'code': 'same'}, {}, card, client))

    def test_reuse_copies_visuals_and_rewrites_bundle_path(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            previous = root / 'previous'
            destination = root / 'current'
            previous.mkdir()
            destination.mkdir()
            (previous / 'resources.assets').write_bytes(b'resources')
            (previous / 'sharedassets0.assets').write_bytes(b'fonts')
            bundle = previous / 'Fonts.mtga'
            bundle.write_bytes(b'bundle')
            (previous / 'fonts-validation.json').write_text(
                json.dumps({'bundle_path': str(bundle)}))
            (previous / 'migration-plan.json').write_text(json.dumps({'ui': {'A': '中文'}}))
            report = {'localization': {'resources': {'changed': 1}, 'fonts': {'status': 'PASS'}}}
            migration, loc = {}, {}
            fonts = cli.reuse_visual_artifacts(previous, report, destination, migration, loc)
            self.assertEqual(migration, {'ui': {'A': '中文'}})
            self.assertEqual(loc, report['localization'])
            self.assertEqual(Path(fonts['bundle_path']), destination / 'Fonts.mtga')
            self.assertEqual((destination / 'resources.assets').read_bytes(), b'resources')
            self.assertEqual((destination / 'sharedassets0.assets').read_bytes(), b'fonts')


if __name__ == '__main__':
    unittest.main()
