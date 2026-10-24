"""Install only the locally built reader app and its two Finder entry points."""
from pathlib import Path
import json
import plistlib
import shlex
import shutil
import subprocess
import tempfile

project = Path(__file__).resolve().parents[2]
source = project / "compatibility/card-language-2026-10-07/MTGA 中文读牌.app"
target = Path.home() / "Applications/MTGA 中文读牌.app"
data_root = Path.home() / "Library/Application Support/MTGA Card Reader"
desktop = Path.home() / "Desktop"
if not source.is_dir():
    raise SystemExit("Run tools/card-overlay/build.sh first.")
subprocess.run(["codesign", "--verify", "--strict", str(source)], check=True)
subprocess.run([str(source / "Contents/MacOS/CardOverlay"), "--self-test", "--data",
                str(source / "Contents/Resources/cards.json")], check=True)
if target.exists():
    raise SystemExit(f"Refusing to overwrite an existing app: {target}")
launch = desktop / "MTGA 中文读牌.command"
update = desktop / "更新浮窗卡牌.command"
if launch.exists() or update.exists():
    raise SystemExit("Desktop entry already exists; inspect it before replacing.")
target.parent.mkdir(parents=True, exist_ok=True)
data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
with tempfile.TemporaryDirectory(prefix=".mtga-reader-", dir=target.parent) as temp:
    staged = Path(temp) / target.name
    shutil.copytree(source, staged)
    staged.rename(target)
data = data_root / "cards.json"
staged_data = data_root / ".cards.installing"
shutil.copy2(source / "Contents/Resources/cards.json", staged_data)
staged_data.chmod(0o600)
staged_data.replace(data)
launch.write_text("#!/bin/zsh\nset -e\nopen " + shlex.quote(str(target)) + "\n")
exporter = project / "tools/card-overlay/export_cards.py"
update.write_text("#!/bin/zsh\nset -e\npython3 " + shlex.quote(str(exporter)) + " --output " +
                  shlex.quote(str(data)) + '\nprint -r -- "卡牌资料已更新。请退出并重新打开 MTGA 中文读牌。"\n')
for path in [launch, update]:
    path.chmod(0o700)
receipt = {"app": str(target), "cards": str(data), "launch": str(launch), "update": str(update),
           "gameFilesModified": False, "startsAtLogin": False}
(project / "compatibility/card-language-2026-10-07/installation.json").write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(receipt, ensure_ascii=False))
