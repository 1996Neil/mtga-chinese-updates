#!/usr/bin/env python3
"""Create a local installer folder, without upload or publishing."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
from publisher import fingerprint, dump


def fixed_text(local_feed=False):
    state_name = 'MTGAChineseSubscriber-LocalTest' if local_feed else 'MTGAChineseSubscriber'
    installer = '''#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
STATE="$HOME/Library/Application Support/STATE_NAME"
"$ROOT/mtga-cn" setup --profile "$ROOT/profile.json" --state "$STATE"
SHORTCUT="$HOME/Desktop/MTGA 订阅更新.command"
EXPECTED_ENTRY="$STATE/MTGA 中文维护.command"
create_desktop_entry() {
  [[ -d "$HOME/Desktop" ]] || return 1
  SHORTCUT_TMP="$(mktemp "$HOME/Desktop/.mtga-subscriber-entry.XXXXXX")" || return 1
  if ! cat > "$SHORTCUT_TMP" <<'LAUNCHER'
#!/bin/zsh
set -euo pipefail
ENTRY="$HOME/Library/Application Support/STATE_NAME/MTGA 中文维护.command"
if [[ ! -f "$ENTRY" ]]; then
  print '订阅工具入口缺失，请重新运行安装包；游戏文件未修改。'
  exit 1
fi
exec /bin/zsh "$ENTRY"
LAUNCHER
  then
    rm -f "$SHORTCUT_TMP"
    return 1
  fi
  if ! chmod 755 "$SHORTCUT_TMP"; then
    rm -f "$SHORTCUT_TMP"
    return 1
  fi
  ENTRY_READY=0
  if [[ ! -e "$SHORTCUT" && ! -L "$SHORTCUT" ]]; then
    if ln "$SHORTCUT_TMP" "$SHORTCUT"; then ENTRY_READY=1; fi
  elif [[ -L "$SHORTCUT" && "${SHORTCUT:A}" == "${EXPECTED_ENTRY:A}" ]]; then
    ENTRY_READY=1
  elif [[ -f "$SHORTCUT" && ! -L "$SHORTCUT" ]] && cmp -s "$SHORTCUT_TMP" "$SHORTCUT"; then
    ENTRY_READY=1
  else
    print '桌面存在其他同名文件，已保留原文件；没有覆盖。'
  fi
  rm -f "$SHORTCUT_TMP"
  [[ "$ENTRY_READY" == 1 ]]
}
print "安装目录：$STATE"
if create_desktop_entry; then
  print "桌面入口：$SHORTCUT"
  open -R "$SHORTCUT" || true
else
  print "未能建立桌面入口，请打开：$STATE/MTGA 中文维护.command"
  open -R "$STATE/MTGA 中文维护.command" || true
fi
print '此候选尚未完成 Developer ID 签名与 Apple 公证；没有通过干净下载的 Gatekeeper 验收。'
read -r '?按回车关闭。' || true
'''.replace('STATE_NAME',state_name)
    description = ('本包是本地测试演示，安装后请保留此文件夹；它内置的测试源不会自动收到网络更新。正式分发时需用固定 HTTPS 更新源重新打包。\n' if local_feed else '安装后由固定 HTTPS 更新源获取维护者发布的适配版本。\n')
    readme = description + '''
支持 macOS 13 及以上、Steam MTGA。二进制含 Apple Silicon 与 Intel；当前真实资源验证只覆盖 Apple Silicon。
双击“安装 MTGA 中文维护.command”，安装完成后会显示并定位桌面的“MTGA 订阅更新.command”；桌面创建失败时会明确显示资源库中的备用入口。
先检查更新，再预演；退出游戏后才可选择应用更新。恢复会回到上次更新之前的实际状态。
无须 Codex、Python、Git 或管理员权限。首版按需检查更新，不自动后台改写游戏。
当前安装包仅做本机 ad-hoc 签名，尚未完成 Developer ID 签名、公证或干净下载 Gatekeeper 验收。若 macOS 拦截，停止使用，不要关闭系统安全检查。
测试渠道的实际验收范围以发布说明为准，未测范围保持未验证。
'''
    return {'安装 MTGA 中文维护.command':installer.encode(), '先读我.txt':readme.encode()}


def package(client, profile_path, out, local_feed=None):
    out = Path(out).resolve(); profile = json.loads(Path(profile_path).read_text())
    if out.exists(): raise ValueError('拒绝覆盖现有安装包。')
    if local_feed:
        if profile['channel'] != 'testing': raise ValueError('内置离线源仅用于 testing。')
        profile['feed'] = './feed'
    elif not profile['feed'].startswith('https://'):
        raise ValueError('可分发安装包需要维护者固定 HTTPS 源；本地演示须显式指定 --local-feed。')
    out.mkdir(parents=True)
    shutil.copy2(client, out/'mtga-cn')
    if local_feed:
        (out/'feed').mkdir()
        for path in Path(local_feed).iterdir():
            if path.is_file() and not path.is_symlink() and (path.suffix in ('.delta', '.full') or path.name.startswith('index-') or path.name.startswith('2026.')):
                shutil.copy2(path, out/'feed'/path.name)
    dump(out/'profile.json', profile)
    for name, data in fixed_text(bool(local_feed)).items():
        (out/name).write_bytes(data)
    (out/'安装 MTGA 中文维护.command').chmod(0o755)
    dump(out/'SHA256.json', {p.name:fingerprint(p) for p in out.iterdir() if p.is_file()})
    with zipfile.ZipFile(str(out)+'.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out.rglob('*')):
            if path.is_file(): archive.write(path, out.name+'/'+str(path.relative_to(out)))
    return out

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client',type=Path,required=True);p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--local-feed',type=Path)
    a=p.parse_args();print(package(a.client,a.profile,a.output,a.local_feed))
