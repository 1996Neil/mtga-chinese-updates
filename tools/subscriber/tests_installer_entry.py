"""Installer shell integration in an isolated cloud fixture; no actual user home or GUI."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from package_installer import fixed_text

class InstallerEntryTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory(prefix="mtga-installer-entry-")
        self.addCleanup(temp.cleanup)
        root=Path(temp.name).resolve()
        desktop=root/"desktop";desktop.mkdir()
        state=root/"state"
        stub=root/"mtga-cn"
        stub.write_text("""#!/bin/zsh
set -euo pipefail
while (( $# )); do
 if [[ "$1" == --state ]]; then shift; STATE="$1"; fi
 shift
done
mkdir -p "$STATE"
print '#!/bin/zsh' > "$STATE/MTGA 中文维护.command"
""")
        stub.chmod(0o755)
        fakeopen=root/"open"
        fakeopen.write_text('#!/bin/zsh\nexit 0\n');fakeopen.chmod(0o755)
        script=fixed_text()["安装 MTGA 中文维护.command"].decode()
        script=script.replace('$HOME/Library/Application Support/MTGAChineseSubscriber',str(state))
        script=script.replace('$HOME/Desktop',str(desktop))
        p=root/"install.command";p.write_text(script);p.chmod(0o755)
        return root,desktop,state,p

    def run_fixture(self,root,script):
        subprocess.run(["/bin/zsh","-n",str(script)],check=True,capture_output=True)
        return subprocess.run(["/bin/zsh",str(script)],input="\n",text=True,capture_output=True,
                              env={**os.environ,"PATH":str(root)+":"+os.environ["PATH"]},timeout=15)

    def test_fresh_entry_is_regular_executable_and_repeat_install_preserves_it(self):
        root,desktop,state,script=self.fixture()
        first=self.run_fixture(root,script)
        self.assertEqual(first.returncode,0,first.stderr)
        entry=desktop/"MTGA 订阅更新.command"
        self.assertTrue(entry.is_file())
        self.assertFalse(entry.is_symlink())
        self.assertTrue(os.access(entry,os.X_OK))
        before=entry.read_bytes()
        self.assertIn("桌面入口",first.stdout)
        self.assertEqual(self.run_fixture(root,script).returncode,0)
        self.assertEqual(before,entry.read_bytes())
        self.assertEqual([],list(desktop.glob(".mtga-subscriber-entry.*")))

    def test_existing_desktop_entry_is_replaced(self):
        root,desktop,state,script=self.fixture()
        entry=desktop/"MTGA 订阅更新.command"
        entry.write_bytes(b"old launcher")
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('MTGA 中文维护.command',entry.read_text())
        self.assertTrue(os.access(entry,os.X_OK))
        self.assertIn('桌面入口：',result.stdout)
        self.assertEqual([],list(desktop.glob('.mtga-subscriber-entry.*')))

    def test_unrelated_symlink_is_replaced_without_writing_its_target(self):
        root,desktop,state,script=self.fixture()
        outside=root/"outside";outside.write_bytes(b"unrelated")
        entry=desktop/"MTGA 订阅更新.command";entry.symlink_to(outside)
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(entry.is_symlink())
        self.assertTrue(entry.is_file())
        self.assertEqual(outside.read_bytes(),b"unrelated")
        self.assertIn('桌面入口：',result.stdout)

    def test_symlink_to_directory_is_replaced_without_entering_directory(self):
        root,desktop,state,script=self.fixture()
        outside=root/'outside';outside.mkdir()
        kept=outside/'keep.txt';kept.write_text('keep')
        entry=desktop/'MTGA 订阅更新.command';entry.symlink_to(outside,target_is_directory=True)
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(entry.is_symlink())
        self.assertTrue(entry.is_file())
        self.assertEqual(list(outside.iterdir()),[kept])
        self.assertEqual(kept.read_text(),'keep')

    def test_same_name_directory_uses_fallback_without_deleting_contents(self):
        root,desktop,state,script=self.fixture()
        entry=desktop/'MTGA 订阅更新.command';entry.mkdir()
        kept=entry/'keep.txt';kept.write_text('keep')
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(kept.read_text(),'keep')
        self.assertIn('未能建立桌面入口',result.stdout)
        self.assertEqual([],list(desktop.glob('.mtga-subscriber-entry.*')))

    def test_replace_failure_keeps_previous_entry_and_cleans_temporary_file(self):
        root,desktop,state,script=self.fixture()
        entry=desktop/'MTGA 订阅更新.command';entry.write_text('old entry')
        fail=root/'mv';fail.write_text('#!/bin/zsh\nexit 73\n');fail.chmod(0o755)
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(entry.read_text(),'old entry')
        self.assertIn('未能建立桌面入口',result.stdout)
        self.assertEqual([],list(desktop.glob('.mtga-subscriber-entry.*')))

    def test_desktop_temp_creation_failure_uses_installed_library_entry(self):
        root,desktop,state,script=self.fixture()
        fail=root/"mktemp";fail.write_text("#!/bin/zsh\nexit 73\n");fail.chmod(0o755)
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((state/"MTGA 中文维护.command").is_file())
        self.assertIn("未能建立桌面入口",result.stdout)
        self.assertFalse((desktop/"MTGA 订阅更新.command").exists())

    def test_desktop_chmod_failure_cleans_temp_and_uses_library_entry(self):
        root,desktop,state,script=self.fixture()
        fail=root/"chmod";fail.write_text("#!/bin/zsh\nexit 73\n");fail.chmod(0o755)
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((state/"MTGA 中文维护.command").is_file())
        self.assertIn("未能建立桌面入口",result.stdout)
        self.assertEqual([],list(desktop.glob(".mtga-subscriber-entry.*")))

    def test_existing_correct_legacy_symlink_is_replaced_and_reported(self):
        root,desktop,state,script=self.fixture()
        state.mkdir()
        original=state/"MTGA 中文维护.command";original.write_text("#!/bin/zsh\n")
        entry=desktop/"MTGA 订阅更新.command";entry.symlink_to(original)
        result=self.run_fixture(root,script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(entry.is_symlink())
        self.assertIn("MTGA 中文维护.command",entry.read_text())
        self.assertIn("桌面入口：",result.stdout)
