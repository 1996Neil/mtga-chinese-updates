"""A finished candidate keeps its verifiable record, not its bulk bytes."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest

import prune


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.run = self.root / 'run'
        self.run.mkdir()
        (self.run / 'report.json').write_text('{"client_version":"x"}')
        (self.run / 'snapshot.json').write_text('{"steam_build":"1"}')
        (self.run / 'card-decisions.json').write_text('[' + ','.join('{"k":1}' for _ in range(2000)) + ']')
        for name in ('card-localized.mtga', 'card-final.mtga', 'Fonts_x.mtga'):
            (self.run / name).write_text('x' * 20000)
        for name in ('official', 'comparison', 'package-testing-r1', 'release-r1', 'fixtures'):
            (self.run / name).mkdir()
            (self.run / name / 'blob.mtga').write_text('y' * 20000)
        (self.run / 'publication.json').write_text('{"release":"r1"}')

    def test_archive_keeps_the_record_and_drops_the_bulk(self):
        summary = prune.archive(self.run, publication=self.run / 'publication.json')
        remaining = sorted(p.name for p in self.run.iterdir())
        self.assertEqual(remaining, ['archive.json', 'card-decisions.json.gz',
                                     'publication.json', 'report.json', 'snapshot.json'])
        self.assertEqual(json.loads(gzip.open(self.run / 'card-decisions.json.gz').read()), json.loads('[' + ','.join('{"k":1}' for _ in range(2000)) + ']'))
        self.assertLess(summary['bytes_after'], summary['bytes_before'] // 4)
        self.assertGreater(summary['bytes_freed'], 100000)

    def test_archive_is_idempotent(self):
        first = prune.archive(self.run, publication=self.run / 'publication.json')
        second = prune.archive(self.run, publication=self.run / 'publication.json')
        self.assertEqual(first, second)

    def test_archive_refuses_a_run_without_its_publication_receipt(self):
        with self.assertRaisesRegex(ValueError, '发布回执'):
            prune.archive(self.run, publication=self.root / 'missing.json')
        self.assertTrue((self.run / 'card-final.mtga').exists())

    def test_archive_state_keeps_the_newest_and_the_protected_base(self):
        state = self.root / 'state'
        for name in ('old-run', 'new-run', 'base-run'):
            run = state / 'runs' / name
            run.mkdir(parents=True)
            (run / 'report.json').write_text(name)
            (run / 'card-final.mtga').write_text('x' * 5000)
            import os
            os.utime(run, (0, {'old-run': 1, 'new-run': 3, 'base-run': 2}[name]))
        protected = {state / 'runs' / 'base-run'}
        archived = prune.archive_state(state, keep=1, protected=protected)
        # newest survives by count, the approved capture survives by protection,
        # only the stale run is archived.
        self.assertEqual([name for name, _ in archived], ['old-run'])
        self.assertEqual(sorted(p.name for p in (state / 'runs' / 'base-run').iterdir()), ['card-final.mtga', 'report.json'])
        self.assertTrue((state / 'runs' / 'new-run' / 'card-final.mtga').exists())
        self.assertFalse((state / 'runs' / 'old-run' / 'card-final.mtga').exists())

    def test_protected_runs_reads_registered_approvals(self):
        state = self.root / 'state'
        (state / 'base-approvals').mkdir(parents=True)
        (state / 'base-approvals' / 'x.json').write_text(json.dumps({'run': str(self.run)}))
        self.assertEqual(prune.protected_runs(state), {self.run.resolve()})


if __name__ == '__main__':
    unittest.main()
