"""Upgrade this project's installed reader, keeping the previous app recoverable."""
from pathlib import Path
import datetime
import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile

project = Path(__file__).resolve().parents[2]
root = project / "compatibility/card-language-2026-10-07"
source = root / "MTGA 中文读牌.app"
target = Path.home() / "Applications/MTGA 中文读牌.app"
receipt_path = root / "installation.json"
receipt = json.loads(receipt_path.read_text())
if receipt.get("app") != str(target) or receipt.get("gameFilesModified") is not False:
    raise SystemExit("Installed app receipt does not identify this reader.")
for app in [source, target]:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "local.neil.mtga.cardreader" or info.get("CFBundleExecutable") != "CardOverlay":
        raise SystemExit(f"Unexpected app identity: {app}")
    subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
processes = subprocess.check_output(["ps", "-axo", "command="], text=True).splitlines()
if any(line.startswith(str(target / "Contents/MacOS/CardOverlay")) for line in processes):
    raise SystemExit("Exit the installed reader before upgrading.")
subprocess.run([str(source / "Contents/MacOS/CardOverlay"), "--self-test", "--data",
                str(source / "Contents/Resources/cards.json")], check=True)
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
backup = root / "revisions" / (stamp + "-installed-app-before") / target.name
backup.parent.mkdir(parents=True, exist_ok=False)
shutil.copytree(target, backup)
subprocess.run(["codesign", "--verify", "--strict", str(backup)], check=True)
old_requirement = subprocess.run(["codesign", "-d", "-r-", str(target)], capture_output=True, text=True, check=True).stderr
new_requirement = subprocess.run(["codesign", "-d", "-r-", str(source)], capture_output=True, text=True, check=True).stderr
old_hash = hashlib.sha256((target / "Contents/MacOS/CardOverlay").read_bytes()).hexdigest()
with tempfile.TemporaryDirectory(prefix=".mtga-reader-upgrade-", dir=target.parent) as temp:
    staged = Path(temp) / target.name
    displaced = Path(temp) / "previous.app"
    shutil.copytree(source, staged)
    subprocess.run(["codesign", "--verify", "--strict", str(staged)], check=True)
    target.rename(displaced)
    try:
        staged.rename(target)
        subprocess.run(["codesign", "--verify", "--strict", str(target)], check=True)
    except BaseException:
        if target.exists():
            failed = backup.parent / "failed-candidate.app"
            target.rename(failed)
        displaced.rename(target)
        raise
info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
receipt["lastUpgrade"] = {"version": info["CFBundleShortVersionString"], "backup": str(backup),
    "oldBinarySHA256": old_hash,
    "newBinarySHA256": hashlib.sha256((target / "Contents/MacOS/CardOverlay").read_bytes()).hexdigest(),
    "externalCardsAndDesktopEntriesPreserved": True,
    "screenRecordingReregistrationRequired": "cdhash" in old_requirement and "cdhash" in new_requirement and old_hash != hashlib.sha256((target / "Contents/MacOS/CardOverlay").read_bytes()).hexdigest()}
receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(receipt["lastUpgrade"], ensure_ascii=False))

if receipt["lastUpgrade"]["screenRecordingReregistrationRequired"]:
    print("此版本的临时签名已变化。请在系统设置移除旧的“MTGA 中文读牌”条目，重新添加当前 App 并开启权限，然后重开读牌。开关显示已开不等于新版本授权成功。")
