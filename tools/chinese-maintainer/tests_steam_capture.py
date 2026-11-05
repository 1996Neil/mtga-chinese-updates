"""Synthetic Steam capture boundaries; no live game or credentials."""
import copy
import hashlib
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch

from common import fingerprint
import steam_capture as capture


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def field(number, value):
    if isinstance(value, bytes):
        return varint(number << 3 | 2) + varint(len(value)) + value
    return varint(number << 3) + varint(value)


def manifest(files, *, depot=capture.DEPOT, identity=123, duplicate=False):
    payload = b''
    for name, data in files.items():
        row = field(1, name.replace('/', '\\').encode()) + field(2, len(data))
        row += field(5, hashlib.sha1(data).digest())
        payload += field(1, row)
        if duplicate:
            payload += field(1, row)
    metadata = field(1, depot) + field(2, identity) + field(4, 0)
    result = b''
    for magic, data in ((0x71F617D0, payload), (0x1F4812BE, metadata), (0x1B81B817, b'')):
        result += struct.pack('<II', magic, len(data)) + data
    return result + struct.pack('<I', 0x32C415AB)


class SteamCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.game = self.root / 'Steam/steamapps/common/MTGA'
        self.game.mkdir(parents=True)
        self.cache = self.root / 'Steam/depotcache'
        self.cache.mkdir()
        self.acf = self.root / 'Steam/steamapps/appmanifest_2141910.acf'
        self.acf.write_text('''"AppState" {
            "appid" "2141910" "StateFlags" "4" "UpdateResult" "0"
            "buildid" "42" "TargetBuildID" "42"
            "InstalledDepots" { "2141914" { "manifest" "456" }
                "2141913" { "manifest" "123" } }
        }''')
        self.snapshot = {'game': str(self.game), 'client_version': 'fixture',
                         'steam_build': '42', 'data_versions': {}, 'files': {},
                         'manifests': {}, 'official_entries': {}}
        names = {'resources': 'MTGA.app/Contents/Resources/Data/resources.assets',
                 'bootstrap_fonts': 'MTGA.app/Contents/Resources/Data/sharedassets0.assets',
                 'info_plist': 'MTGA.app/Contents/Info.plist',
                 'executable': 'MTGA.app/Contents/MacOS/MTGA',
                 'game_assembly': 'MTGA.app/Contents/Frameworks/GameAssembly.dylib',
                 'card': 'Raw_CardDatabase_fixture.mtga', 'client': 'Raw_ClientLocalization_fixture.mtga',
                 'font_bundle': 'Fonts_fixture.mtga'}
        self.depot_files = {}
        for layer, relative in names.items():
            data = ('original-' + layer).encode()
            path = self.game / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            fp = fingerprint(path)
            self.snapshot['files'][layer] = {'relative': relative, **fp}
            if layer in ('card', 'client', 'font_bundle'):
                self.snapshot['official_entries'][path.name] = {'fingerprint': fp}
            else:
                self.depot_files[relative] = data
        seal = {'files2': {names[layer].removeprefix('MTGA.app/Contents/'):
                         {'hash2': bytes.fromhex(self.snapshot['files'][layer]['sha256'])}
                         for layer in capture.OPAQUE}}
        self.seal = self.game / capture.CODE_RESOURCES
        self.seal.parent.mkdir(parents=True, exist_ok=True)
        self.seal.write_bytes(plistlib.dumps(seal))
        self.depot_files[capture.CODE_RESOURCES] = self.seal.read_bytes()
        self.manifest = self.cache / '2141913_123.manifest'
        self.manifest.write_bytes(manifest(self.depot_files))
        self.state = self.root / 'state'
        self.patches = [
            patch.object(capture, 'STATE', self.state),
            patch.object(capture, 'detect', side_effect=lambda *args: copy.deepcopy(self.snapshot)),
            patch('capture_base.ui_text', return_value={'one': 'Hello'}),
            patch('capture_base.ui_oracle', return_value={'one': 'Hello'}),
            patch('capture_base.tmp_fallback_pointers', return_value=[]),
            patch('capture_base.font_payload', return_value={'official': 'fixture'}),
        ]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def collect(self):
        return capture.collect(self.snapshot, depotcache=self.cache)

    def test_collect_verifies_depot_after_other_depot_and_does_not_approve(self):
        result = self.collect()
        self.assertTrue(result['all_pass'])
        self.assertEqual(result['steam']['manifest_id'], '123')
        self.assertFalse(result['vendor_authentication'])
        self.assertFalse(result['runtime_acceptance'])
        self.assertEqual(result['independent_review'], 'PENDING')
        self.assertNotIn('LastOwner', str(result))

    def test_wrong_depot_metadata_rejected(self):
        self.manifest.write_bytes(manifest(self.depot_files, identity=124))
        with self.assertRaisesRegex(RuntimeError, 'depot metadata'):
            self.collect()

    def test_duplicate_payload_name_rejected(self):
        self.manifest.write_bytes(manifest(self.depot_files, duplicate=True))
        with self.assertRaisesRegex(RuntimeError, 'Duplicate Steam manifest'):
            self.collect()

    def test_truncated_protobuf_and_overflow_rejected(self):
        for data in (b'\x0a\x05x', b'\x80' * 10 + b'\x02', b'\x0f', b'\x00'):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                capture.protobuf(data)

    def test_truncated_or_extra_manifest_rejected(self):
        original = self.manifest.read_bytes()
        for data in (original[:-1], original + b'x', original[:20]):
            self.manifest.write_bytes(data)
            with self.assertRaises(RuntimeError):
                self.collect()

    def test_path_traversal_rejected(self):
        self.manifest.write_bytes(manifest({'../outside': b'bad'}))
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            self.collect()

    def test_wrong_install_build_or_incomplete_rejected(self):
        original = self.acf.read_text()
        for text in (original.replace('"StateFlags" "4"', '"StateFlags" "6"'),
                     original.replace('"TargetBuildID" "42"', '"TargetBuildID" "43"')):
            self.acf.write_text(text)
            with self.assertRaises(RuntimeError):
                self.collect()

    def test_tampered_opaque_rejected_even_snapshot_self_hash_changed(self):
        row = self.snapshot['files']['resources']
        path = self.game / row['relative']
        path.write_bytes(b'tampered')
        row.update(fingerprint(path))
        with self.assertRaisesRegex(RuntimeError, 'depot file mismatch'):
            self.collect()

    def test_tampered_binary_rejected_even_snapshot_self_hash_changed(self):
        row = self.snapshot['files']['game_assembly']
        path = self.game / row['relative']
        path.write_bytes(b'altered binary')
        row.update(fingerprint(path))
        with self.assertRaisesRegex(RuntimeError, 'depot file mismatch'):
            self.collect()

    def test_tampered_code_resources_rejected(self):
        self.seal.write_bytes(b'bad seal')
        with self.assertRaisesRegex(RuntimeError, 'CodeResources mismatch'):
            self.collect()

    def test_matching_depot_with_wrong_seal_sha256_rejected(self):
        seal = plistlib.loads(self.seal.read_bytes())
        seal['files2']['Resources/Data/resources.assets']['hash2'] = b'x' * 32
        self.seal.write_bytes(plistlib.dumps(seal))
        self.depot_files[capture.CODE_RESOURCES] = self.seal.read_bytes()
        self.manifest.write_bytes(manifest(self.depot_files))
        with self.assertRaisesRegex(RuntimeError, 'CodeResources SHA256'):
            self.collect()

    def test_unverified_raw_or_english_or_fallback_rejected(self):
        path = self.game / self.snapshot['files']['card']['relative']
        original = path.read_bytes()
        path.write_bytes(b'patched raw')
        with self.assertRaisesRegex(RuntimeError, 'manifest mismatch'):
            self.collect()
        path.write_bytes(original)
        with patch('capture_base.ui_text', return_value={'one': '中文'}):
            with self.assertRaisesRegex(RuntimeError, 'UI differs'):
                self.collect()
        with patch('capture_base.tmp_fallback_pointers', return_value=[{'m_PathID': 1}]):
            with self.assertRaisesRegex(RuntimeError, 'fallback'):
                self.collect()

    def test_live_snapshot_drift_stops_capture(self):
        other = copy.deepcopy(self.snapshot)
        other['steam_build'] = '43'
        with self.assertRaisesRegex(RuntimeError, 'drift'):
            capture.collect(self.snapshot, depotcache=self.cache, inspect=lambda: other)

    def test_stage_copies_originals_and_metadata_without_game_writes(self):
        before = {str(path): fingerprint(path) for path in self.game.rglob('*') if path.is_file()}
        destination = self.state / 'runs/capture'
        evidence = capture.stage(self.snapshot, destination)
        self.assertTrue((destination / 'base-capture.json').exists())
        self.assertEqual(fingerprint(Path(evidence['depot_manifest']['staged_path'])),
                         fingerprint(self.manifest))
        for layer in capture.LAYERS:
            row = self.snapshot['files'][layer]
            self.assertEqual(fingerprint(destination / 'official' / row['relative']),
                             {key: row[key] for key in ('sha256', 'bytes')})
        self.assertFalse((destination / 'independent-review.json').exists())
        self.assertEqual(before, {str(path): fingerprint(path) for path in self.game.rglob('*')
                                  if path.is_file()})

    def test_stage_existing_or_outside_destination_rejected(self):
        target = self.state / 'runs/existing'
        target.mkdir(parents=True)
        for path in (target, self.root / 'elsewhere'):
            with self.assertRaisesRegex(RuntimeError, 'fresh'):
                capture.stage(self.snapshot, path)


if __name__ == '__main__':
    unittest.main()
