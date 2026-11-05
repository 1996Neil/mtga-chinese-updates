"""First-run dependency setup is retryable and never used for ordinary commands."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LauncherBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tools = self.root / 'tools/chinese-maintainer'
        self.tools.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        source = Path(__file__).resolve().parents[2] / 'bin/mtga-cn'
        shutil.copyfile(source, self.bin / 'mtga-cn')
        (self.tools / 'requirements.lock').write_text('fixture==1\n')
        self.fake = self.root / 'fake-python'
        self.fake.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$*" >> "$MTGA_TEST_CALLS"\n'
            'if [ "$1" = "-m" ] && [ "${MTGA_TEST_FAIL_PIP:-}" = "1" ]; then exit 9; fi\n')
        self.fake.chmod(0o755)
        initializer = self.bin / 'python3.11'
        initializer.write_text(
            '#!/bin/sh\n'
            'mkdir -p "$3/bin"\n'
            'cp "$MTGA_TEST_FAKE_PYTHON" "$3/bin/python"\n'
            'chmod 755 "$3/bin/python"\n')
        initializer.chmod(0o755)
        self.calls = self.root / 'calls'
        self.env = dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin',
                        MTGA_TEST_FAKE_PYTHON=str(self.fake), MTGA_TEST_CALLS=str(self.calls))

    def invoke(self, *arguments, fail=False):
        env = dict(self.env)
        if fail:
            env['MTGA_TEST_FAIL_PIP'] = '1'
        return subprocess.run(['/bin/sh', str(self.bin / 'mtga-cn'), *arguments],
                              text=True, capture_output=True, env=env)

    def test_first_maintenance_prepares_once_and_reuses_receipt(self):
        self.assertEqual(self.invoke('maintain', '--online').returncode, 0)
        self.assertEqual(self.invoke('maintain', '--online').returncode, 0)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(sum(line.startswith('-m pip install') for line in calls), 1)
        self.assertEqual(sum(line.endswith('maintain --online') for line in calls), 2)
        self.assertTrue((self.tools / '.venv/requirements.receipt').exists())

    def test_failed_dependency_install_is_retried_without_false_ready_stamp(self):
        self.assertNotEqual(self.invoke('maintain', fail=True).returncode, 0)
        self.assertFalse((self.tools / '.venv/requirements.receipt').exists())
        self.assertEqual(self.invoke('maintain').returncode, 0)
        self.assertEqual(sum(line.startswith('-m pip install')
                             for line in self.calls.read_text().splitlines()), 2)

    def test_nonmaintenance_command_does_not_install_dependencies(self):
        self.assertNotEqual(self.invoke('update').returncode, 0)
        self.assertFalse((self.tools / '.venv').exists())
        self.assertFalse(self.calls.exists())


if __name__ == '__main__':
    unittest.main()
