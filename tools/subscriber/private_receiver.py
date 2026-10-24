#!/usr/bin/env python3
"""Owner-private cloud candidate receiver. Executes only an already pinned native client."""
import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from private_delivery import (DeliveryError, PrivateGitHub, REPOSITORY, fingerprint,
                              regular_parents, require, valid_fingerprint,
                              extract_data_archive)

LAYERS = {'card','client','resources','bootstrap_fonts','font_bundle'}
SCHEMA = 'MTGA_PRIVATE_RECEIVER_CONFIG_V1'
BRANCH = 'codex/cloud-maintenance-unattended-20261017'
WORKFLOW = '.github/workflows/cloud-maintenance-unattended.yml'
SHA = re.compile(r'^[0-9a-f]{40}$')

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

def read_json(path):
    path=Path(path)
    regular_parents(path)
    require(path.is_file() and not path.is_symlink(),'LOCAL_JSON_REGULAR_FILE')
    require(path.stat().st_size <= 3*1024*1024,'LOCAL_JSON_BOUND')
    return json.loads(path.read_text())

def save(path,value):
    path=Path(path);regular_parents(path.parent)
    fd,tmp=tempfile.mkstemp(prefix='.private-delivery-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as f:f.write(canonical(value)+b'\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def remove_durable(path):
    path.unlink()
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def decoded_contents(data):
    require(data.get('encoding')=='base64' and isinstance(data.get('content'),str),'GITHUB_CONTENT_ENCODING')
    try:return base64.b64decode(''.join(data['content'].split()),validate=True)
    except ValueError:raise DeliveryError('GITHUB_CONTENT_INVALID') from None

def positive_id(value,label):
    require(isinstance(value,(int,str)) and type(value) is not bool and
            re.fullmatch(r'[1-9][0-9]*',str(value)) is not None,label)
    return int(value)

def same_game_path(value,root):
    # Foundation may spell macOS's /private/var alias as /var.
    return isinstance(value,str) and Path(value).is_absolute() and Path(value).resolve()==root

def validate_outputs(outputs):
    require(isinstance(outputs,dict) and set(outputs)==LAYERS,'FIVE_OUTPUTS_REQUIRED')
    for layer,row in outputs.items():
        require(set(row)=={'relative','sha256','bytes'},'OUTPUT_FIELDS')
        valid_fingerprint({k:row[k] for k in ('sha256','bytes')},1024*1024*1024)
        rel=row['relative']
        allowed = bool(layer=='card' and re.fullmatch(r'MTGA_Data/Downloads/Raw/Raw_CardDatabase_[0-9a-f]{32}\.mtga',rel))
        allowed |= bool(layer=='client' and re.fullmatch(r'MTGA_Data/Downloads/Raw/Raw_ClientLocalization_[0-9a-f]{32}\.mtga',rel))
        allowed |= layer=='resources' and rel=='MTGA.app/Contents/Resources/Data/resources.assets'
        allowed |= layer=='bootstrap_fonts' and rel=='MTGA.app/Contents/Resources/Data/sharedassets0.assets'
        allowed |= bool(layer=='font_bundle' and re.fullmatch(r'MTGA_Data/Downloads/AssetBundle/Fonts_[0-9a-f-]+\.mtga',rel))
        require(allowed,'OUTPUT_PATH_SCOPE')
    return outputs

def validate_descriptor(data,config):
    require(data.get('schema')=='MTGA_PRIVATE_DELIVERY_V1' and data.get('phase')=='FINALIZED','DELIVERY_NOT_FINALIZED')
    require(data.get('repository')==REPOSITORY and data.get('branch')==BRANCH and
            data.get('workflow_path')==WORKFLOW,'DELIVERY_SCOPE')
    require(data.get('workflow_source_commit')==config['reviewed_source_commit'] and
            SHA.fullmatch(data.get('execution_source_commit','')),'DELIVERY_SOURCE')
    require(type(data.get('run_attempt')) is int and data['run_attempt']==1,'DELIVERY_ATTEMPT')
    positive_id(data.get('run_id'),'DELIVERY_RUN_ID')
    artifact=data['artifact'];positive_id(artifact['id'],'DELIVERY_ARTIFACT_ID')
    valid_fingerprint({k:artifact[k] for k in ('bytes','sha256')})
    require(artifact['name']=='mtga-private-native-data-'+str(data['run_id']),'DELIVERY_ARTIFACT_NAME')
    target=data['target']
    require(target.get('app_id')==2141910 and type(target['app_id']) is int and
            target.get('depot_id')==2141913 and type(target['depot_id']) is int and
            target.get('steam_branch')=='public' and target.get('platform')=='macos',
            'DELIVERY_TARGET')
    require(re.fullmatch(r'2026\.[0-9.]+',target.get('client_version','')) and
            re.fullmatch(r'[0-9]+',str(target.get('steam_build',''))) and
            isinstance(target.get('resource_generation'),str),'DELIVERY_VERSION')
    outputs=validate_outputs(data['five_outputs'])
    identity=hashlib.sha256(canonical({'target':target,'five_outputs':outputs})).hexdigest()
    require(data.get('candidate_identity')==identity,'CANDIDATE_IDENTITY')
    report=data['report']
    require(SHA.fullmatch(report.get('commit','')) and
            report.get('path')=='cloud-maintenance/results/unattended-'+str(data['run_id'])+'-1.json' and
            re.fullmatch(r'[0-9a-f]{64}',report.get('sha256','')),'DELIVERY_REPORT')
    release=target['client_version']+'-'+str(target['steam_build'])+'-r3.json'
    names={layer+'.delta' for layer in LAYERS}|{'profile.json','index-testing.json',release,'delivery.json','report.json'}
    require(type(data.get('archive_members')) is list and len(data['archive_members'])==10 and
            set(data['archive_members'])==names and set(data['member_fingerprints'])==names,
            'DELIVERY_MEMBERS')
    for value in data['member_fingerprints'].values():valid_fingerprint(value)
    try:key=base64.b64decode(data['public_key'],validate=True)
    except ValueError:raise DeliveryError('DELIVERY_PUBLIC_KEY') from None
    require(len(key)==32,'DELIVERY_PUBLIC_KEY')
    require(data.get('effects')=={'install':False,'publish':False,'schedule':False},'CLOUD_EFFECT_SCOPE')
    return release

def validate_producer(data,run,artifact,tree,config):
    require(run.get('id')==int(data['run_id']) and run.get('status')=='completed' and
            run.get('conclusion')=='success' and run.get('event')=='push' and
            run.get('head_branch')==BRANCH and run.get('head_sha')==data['execution_source_commit'] and
            run.get('path')==WORKFLOW and run.get('run_attempt')==1,'PRODUCER_RUN_NOT_VERIFIED')
    a=data['artifact'];origin=artifact.get('workflow_run',{})
    require(artifact.get('id')==int(a['id']) and artifact.get('name')==a['name'] and
            artifact.get('expired') is False and artifact.get('size_in_bytes')==a['bytes'] and
            artifact.get('digest')=='sha256:'+a['sha256'] and
            origin.get('id')==int(data['run_id']) and origin.get('head_sha')==data['execution_source_commit'],
            'PRODUCER_ARTIFACT_NOT_VERIFIED')
    require(tree.get('truncated') is False,'PRODUCER_TREE_TRUNCATED')
    actual={e['path']:e['sha'] for e in tree['tree'] if e['type']=='blob'}
    pins=config['reviewed_blobs']
    require(isinstance(pins,dict) and len(pins)>=4 and WORKFLOW in pins and
            all(SHA.fullmatch(v) and actual.get(p)==v for p,v in pins.items()),
            'PRODUCER_CODE_DRIFT')

def current_game(library):
    library=Path(library);regular_parents(library)
    manifest=library/'steamapps/appmanifest_2141910.acf'
    regular_parents(manifest)
    require(manifest.stat().st_size<1024*1024,'STEAM_MANIFEST_BOUND')
    text=manifest.read_text()
    def vdf(key):
        found=re.findall(r'"'+re.escape(key)+r'"\s*"([^"]*)"',text)
        require(len(found)==1,'STEAM_FIELD_'+key);return found[0]
    require(vdf('appid')=='2141910' and vdf('StateFlags')=='4','STEAM_NOT_READY')
    directory=vdf('installdir');require(directory=='MTGA','STEAM_DIRECTORY')
    root=library/'steamapps/common'/directory;regular_parents(root)
    info=root/'MTGA.app/Contents/Info.plist'
    regular_parents(info)
    require(info.stat().st_size<1024*1024,'GAME_INFO_BOUND')
    return root,plistlib.loads(info.read_bytes())['CFBundleShortVersionString'],vdf('buildid')

class Receiver:
    def __init__(self,state):
        self.state=Path(state);regular_parents(self.state)
        require(self.state.is_dir(),'RECEIVER_STATE_MISSING')
        self.config=read_json(self.state/'config.json')
        c=self.config
        require(c.get('schema')==SCHEMA and c.get('repository')==REPOSITORY and
                c.get('branch')==BRANCH and c.get('workflow_path')==WORKFLOW and
                SHA.fullmatch(c.get('reviewed_source_commit','')),'RECEIVER_CONFIG_SCOPE')
        self.native=Path(c['native_path'])
        require(fingerprint(self.native)==valid_fingerprint(c['native_fingerprint']),'NATIVE_CLIENT_IDENTITY')
        self.github=None
        lock=self.state/'receiver.lock'
        require(not lock.is_symlink(),'RECEIVER_LOCK_SYMLINK')
        self.lock=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:os.close(self.lock);raise DeliveryError('RECEIVER_BUSY') from None

    def close(self):
        fcntl.flock(self.lock,fcntl.LOCK_UN);os.close(self.lock)

    def native_command(self,command,native_state=None,extra=()):
        require(command in {'verify-envelope','setup','check','preview','update','recover','status'},
                'NATIVE_COMMAND_SCOPE')
        require(fingerprint(self.native)==self.config['native_fingerprint'],'NATIVE_CLIENT_CHANGED')
        args=[str(self.native),command,*map(str,extra)]
        if native_state is not None:
            args+=['--state',str(native_state),'--steam-library',self.config['steam_library']]
        try:result=subprocess.run(args,capture_output=True,text=True,timeout=1200)
        except subprocess.TimeoutExpired:raise DeliveryError('NATIVE_TIMEOUT_PRESERVE_STATE_AND_RECOVER') from None
        require(result.returncode==0,'NATIVE_'+command.upper()+'_FAILED: '+(result.stdout+result.stderr)[:3000])
        require(fingerprint(self.native)==self.config['native_fingerprint'],'NATIVE_CLIENT_CHANGED')
        if command not in {'setup','verify-envelope'}:print(result.stdout,end='',flush=True)
        return result.stdout

    def pull(self):
        if self.github is None:
            self.github=PrivateGitHub(self.config['gh_path'],self.config['gh_fingerprint'])
        self.github.repository()
        raw=decoded_contents(self.github.contents('delivery/latest.json',BRANCH))
        data=json.loads(raw);release_name=validate_descriptor(data,self.config)
        run=self.github.json('actions/runs/'+str(data['run_id']))
        artifact=self.github.json('actions/artifacts/'+str(data['artifact']['id']))
        tree=self.github.json('git/trees/'+data['execution_source_commit']+'?recursive=1')
        validate_producer(data,run,artifact,tree,self.config)
        report=decoded_contents(self.github.contents(data['report']['path'],data['report']['commit']))
        require(hashlib.sha256(report).hexdigest()==data['report']['sha256'],'FINAL_REPORT_FINGERPRINT')
        a=data['artifact'];sha=a['sha256']
        cache=self.state/'cache';cache.mkdir(mode=0o700,exist_ok=True);regular_parents(cache)
        archive=cache/(sha+'.zip')
        expected={k:a[k] for k in ('bytes','sha256')}
        if not archive.exists():
            print('正在从私有云端下载并核验汉化数据包；完成前不会修改游戏。',flush=True)
            self.github.archive(int(a['id']),archive,expected)
        else:require(fingerprint(archive)==expected,'CACHED_ARCHIVE_DRIFT')
        feed=cache/sha
        if not feed.exists():extract_data_archive(archive,feed,data['member_fingerprints'])
        else:
            regular_parents(feed)
            require(set(p.name for p in feed.iterdir())==set(data['archive_members']),'CACHED_FEED_MEMBERS')
            for name,expected in data['member_fingerprints'].items():
                require(fingerprint(feed/name)==expected,'CACHED_FEED_DRIFT')
        profile=read_json(feed/'profile.json')
        require(profile=={'schema':'MTGA_CN_PROFILE_V1','feed':'./feed',
                          'public_key':data['public_key'],'channel':'testing'},'DATA_PROFILE')
        pubkey=cache/(sha+'.public-key.txt')
        if not pubkey.exists():
            fd=os.open(pubkey,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as f:f.write(data['public_key']+'\n')
        regular_parents(pubkey)
        require(pubkey.is_file() and not pubkey.is_symlink() and
                pubkey.read_text()==data['public_key']+'\n','CACHED_PUBLIC_KEY_DRIFT')
        for name in ('index-testing.json',release_name):
            self.native_command('verify-envelope',extra=('--input',feed/name,'--key',pubkey))
        release=json.loads(base64.b64decode(read_json(feed/release_name)['payload'],validate=True))
        require(release.get('client_version')==data['target']['client_version'] and
                release.get('steam_build')==str(data['target']['steam_build']) and
                release.get('id')+'.json'==release_name and release.get('revision')==3 and
                release.get('acceptance',{}).get('status')=='testing','SIGNED_RELEASE_IDENTITY')
        rows=release.get('resources',[])
        require(len(rows)==5 and {r['layer'] for r in rows}==LAYERS,'SIGNED_RESOURCE_SET')
        for row in rows:
            expected=data['five_outputs'][row['layer']]
            require(row['relative']==expected['relative'] and
                    row['output']=={k:expected[k] for k in ('bytes','sha256')} and
                    set(row['patch'])=={'name','fingerprint'} and
                    row['patch']['name']==row['layer']+'.delta' and
                    row['patch']['fingerprint']==data['member_fingerprints'][row['patch']['name']],
                    'SIGNED_RESOURCE_OR_PATCH_MISMATCH')
        root,version,build=current_game(self.config['steam_library'])
        require((version,build)==(data['target']['client_version'],str(data['target']['steam_build'])),
                'CANDIDATE_DOES_NOT_MATCH_CURRENT_GAME')
        states=self.state/'native-states';states.mkdir(mode=0o700,exist_ok=True)
        native_state=states/sha
        local_profile=cache/(sha+'.profile.json')
        resolved=dict(profile,feed=str(feed))
        if local_profile.exists():require(read_json(local_profile)==resolved,'LOCAL_PROFILE_DRIFT')
        else:save(local_profile,resolved)
        if not native_state.exists():
            self.native_command('setup',extra=('--profile',local_profile,'--state',native_state))
        else:
            require(read_json(native_state/'profile.json')==resolved,'NATIVE_STATE_PROFILE_DRIFT')
        self.native_command('check',native_state)
        result={'artifact_sha256':sha,'candidate_identity':data['candidate_identity'],
                'native_state':str(native_state),'descriptor':data}
        save(self.state/'selected.json',result)
        print('私有云端数据包与构建来源核验通过。',flush=True)
        return result

    def execute(self,command):
        require(command in {'check','preview','update','rollback','status'},'RECEIVER_COMMAND')
        pending=self.state/'pending.json'
        installed=self.state/'installed.json'
        if command=='status':
            pointer=pending if pending.exists() else installed
            if not pointer.exists():
                print('此云端入口尚无安装记录；当前 r3 和原恢复入口保持不变。')
                return
            active=read_json(pointer)
            native_state=self.bound_state(active)
            if pointer==pending:print('上一次云端安装尚未确认完成，请选择恢复；不会自动开始下一次安装。')
            self.native_command('status',native_state)
            return
        if command=='rollback':
            pointer=pending if pending.exists() else installed
            require(pointer.exists(),'NO_PRIVATE_CLOUD_INSTALLATION')
            active=read_json(pointer);native_state=self.bound_state(active)
            journal_path=native_state/'installation.json'
            validate_descriptor(active['descriptor'],self.config)
            outputs=validate_outputs(active['descriptor']['five_outputs'])
            archive=self.state/'cache'/(active['artifact_sha256']+'.zip')
            require(fingerprint(archive)=={k:active['descriptor']['artifact'][k] for k in ('bytes','sha256')},
                    'RECOVERY_ARCHIVE_DRIFT')
            release_name=active['descriptor']['target']['client_version']+'-'+str(active['descriptor']['target']['steam_build'])+'-r3.json'
            feed=self.state/'cache'/active['artifact_sha256']
            for name,expected in active['descriptor']['member_fingerprints'].items():
                require(fingerprint(feed/name)==expected,'RECOVERY_FEED_DRIFT')
            release=json.loads(base64.b64decode(read_json(feed/release_name)['payload'],validate=True))
            signed_rows={r['layer']:r for r in release['resources']}
            if not journal_path.exists():
                # The pinned native client writes its journal before the first game
                # write. An interrupted intent can be cleared only with exact known
                # resources and every protected file still present and unchanged.
                require(pointer==pending,'INSTALLED_JOURNAL_MISSING_PRESERVE_STATE')
                root,version,build=current_game(self.config['steam_library'])
                require((version,build)==(release['client_version'],release['steam_build']),
                        'ROLLBACK_GAME_IDENTITY')
                require(set(signed_rows)==LAYERS,'RECOVERY_SIGNED_ROWS')
                for layer,row in signed_rows.items():
                    require(row['relative']==outputs[layer]['relative'] and
                            row['output']=={k:outputs[layer][k] for k in ('bytes','sha256')},
                            'RECOVERY_RESOURCE_SCOPE')
                    require(fingerprint(root/row['relative']) in (row['input'],row['output']),
                            'RECOVERY_WITHOUT_JOURNAL_UNKNOWN_RESOURCE')
                for row in release['protected_files']:
                    require(fingerprint(root/row['relative'])==row['fingerprint'],
                            'RECOVERY_WITHOUT_JOURNAL_PROTECTED_DRIFT')
                remove_durable(pending)
                print('已核验中断发生在写入游戏之前，已清除待处理记录；游戏文件未修改。')
                return
            journal=read_json(journal_path)
            require(journal.get('protected_files')==release['protected_files'],'RECOVERY_PROTECTED_SET_DRIFT')
            root,version,build=current_game(self.config['steam_library'])
            require(same_game_path(journal.get('game'),root) and
                    (journal.get('client_version'),journal.get('steam_build'))==(version,build),
                    'ROLLBACK_GAME_IDENTITY')
            require(len(journal.get('rows',[]))==5 and {r['layer'] for r in journal['rows']}==LAYERS,
                    'ROLLBACK_FIVE_ROWS')
            def guard(restored=False):
                require(current_game(self.config['steam_library'])==(root,version,build),'ROLLBACK_GAME_CHANGED')
                for row in journal['rows']:
                    require(row['relative']==outputs[row['layer']]['relative'],'ROLLBACK_PATH_SCOPE')
                    for name in ('before','after'):valid_fingerprint(row[name],1024*1024*1024)
                    require(row['after']=={k:outputs[row['layer']][k] for k in ('bytes','sha256')} and
                            row['before'] in (row['after'],signed_rows[row['layer']]['input']),
                            'ROLLBACK_JOURNAL_FINGERPRINT_SCOPE')
                    observed=fingerprint(root/row['relative'])
                    require(observed==row['before'] or (not restored and observed==row['after']),
                            'ROLLBACK_UNKNOWN_RESOURCE')
                protected=journal.get('protected_files',[])
                require(isinstance(protected,list) and len(protected)>=4,'ROLLBACK_PROTECTED_SET')
                for item in protected:
                    rel=item['relative']
                    allowed=rel in {'MTGA.app/Contents/Info.plist','MTGA.app/Contents/MacOS/MTGA',
                                    'MTGA.app/Contents/Frameworks/GameAssembly.dylib'}
                    allowed |= bool(re.fullmatch(r'MTGA_Data/Downloads/Manifest_[A-Za-z0-9_]+\.mtga',rel))
                    allowed |= bool(re.fullmatch(r'MTGA_Data/Downloads/AssetBundle/Bucket_Card\.(FieldFont|FontMaterialSettings)_[A-Za-z0-9_-]+\.mtga',rel))
                    require(allowed and fingerprint(root/rel)==item['fingerprint'],'ROLLBACK_PROTECTED_DRIFT')
            guard();self.native_command('recover',native_state);guard(True)
            if pending.exists():remove_durable(pending)
            print('已核验并恢复到最近一次通过此云端入口安装之前的文件状态。')
            return
        require(not pending.exists(),'PREVIOUS_CLOUD_INSTALLATION_NEEDS_RECOVERY')
        active=self.pull()
        native_state=Path(active['native_state'])
        if command=='check':return
        if command=='update':
            require(self.config.get('allow_game_update') is True,'THIS_RECEIVER_IS_PREVIEW_ONLY')
            if installed.exists():
                previous=read_json(installed)
                previous_state=self.bound_state(previous)
                journal_path=previous_state/'installation.json'
                require(journal_path.exists(),'INSTALLED_JOURNAL_MISSING_PRESERVE_STATE')
                journal=read_json(journal_path)
                if previous['candidate_identity']==active['candidate_identity'] and journal.get('state')=='installed':
                    # New signing keys/artifact expiry can repackage identical
                    # outputs. Keep the original pre-install recovery journal.
                    root,version,build=current_game(self.config['steam_library'])
                    require(same_game_path(journal.get('game'),root) and
                            (journal.get('client_version'),journal.get('steam_build'))==(version,build),
                            'EXISTING_INSTALLATION_GAME_DRIFT')
                    for row in active['descriptor']['five_outputs'].values():
                        require(fingerprint(root/row['relative'])=={k:row[k] for k in ('bytes','sha256')},
                                'EXISTING_INSTALLATION_RESOURCE_DRIFT')
                    for row in journal['protected_files']:
                        require(fingerprint(root/row['relative'])==row['fingerprint'],
                                'EXISTING_INSTALLATION_PROTECTED_DRIFT')
                    print('当前已是相同候选内容，已保留原安装备份与恢复记录，无需重复安装。')
                    return
            save(pending,active)
        self.native_command({'preview':'preview','update':'update','status':'status'}[command],native_state)
        if command=='update':
            self.native_command('status',native_state)
            journal=read_json(native_state/'installation.json')
            require(journal.get('state')=='installed','NATIVE_INSTALLATION_NOT_CONFIRMED')
            save(installed,active)
            remove_durable(pending)

    def bound_state(self,active):
        native_state=Path(active['native_state'])
        require(re.fullmatch(r'[0-9a-f]{64}',active.get('artifact_sha256','')) and
                native_state.parent==self.state/'native-states' and
                native_state.name==active['artifact_sha256'] and
                active.get('candidate_identity')==active['descriptor']['candidate_identity'] and
                active['artifact_sha256']==active['descriptor']['artifact']['sha256'],
                'ACTIVE_STATE_SCOPE')
        regular_parents(native_state)
        return native_state

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('check','preview','update','rollback','status'))
    parser.add_argument('--state',required=True,type=Path)
    args=parser.parse_args();client=None
    try:
        client=Receiver(args.state.resolve());client.execute(args.command)
    except (DeliveryError,ValueError,KeyError,OSError,TypeError) as error:
        print('私有云端检查已停止：'+str(error)[:3000],file=sys.stderr);return 1
    finally:
        if client is not None:client.close()
    return 0

if __name__=='__main__':raise SystemExit(main())
