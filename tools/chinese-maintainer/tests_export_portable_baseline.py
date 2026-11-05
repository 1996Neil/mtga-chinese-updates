"""Portable export integrity and provenance; synthetic inputs only."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from common import fingerprint
import export_portable_baseline as exporter


class PortableExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.release_file = self.root / 'release.json'
        self.release_file.write_text('fixture signed source')
        self.client = self.root / 'client'
        self.client.write_bytes(b'fixture verifier')
        self.key = self.root / 'public.txt'
        self.key.write_text('fixture public key')
        self.original_card = self.root / 'original-card'
        with sqlite3.connect(self.original_card) as db:
            fields = ','.join(name + (' TEXT' if name in
                            ('ExpansionCode', 'CollectorNumber', 'LinkedFaceGrpIds') else ' INTEGER')
                              for name in exporter.IDENTITY_FIELDS)
            db.execute('CREATE TABLE Cards(' + fields + ')')
            db.execute('INSERT INTO Cards VALUES (1,10,"SET","1",0,1,0,0,0,"")')
            db.execute('CREATE TABLE Localizations_enUS(LocId INTEGER,Formatted INTEGER,Loc TEXT)')
            db.execute('INSERT INTO Localizations_enUS VALUES (10,0,"Plain name")')
            db.execute('INSERT INTO Localizations_enUS VALUES (10,1,"Formatted name")')
        self.original_client = self.root / 'original-client'
        self.original_client.write_bytes(b'original-client')
        self.outputs = {}
        resources = []
        for layer in ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle'):
            path = self.root / ('signed-' + layer)
            if layer == 'card':
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE Localizations_enUS(LocId INTEGER,Formatted INTEGER,Loc TEXT)')
                    db.execute('INSERT INTO Localizations_enUS VALUES (10,1,"中文牌名")')
            elif layer == 'client':
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE Loc(Key TEXT,enUS TEXT)')
                    db.execute('INSERT INTO Loc VALUES ("ui","中文界面")')
            else:
                path.write_bytes(('signed-' + layer).encode())
            self.outputs[layer] = path
            original = {'card': self.original_card, 'client': self.original_client}.get(layer, path)
            resources.append({'layer': layer, 'input': fingerprint(original), 'output': fingerprint(path)})
        self.release = {'schema': 'MTGA_CN_RELEASE_V1', 'id': 'fixture-42-r4',
                        'client_version': 'fixture', 'steam_build': '42',
                        'acceptance': {'status': 'accepted'}, 'resources': resources}
        identity = {'cards': [{name: (1 if name in ('GrpId', 'IsPrimaryCard') else
                                     10 if name == 'TitleId' else
                                     'SET' if name == 'ExpansionCode' else
                                     '1' if name == 'CollectorNumber' else
                                     '' if name == 'LinkedFaceGrpIds' else 0)
                               for name in exporter.IDENTITY_FIELDS}]}
        self.contexts = {('card', '10'): {'english': 'Formatted name',
                         'field_use': ['Cards.TitleId'], 'identity': identity},
                         ('client', 'ui'): {'english': 'English interface',
                         'field_use': ['Loc.enUS'], 'identity': {'client_key': 'ui'}}}
        self.destination = self.root / 'portable'
        import sys
        sys.path.insert(0, str(Path(exporter.__file__).resolve().parents[1] / 'subscriber'))
        self.patches = [
            patch('github_publish.signed', side_effect=lambda *args: copy.deepcopy(self.release)),
            patch.object(exporter, 'contexts', side_effect=lambda *args: copy.deepcopy(self.contexts)),
            patch('capture_base.ui_text', return_value={'ui': '中文界面'}),
        ]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def export(self):
        return exporter.export(self.destination, self.release_file, self.original_card,
                               self.original_client, self.outputs, self.client, self.key)

    def test_all_contexts_portable_no_status_upgrade_and_plain_canonical_title(self):
        result = self.export()
        self.assertFalse(result['human_acceptance'])
        self.assertFalse(result['runtime_acceptance'])
        self.assertTrue(result['contains_ai_authored_or_unknown'])
        self.assertEqual(result['files']['ui']['records'], 1)
        title = json.loads((self.destination / 'card-titles.jsonl').read_text())
        self.assertEqual(title['english'], 'Plain name')
        for path in self.destination.iterdir():
            self.assertNotIn(str(self.root), path.read_text())

    def test_wrong_signed_output_stops_before_destination_created(self):
        self.outputs['resources'].write_bytes(b'wrong')
        with self.assertRaisesRegex(RuntimeError, 'Signed output mismatch'):
            self.export()
        self.assertFalse(self.destination.exists())

    def test_signature_failure_stops_before_output(self):
        with patch('github_publish.signed', side_effect=RuntimeError('invalid signature')):
            with self.assertRaisesRegex(RuntimeError, 'signature'):
                self.export()
        self.assertFalse(self.destination.exists())

    def test_identity_use_change_rejected_even_same_source_strings(self):
        other = copy.deepcopy(self.contexts)
        other['card', '10']['field_use'] = ['Cards.FlavorTextId']
        with patch.object(exporter, 'contexts', side_effect=[self.contexts, other]):
            with self.assertRaisesRegex(RuntimeError, 'identity/use'):
                self.export()

    def test_missing_official_ui_oracle_rejected(self):
        with patch('capture_base.ui_text', return_value={'unknown': '中文'}):
            with self.assertRaisesRegex(RuntimeError, 'UI English absent'):
                self.export()

    def test_source_drift_does_not_write_completion_manifest(self):
        calls = 0
        original = exporter.fingerprint

        def changing(path):
            nonlocal calls
            if Path(path) == self.release_file:
                calls += 1
                if calls > 1:
                    return {'bytes': 0, 'sha256': '0' * 64}
            return original(path)

        with patch.object(exporter, 'fingerprint', side_effect=changing):
            with self.assertRaises(RuntimeError):
                self.export()
        self.assertFalse((self.destination / 'manifest.json').exists())

    def test_existing_output_never_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / 'marker'
        marker.write_text('preserve')
        with self.assertRaisesRegex(RuntimeError, 'must be new'):
            self.export()
        self.assertEqual(marker.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
