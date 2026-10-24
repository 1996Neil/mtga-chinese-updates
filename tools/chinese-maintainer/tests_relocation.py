"""Relocation must preserve immutable evidence and guarded rollback semantics."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import common as c
import installer as i
import official_base as b
from tests_base import BaseFixture


class PathMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.old, self.new = self.root / 'gone', self.root / 'moved'
        self.new.mkdir()
        self.mapping = patch.object(c, 'MOVED_ROOTS', ((self.old, self.new),))
        self.mapping.start()

    def tearDown(self):
        self.mapping.stop()
        self.temp.cleanup()

    def test_immutable_json_and_database_use_new_location(self):
        raw = ('{"path":"' + str(self.old / 'blob') + '"}\n').encode()
        (self.new / 'receipt.json').write_bytes(raw)
        before = c.fingerprint(self.new / 'receipt.json')
        self.assertEqual(c.read_json(self.old / 'receipt.json')['path'], str(self.old / 'blob'))
        self.assertEqual(c.evidence_path(self.new / 'blob'), str(self.old / 'blob'))
        self.assertEqual(c.fingerprint(self.old / 'receipt.json'), before)
        self.assertEqual((self.new / 'receipt.json').read_bytes(), raw)
        with sqlite3.connect(self.new / 'data.sqlite') as db:
            db.execute('CREATE TABLE evidence(value TEXT)')
            db.execute('INSERT INTO evidence VALUES (?)', (str(self.old / 'blob'),))
        with c.database(self.old / 'data.sqlite') as db:
            self.assertEqual(db.execute('SELECT value FROM evidence').fetchone()[0], str(self.old / 'blob'))
        self.assertFalse(self.old.exists())

    def test_checked_copy_and_atomic_write_do_not_recreate_old_root(self):
        c.atomic_bytes(self.old / 'blob', b'original')
        c.copy_checked(self.old / 'blob', self.old / 'copied', c.fingerprint(self.old / 'blob'))
        self.assertEqual((self.new / 'copied').read_bytes(), b'original')
        self.assertFalse(self.old.exists())

    def test_traversal_symlink_sidecar_and_unrelated_prefix(self):
        (self.new / 'blob').write_bytes(b'original')
        with self.assertRaises(RuntimeError):
            c.regular(self.old / '..' / 'moved' / 'blob')
        (self.new / 'alias').symlink_to(self.new / 'blob')
        with self.assertRaises(RuntimeError):
            c.regular(self.old / 'alias')
        (self.new / 'blob-wal').touch()
        with self.assertRaises(RuntimeError):
            c.regular(self.old / 'blob')
        outside = self.root / 'gone-sibling' / 'blob'
        self.assertEqual(c.managed_path(outside), outside)

    def test_capture_reference_keys_still_bind_original_identifiers(self):
        import capture_base as capture
        state = self.new / 'state'
        run = state / 'runs/pin'
        receipt = {}
        for layer, rel in capture.ASSET_RELATIVE.items():
            for p in (run / 'official' / rel, run / Path(rel).name):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(layer.encode())
                receipt[c.evidence_path(p)] = c.fingerprint(p)
        path = state / 'receipt.json'
        c.save_json(path, receipt)
        before = path.read_bytes()
        with patch.object(capture, 'STATE', state), patch.object(b, 'PIN_RUN', 'pin'), \
                patch.object(b, 'PIN_RECEIPT_PATH', path), patch.object(b, 'PIN_RECEIPT', c.fingerprint(path)):
            official, patched = capture.pinned_references(receipt_path=self.old / 'state/receipt.json')
        self.assertTrue(all(p.exists() for p in [*official.values(), *patched.values()]))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.old.exists())


class RelocatedTransactionTests(BaseFixture):
    def relocate(self):
        old = self.state
        self.state = self.root / 'relocated-state'
        old.rename(self.state)
        self.run = self.state / 'runs' / self.run.name
        self.official = self.state / 'runs/initial/official'
        self.journal = self.state / 'installation.json'
        self.mapping = patch.object(c, 'MOVED_ROOTS', ((old, self.state),))
        self.mapping.start()
        self.addCleanup(self.mapping.stop)
        return old

    def test_legacy_transaction_diagnosis_and_exact_rollback(self):
        tx = self.install_patch('trial')
        tx['state'] = 'trial'
        c.save_json(self.journal, tx)
        historical = copy.deepcopy(tx)
        receipt = self.journal.read_bytes()
        old = self.relocate()
        result = self.diagnose()
        self.assertTrue(result['safe_to_rebuild'])
        self.assertTrue(result['rollback']['valid'])
        self.assertEqual(tx, historical)
        self.assertEqual(self.journal.read_bytes(), receipt)
        i.restore_transaction(tx, self.journal, self.game, self.inspect, lambda: None)
        self.assertEqual(self.inspect(), self.snap)
        self.assertFalse(old.exists())

    def test_legacy_candidate_containment_maps_both_sides(self):
        run, report = self.prepare('candidate')
        old = self.relocate()
        tx = i.create_transaction(report, run, self.game, self.root / 'backups',
                                  self.inspect, lambda: None, self.state)
        i.validate_transaction_shape(tx)
        self.assertEqual(Path(tx['run_dir']).parent, self.state / 'runs')
        self.assertTrue(all(str(self.state) in row['candidate_path'] for row in tx['files']))
        self.assertFalse(old.exists())


if __name__ == '__main__':
    unittest.main()
