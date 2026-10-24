"""Portable source entry points; fixtures never invoke a game or installer."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SourcePortabilityTests(unittest.TestCase):
    def test_cli_uses_its_own_checkout_with_spaces_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp).resolve() / 'fork with spaces'
            (checkout/'bin').mkdir(parents=True)
            envdir = checkout/'tools/chinese-maintainer/.venv/bin'
            envdir.mkdir(parents=True)
            shutil.copy2(ROOT/'bin/mtga-cn', checkout/'bin/mtga-cn')
            fake = envdir/'python'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            fake.chmod(0o700)
            result = subprocess.run(['sh',str(checkout/'bin/mtga-cn'),'guide','a b'],
                                    cwd=temp, text=True,capture_output=True,check=True)
            lines = result.stdout.splitlines()
            self.assertEqual(Path(lines[0]).resolve(),checkout/'tools/chinese-maintainer/cli.py')
            self.assertEqual(lines[1:],['guide','a b'])

    def test_environment_overrides_refer_to_own_inputs_not_author_home(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            overrides = {'MTGA_GAME_PATH':str(base/'SteamLibrary/MTGA'),
                         'MTGA_BACKUP_ROOT':str(base/'backups'),
                         'MTGA_HISTORY_ROOT':str(base/'history')}
            script = 'import json,common;print(json.dumps([str(common.GAME),str(common.BACKUPS),str(common.HISTORY)]))'
            result = subprocess.run([sys.executable,'-c',script],cwd=ROOT/'tools/chinese-maintainer',
                                    env={**os.environ,**overrides},capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout),list(overrides.values()))
            self.assertFalse((base/'SteamLibrary').exists())

    def test_missing_virtualenv_reports_setup_instead_of_using_another_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp).resolve()
            (checkout/'bin').mkdir()
            shutil.copy2(ROOT/'bin/mtga-cn',checkout/'bin/mtga-cn')
            result = subprocess.run(['sh',str(checkout/'bin/mtga-cn'),'update'],
                                    capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertIn('docs/DEVELOPMENT.md',result.stderr)
            self.assertFalse((checkout/'tools').exists())
