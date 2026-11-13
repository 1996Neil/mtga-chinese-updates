"""Pinned online data recovery must reject modified inputs, without live network."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import online_sources as online


class OnlineSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.lock = {'schema': 'MTGA_CN_ONLINE_SOURCE_LOCK_V1',
                     'repository': '1996Neil/mtga-chinese-baseline',
                     'commit': 'a' * 40, 'directory': 'portable/fixture',
                     'manifest_fingerprint': {'sha256': 'b' * 64, 'bytes': 2}}

    def load(self):
        path = self.root / 'source.json'
        path.write_text(json.dumps(self.lock))
        return online.load_lock(path)

    def test_lock_rejects_floating_ref_other_repo_and_traversal(self):
        self.assertEqual(self.load(), self.lock)
        for field, value in [('commit', 'main'), ('directory', '../outside'),
                             ('repository', 'other/repo')]:
            original = self.lock[field]
            self.lock[field] = value
            with self.assertRaises(RuntimeError):
                self.load()
            self.lock[field] = original

    def checkout(self):
        checkout = self.root / 'seed'
        subprocess.run(['git', 'init', '--quiet', str(checkout)], check=True)
        data = checkout / self.lock['directory']
        data.mkdir(parents=True)
        (data / 'manifest.json').write_text('{}')
        subprocess.run(['git', '-C', str(checkout), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet',
                        '-m', 'fixture'], check=True)
        commit = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                         text=True).strip()
        self.lock['commit'] = commit
        self.lock['manifest_fingerprint'] = online.fingerprint(data / 'manifest.json')
        destination = self.root / 'sources' / commit
        destination.parent.mkdir()
        checkout.rename(destination)
        return destination, destination / self.lock['directory']

    def mocked_run(self, args):
        if args[0] == 'gh':
            return json.dumps({'nameWithOwner': self.lock['repository'], 'visibility': 'PRIVATE'})
        result = subprocess.run([str(x) for x in args], text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def test_clean_pinned_checkout_reuses_without_clone(self):
        _, data = self.checkout()
        with patch.object(online, 'run', side_effect=self.mocked_run):
            self.assertEqual(online.sync_data(self.lock, self.root), data)

    def test_modified_data_rejected_without_reset_or_overwrite(self):
        _, data = self.checkout()
        (data / 'manifest.json').write_text('{"changed":true}')
        with patch.object(online, 'run', side_effect=self.mocked_run):
            with self.assertRaisesRegex(RuntimeError, '本地修改'):
                online.sync_data(self.lock, self.root)
        self.assertEqual((data / 'manifest.json').read_text(), '{"changed":true}')

    def test_public_data_repository_is_rejected(self):
        self.checkout()
        with patch.object(online, 'run', return_value=json.dumps({
                'nameWithOwner': self.lock['repository'], 'visibility': 'PUBLIC'})):
            with self.assertRaisesRegex(RuntimeError, '私有'):
                online.sync_data(self.lock, self.root)

    def test_wrong_manifest_hash_rejected(self):
        self.checkout()
        self.lock['manifest_fingerprint']['sha256'] = '0' * 64
        with patch.object(online, 'run', side_effect=self.mocked_run):
            with self.assertRaisesRegex(RuntimeError, '哈希'):
                online.sync_data(self.lock, self.root)

    def test_config_migration_preserves_original_and_does_not_read_private_key(self):
        path = self.root / 'maintenance.json'
        original = b'{"schema":"legacy", "state_root":"/old"}\n'
        path.write_bytes(original)
        online.save(path, {'online': True, 'key_file': '/missing/key-not-read'})
        self.assertEqual(path.with_name('maintenance-legacy.json').read_bytes(), original)
        self.assertEqual(json.loads(path.read_text())['key_file'], '/missing/key-not-read')
        online.save(path, {'online': True})
        self.assertEqual(path.with_name('maintenance-legacy.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
