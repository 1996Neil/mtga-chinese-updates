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
            light = root / 'light'
            medium = root / 'medium'
            light.write_bytes(b'light')
            medium.write_bytes(b'medium')
            (previous / 'fonts-validation.json').write_text(json.dumps({
                'bundle_path': str(previous / 'Fonts.mtga'),
                'source_fonts': {'Light': {'source': str(light), 'source_fingerprint': fingerprint(light)},
                                 'Medium': {'source': str(medium), 'source_fingerprint': fingerprint(medium)}},
            }))
            (previous / 'migration-plan.json').write_text(json.dumps({'ui': {'A': '中文'}}))
            proof_files = {
                'fonts-validation.json': fingerprint(previous / 'fonts-validation.json'),
                'migration-plan.json': fingerprint(previous / 'migration-plan.json'),
            }
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
                'visual_inputs': {'official': {}, 'font_bundle_entry': {}},
                'proof_files': {
                    **proof_files,
                },
                'candidate_files': {
                    'card': {'sha256': fp(card)['sha256'], 'bytes': fp(card)['bytes']},
                    'client': {'sha256': fp(client)['sha256'], 'bytes': fp(client)['bytes']},
                    'resources': {'sha256': fp(previous / 'resources.assets')['sha256'],
                                  'bytes': fp(previous / 'resources.assets')['bytes']},
                    'bootstrap_fonts': {'sha256': fp(previous / 'sharedassets0.assets')['sha256'],
                                        'bytes': fp(previous / 'sharedassets0.assets')['bytes']},
                    'font_bundle': {'sha256': fp(previous / 'Fonts.mtga')['sha256'],
                                    'bytes': fp(previous / 'Fonts.mtga')['bytes']},
                },
                'localization': {'static_status': 'PASS',
                                 'resources': {'changed': 1},
                                 'fonts': {'status': 'PASS'}},
            }
            snapshot = {'files': {
                'resources': {'sha256': fp(previous / 'resources.assets')['sha256'],
                              'bytes': fp(previous / 'resources.assets')['bytes']},
                'bootstrap_fonts': {'sha256': fp(previous / 'sharedassets0.assets')['sha256'],
                                    'bytes': fp(previous / 'sharedassets0.assets')['bytes']},
                'font_bundle': {'sha256': fp(previous / 'Fonts.mtga')['sha256'],
                                'bytes': fp(previous / 'Fonts.mtga')['bytes']},
            }}
            previous_report['visual_inputs'] = {'official': {}, 'font_bundle_entry': {}}
            (previous / 'snapshot.json').write_text(json.dumps(snapshot))
            with patch.object(cli, 'review_input_fingerprints', return_value={'reviews': 'same'}):
                self.assertTrue(cli.reusable_visuals(
                    previous, previous_report, previous_report['visual_inputs'],
                    {'code': 'same'}, {}, card, client))
                card.write_bytes(b'changed')
                self.assertFalse(cli.reusable_visuals(
                    previous, previous_report, previous_report['visual_inputs'],
                    {'code': 'same'}, {}, card, client))

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
            proof_files = {
                'fonts-validation.json': fingerprint(previous / 'fonts-validation.json'),
                'migration-plan.json': fingerprint(previous / 'migration-plan.json'),
            }
            report = {'localization': {'resources': {'changed': 1}, 'fonts': {'status': 'PASS'}}}
            report['proof_files'] = proof_files
            light = root / 'light'
            medium = root / 'medium'
            light.write_bytes(b'light')
            medium.write_bytes(b'medium')
            (previous / 'fonts-validation.json').write_text(json.dumps({
                'bundle_path': str(bundle),
                'source_fonts': {'Light': {'source': str(light), 'source_fingerprint': fingerprint(light)},
                                 'Medium': {'source': str(medium), 'source_fingerprint': fingerprint(medium)}},
            }))
            report['proof_files']['fonts-validation.json'] = fingerprint(previous / 'fonts-validation.json')
            report['candidate_files'] = {
                'resources': {'sha256': fingerprint(previous / 'resources.assets')['sha256'],
                              'bytes': fingerprint(previous / 'resources.assets')['bytes']},
                'bootstrap_fonts': {'sha256': fingerprint(previous / 'sharedassets0.assets')['sha256'],
                                    'bytes': fingerprint(previous / 'sharedassets0.assets')['bytes']},
                'font_bundle': {'sha256': fingerprint(bundle)['sha256'],
                                'bytes': fingerprint(bundle)['bytes']},
            }
            migration, loc = {}, {}
            fonts = cli.reuse_visual_artifacts(previous, report, destination, migration, loc)
            self.assertEqual(migration, {'ui': {'A': '中文'}})
            self.assertEqual(loc['resources']['changed'], report['localization']['resources']['changed'])
            self.assertEqual(loc['fonts']['status'], report['localization']['fonts']['status'])
            self.assertEqual(Path(fonts['bundle_path']), destination / 'Fonts.mtga')
            self.assertEqual((destination / 'resources.assets').read_bytes(), b'resources')
            self.assertEqual((destination / 'sharedassets0.assets').read_bytes(), b'fonts')


if __name__ == '__main__':
    unittest.main()
