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
print 'MTGA 中文维护'
print '最新工具 / 使用说明 / 反馈：https://github.com/1996Neil/mtga-chinese-updates'
"$ROOT/mtga-cn" setup --profile "$ROOT/profile.json" --state "$STATE" > /dev/null
print '安装完成。'
SHORTCUT="$HOME/Desktop/MTGA 订阅更新.command"
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
  # Replace this exact desktop entry, including an old or broken symlink.
  # -h prevents following a destination symlink to a directory on macOS.
  if [[ -d "$SHORTCUT" && ! -L "$SHORTCUT" ]]; then
    print '桌面同名位置是文件夹，无法替换；请使用下面的备用入口。'
    rm -f "$SHORTCUT_TMP"
    return 1
  fi
  if mv -fh "$SHORTCUT_TMP" "$SHORTCUT"; then return 0; fi
  rm -f "$SHORTCUT_TMP"
  return 1
}
if create_desktop_entry; then
  print "桌面入口：$SHORTCUT"
  open -R "$SHORTCUT" || true
else
  print "未能建立桌面入口，请打开：$STATE/MTGA 中文维护.command"
  open -R "$STATE/MTGA 中文维护.command" || true
fi
print '游戏语言必须设为 English（英语），安装或撤销汉化更新前必须完全退出 MTGA。'
print '以后更新汉化：打开桌面入口，依次选 2 → 3 → 4。'
print '收到转发的安装包也可使用；若提示工具需要升级，请到项目主页下载。'
print 'INSTALLER_LIFETIME'
read -r '?按回车关闭。' || true
'''.replace('STATE_NAME',state_name).replace('INSTALLER_LIFETIME', '离线演示包：请保留解压文件夹，删除后无法读取内置资源。' if local_feed else '安装完成后，可以删除下载的 ZIP 和解压出的安装包文件夹；以后只用桌面入口。')
    lifetime = ('这是离线演示包，安装后仍须保留解压文件夹；它不会收到网上的新汉化。' if local_feed else
                '安装完成后，下载的 ZIP 和解压出的安装包文件夹都可以删除。工具已装到本机，以后只用桌面入口。')
    readme = """MTGA 中文维护

项目主页 / 最新工具 / 使用说明 / 反馈：
https://github.com/1996Neil/mtga-chinese-updates
即使安装包由朋友转发，也请认准这个地址。汉化内容可在桌面工具里更新；
只有提示需要升级工具时，才到项目主页重新下载安装。想收新版邮件，在主页选 Watch → Custom → Releases。

第一次用
1. 双击“安装 MTGA 中文维护.command”，等出现“安装完成”。桌面同名入口会自动替换。
2. 等 Steam 和游戏内下载完成，把游戏语言设为 English（英语），然后完全退出 MTGA。
3. 打开桌面“MTGA 订阅更新.command”，依次选：
   2 检查更新 → 3 下载并检查（不改游戏）→ 4 安装或更新汉化。
4. 看到“更新已应用”再开游戏，语言一直保持 English。
输入数字后按回车；操作结束后按回车返回菜单，0 退出。

以后用
完成游戏下载 → 语言保持 English → 退出游戏 → 桌面工具 → 2 → 3 → 4。
更新后有问题或安装中断：退出游戏，选 5“撤销上次更新”。
恢复到更新前实际状态，不一定是官方原版；Steam 已更新游戏时，旧备份可能无法恢复。
下载失败可重选 3；没有适配版本就等发布。安装或恢复出错，请保留完整提示到主页反馈。

安装包能删吗？
LIFETIME_TEXT
保留桌面入口，不要手动删除工具的安装目录（里面有备份和恢复记录）：
~/Library/Application Support/STATE_NAME

macOS 阻止打开时
仅对确认来自上述项目的文件，在“系统设置 → 隐私与安全性”里找到该文件，点“仍要打开”。
若提示会损害电脑或文件已损坏，请停止并到主页反馈，不要强行放行。
图文操作步骤也在项目主页。
""".replace('LIFETIME_TEXT', lifetime).replace('STATE_NAME', state_name)
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
