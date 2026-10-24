"""Install an additional owner-private entry. Never overwrite existing entries/state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from private_delivery import fingerprint, regular_parents, require, PrivateGitHub
from private_receiver import SCHEMA, REPOSITORY, BRANCH, WORKFLOW, SHA, save, Receiver

FILES=('private_delivery.py','private_receiver.py','private_menu.py')

def install(config,state,desktop,python_path=None):
    state=Path(state);desktop=Path(desktop)
    regular_parents(state.parent);regular_parents(desktop.parent)
    require(not state.exists() and not state.is_symlink(),'INSTALL_STATE_ALREADY_EXISTS')
    require(not desktop.exists() and not desktop.is_symlink(),'INSTALL_ENTRY_ALREADY_EXISTS')
    require(config.get('schema')==SCHEMA and config.get('repository')==REPOSITORY and
            config.get('branch')==BRANCH and config.get('workflow_path')==WORKFLOW and
            SHA.fullmatch(config.get('reviewed_source_commit','')),'INSTALL_CONFIG_SCOPE')
    python_path=Path(python_path or sys.executable).resolve()
    pins={str(python_path):fingerprint(python_path)}
    created=False;entry_identity=None
    try:
        state.mkdir(mode=0o700);created=True
        for name in FILES:
            source=Path(__file__).resolve().parent/name;target=state/name
            expected=fingerprint(source);shutil.copyfile(source,target);target.chmod(0o600)
            require(fingerprint(target)==expected,'INSTALL_SOURCE_CHANGED')
            pins[str(target)]=expected
        save(state/'config.json',config);pins[str(state/'config.json')]=fingerprint(state/'config.json')
        checked=Receiver(state)
        try:PrivateGitHub(config['gh_path'],config['gh_fingerprint'])
        finally:checked.close()
        save(state/'installation-receipt.json',{'schema':'MTGA_PRIVATE_RECEIVER_INSTALL_V1',
             'file_fingerprints':pins,'python_path':str(python_path),
             'desktop_entry':str(desktop),'old_entries_changed':False,'game_changed':False})
        lines=['#!/bin/zsh','set -euo pipefail']
        for name,fp in pins.items():
            path=shlex.quote(name)
            lines.extend([f'if [[ ! -f {path} || -L {path} || "$(/usr/bin/shasum -a 256 {path} | /usr/bin/cut -d " " -f 1)" != {shlex.quote(fp["sha256"])} ]]; then',
                          '  print "私有云端入口文件发生变化，已停止。请保留原 r3 入口并联系维护者。"',
                          '  read "?按回车退出。"','  exit 1','fi'])
        lines.append('exec '+shlex.quote(str(python_path))+' -E -s '+shlex.quote(str(state/'private_menu.py'))+' --state '+shlex.quote(str(state)))
        raw=('\n'.join(lines)+'\n').encode()
        fd=os.open(desktop,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700)
        st=os.fstat(fd);entry_identity=(st.st_dev,st.st_ino)
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        require(fingerprint(desktop)=={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},'INSTALL_ENTRY_DRIFT')
        return {'state':str(state),'desktop':str(desktop),'entry':fingerprint(desktop),'pins':pins}
    except BaseException:
        # Never delete an existing entry; remove only files this invocation made.
        if entry_identity is not None and desktop.exists() and not desktop.is_symlink():
            st=desktop.stat()
            if (st.st_dev,st.st_ino)==entry_identity:desktop.unlink()
        if created:shutil.rmtree(state)
        raise

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path)
    p.add_argument('--state',required=True,type=Path);p.add_argument('--desktop',required=True,type=Path)
    a=p.parse_args();result=install(json.loads(a.config.read_text()),a.state,a.desktop)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
