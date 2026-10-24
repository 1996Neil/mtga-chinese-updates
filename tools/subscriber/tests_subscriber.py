"""Real native process integration against synthetic Steam trees, never real MTGA."""
import copy
import fcntl
import hashlib
from unittest.mock import patch
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from publisher import canonical, fingerprint, make_delta, validate_acceptance, package, subject_for, CHECKS, verify_delta

BINARY = Path(os.environ['MTGA_SUBSCRIBER_TEST_BINARY']).resolve()


class SubscriberTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mtga-subscriber-fixture-')
        self.root = Path(self.temp.name).resolve()
        self.feed = self.root/'feed';self.feed.mkdir()
        self.state = self.root/'state'; self.library = self.root/'Steam Library'
        self.game = self.library/'steamapps/common/MTGA'; self.game.mkdir(parents=True)
        self.app = self.library/'steamapps/appmanifest_2141910.acf'
        self.app.write_text('"AppState" { "appid" "2141910" "StateFlags" "4" "installdir" "MTGA" "buildid" "25763111" }')
        self.private = self.root/'private-key'
        self.pub = self.invoke('keygen','--output',str(self.private),base=False).stdout.strip()
        self.profile = self.root/'profile.json'
        self.profile.write_bytes(canonical({'schema':'MTGA_CN_PROFILE_V1','feed':str(self.feed),'public_key':self.pub,'channel':'testing'}))
        self.paths = {'card':'MTGA_Data/Downloads/Raw/Raw_CardDatabase_'+'a'*32+'.mtga',
            'client':'MTGA_Data/Downloads/Raw/Raw_ClientLocalization_'+'b'*32+'.mtga',
            'resources':'MTGA.app/Contents/Resources/Data/resources.assets',
            'bootstrap_fonts':'MTGA.app/Contents/Resources/Data/sharedassets0.assets',
            'font_bundle':'MTGA_Data/Downloads/AssetBundle/Fonts_abcd-1234.mtga'}
        self.bases = {}
        for layer,rel in self.paths.items():
            data=(layer.encode()+b'|0123456789|')*10000
            path=self.game/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            self.bases[layer]=self.root/('base-'+layer);self.bases[layer].write_bytes(data)
        protections = ['MTGA.app/Contents/Info.plist','MTGA.app/Contents/MacOS/MTGA',
                       'MTGA.app/Contents/Frameworks/GameAssembly.dylib','MTGA_Data/Downloads/Manifest_deadbeef.mtga']
        for rel in protections:
            path=self.game/rel;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'2026.64.0'}) if rel.endswith('Info.plist') else b'protected-official-file')
        self.protected=[{'relative':rel,'fingerprint':fingerprint(self.game/rel)} for rel in protections]
        self.release=None;self.index=None;self.publish(1)

    def tearDown(self):self.temp.cleanup()

    def invoke(self,*args,base=True,ok=True,env=None):
        argv=[str(BINARY),*args]
        if base:argv+=['--state',str(self.state),'--profile',str(self.profile),'--steam-library',str(self.library)]
        result=subprocess.run(argv,text=True,capture_output=True,timeout=30,env={**os.environ,'MTGA_TEST_ASSUME_STOPPED':'1',**(env or {})})
        if ok:self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        return result

    def sign(self,obj,path):
        payload=self.root/'sign-input.json';payload.write_bytes(canonical(obj))
        self.invoke('sign','--key',str(self.private),'--input',str(payload),'--output',str(path),base=False)

    def publish(self,revision):
        resources=[]
        for layer,rel in self.paths.items():
            candidate=self.root/(layer+'-r'+str(revision));data=self.bases[layer].read_bytes()
            candidate.write_bytes(data[:65536]+('中文修订'+str(revision)).encode()*1000+data[80000:])
            patch=self.feed/(layer+'-r'+str(revision)+'.delta');fp=make_delta(self.bases[layer],candidate,patch)
            resources.append({'layer':layer,'relative':rel,'input':fingerprint(self.bases[layer]),'output':fingerprint(candidate),'patch':{'name':patch.name,'fingerprint':fp}})
        self.release={'schema':'MTGA_CN_RELEASE_V1','id':'2026.64.0-r'+str(revision),'revision':revision,'client_version':'2026.64.0','steam_build':'25763111','minimum_client':1,'platform':'macOS-Steam','architectures':['arm64','x86_64'],
            'acceptance':{'status':'testing','evidence_sha256':None,'scope':'synthetic integration fixture only'},'resources':resources,'protected_files':self.protected}
        self.index={'schema':'MTGA_CN_INDEX_V1','sequence':revision,'expires_at':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),'channel':'testing','releases':[]}
        self.resign()

    def resign(self):
        path=self.feed/(self.release['id']+'.json');self.sign(self.release,path)
        self.index['releases']=[{'id':self.release['id'],'revision':self.release['revision'],'client_version':self.release['client_version'],'steam_build':self.release['steam_build'],'manifest':{'name':path.name,'fingerprint':fingerprint(path)}}]
        self.sign(self.index,self.feed/('index-'+self.index['channel']+'.json'))

    def game_hashes(self):return {rel:fingerprint(self.game/rel) for rel in [*self.paths.values(),*(r['relative'] for r in self.protected)]}
    def refused(self,command='preview',contains=None):
        before=self.game_hashes();r=self.invoke(command,ok=False);self.assertNotEqual(r.returncode,0)
        if contains:self.assertIn(contains,r.stderr)
        self.assertEqual(before,self.game_hashes());return r

    def test_first_install_upgrade_rollback_and_official_base_are_distinct(self):
        baseline=self.game_hashes();self.invoke('preview');self.assertEqual(baseline,self.game_hashes())
        self.invoke('update');first=self.game_hashes();self.assertNotEqual(first,baseline)
        self.assertIn('无需重复',self.invoke('update').stdout)
        self.publish(2);self.invoke('update');second=self.game_hashes();self.assertNotEqual(first,second)
        journal=json.loads((self.state/'installation.json').read_text())
        for row in journal['rows']:
            self.assertNotEqual(row['before'],row['original'])
            self.assertEqual(row['original'],fingerprint(self.bases[row['layer']]))
        # Loss of build base must not prevent rollback from pre-install backups.
        for path in (self.state/'transactions'/journal['id']).glob('original-*'):path.unlink()
        self.invoke('rollback');self.assertEqual(first,self.game_hashes())

    def test_rollback_then_reapply_or_upgrade(self):
        self.invoke('update');self.publish(2);self.invoke('update')
        second=self.game_hashes();self.invoke('rollback');self.invoke('update')
        self.assertEqual(second,self.game_hashes())
        self.invoke('rollback');self.publish(3);self.invoke('update')
        self.assertNotEqual(second,self.game_hashes())

    def test_game_lock_blocks_different_state(self):
        identity=self.game.stat()
        name=f'{identity.st_dev}-{identity.st_ino}.lock'
        locks=Path.home()/'Library/Application Support/MTGAChineseSubscriberLocks';locks.mkdir(parents=True,exist_ok=True)
        with (locks/name).open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for command in ('check','preview','update','rollback'):
                self.refused(command,contains='另一状态目录')
        self.invoke('preview')

    def test_publisher_rejects_mutation_while_packaging(self):
        run=self.root/'run';run.mkdir()
        snapshot={'client_version':'2026.64.0','steam_build':'25763111','data_versions':{},'files':{},'manifests':{}}
        report={'schema':'MTGA_CN_MAINTAINER_V1','candidate_files':{},'localization':{'static_status':'PASS'},'import_compatibility':{'static_status':'PASS'},'code_fingerprints':{}}
        meta={'layers':[]}
        for layer,rel in self.paths.items():
            base=run/'official'/rel;base.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(self.bases[layer],base)
            out=run/('candidate-'+layer);shutil.copyfile(self.root/(layer+'-r1'),out)
            snapshot['files'][layer]={'relative':rel,**fingerprint(base)}
            report['candidate_files'][layer]={'relative':rel,'path':str(out),**fingerprint(out)}
            meta['layers'].append({'layer':layer,'relative':rel,'fingerprint':fingerprint(base)})
        for name,obj in (('snapshot.json',snapshot),('report.json',report),('official-base.json',meta)):(run/name).write_bytes(canonical(obj))
        subject=subject_for(snapshot,report,meta)
        evidence={'schema':'MTGA_CN_PUBLISHER_ACCEPTANCE_V1','subject_sha256':hashlib.sha256(canonical(subject)).hexdigest(),'checks':{k:True for k in CHECKS},'tester_quote':'fixture only','scope':'synthetic','architectures':['arm64']}
        acceptance=run/'acceptance.json';acceptance.write_bytes(canonical(evidence))
        (run/'independent-review.json').write_bytes(canonical({'status':'PASS','code_fingerprints':{},'candidate_files':report['candidate_files']}))
        real_make=make_delta
        def mutate(base,out,dest):
            fp=real_make(base,out,dest);out.write_bytes(b'changed during packaging');return fp
        dest=self.root/'package'
        with patch('publisher.make_delta',side_effect=mutate):
            with self.assertRaises(ValueError):package(run,dest,BINARY,self.private,self.pub,1,'stable',acceptance)
        self.assertFalse((dest/'index-stable.json').exists())
        self.assertFalse((dest/'2026.64.0-r1.json').exists())
        snapshot['files']['card']['sha256']='f'*64
        self.assertEqual(subject_for(snapshot,report,meta),subject)

    def test_failed_journal_sync_stops_before_game_write(self):
        before=self.game_hashes()
        result=self.invoke('update',env={'MTGA_TEST_FAIL_JOURNAL_SYNC':'1'},ok=False)
        self.assertNotEqual(result.returncode,0);self.assertIn('同步失败',result.stderr)
        self.assertEqual(before,self.game_hashes());self.invoke('recover')

    def test_enospc_after_first_resource_restores_prior_state(self):
        before=self.game_hashes()
        result=self.invoke('update',env={'MTGA_TEST_ENOSPC_TARGET':Path(self.paths['client']).name},ok=False)
        self.assertNotEqual(result.returncode,0);self.assertIn('ENOSPC',result.stderr)
        self.assertEqual(before,self.game_hashes())
        self.invoke('update')

    def test_upgrade_interruption_recovery_then_retry(self):
        self.invoke('update');first=self.game_hashes();self.publish(2)
        result=self.invoke('update',env={'MTGA_TEST_CRASH_AFTER':'client'},ok=False)
        self.assertEqual(result.returncode,79);self.invoke('recover');self.assertEqual(first,self.game_hashes())
        self.invoke('update');self.assertNotEqual(first,self.game_hashes())

    def test_kill_after_every_resource_is_recoverable(self):
        before=self.game_hashes()
        for layer in self.paths:
            r=self.invoke('update',env={'MTGA_TEST_CRASH_AFTER':layer},ok=False);self.assertEqual(r.returncode,79)
            self.refused('update',contains='上次操作未完成')
            self.invoke('recover');self.assertEqual(before,self.game_hashes())

    def test_unknown_changes_and_new_steam_version_block_recovery(self):
        self.invoke('update');path=self.game/self.paths['card'];path.write_bytes(b'unknown third party')
        self.refused('recover',contains='未知变化')
        self.app.write_text(self.app.read_text().replace('25763111','25763112'))
        self.refused('recover',contains='游戏版本发生变化')

    def test_corrupt_backup_stops_before_any_rollback_write(self):
        self.invoke('update');j=json.loads((self.state/'installation.json').read_text())
        (self.state/'transactions'/j['id']/'before-card').write_bytes(b'corrupt')
        self.refused('rollback',contains='完整性不匹配')

    def test_signature_tampering_is_rejected(self):
        path=self.feed/'index-testing.json';obj=json.loads(path.read_text());obj['signature']='A'*88;path.write_bytes(canonical(obj))
        self.refused(contains='签名校验失败')

    def test_expired_index_is_rejected(self):
        self.index['expires_at']='2001-01-01T00:00:00Z';self.resign();self.refused(contains='已过期')

    def test_old_index_and_same_sequence_replacement_are_rejected(self):
        self.invoke('check');old=(self.feed/'index-testing.json').read_bytes();self.publish(2);self.invoke('check')
        (self.feed/'index-testing.json').write_bytes(old);self.refused(contains='索引版本倒退')
        self.index['sequence']=2;self.index['expires_at']='2099-01-01T00:00:00Z';self.resign();self.refused(contains='同一索引序号')

    def test_clock_rollback_is_rejected(self):
        self.invoke('check');p=self.state/'seen-testing.json';data=json.loads(p.read_text());data['checked_at']+=7200;p.write_bytes(canonical(data));self.refused(contains='时间明显倒退')

    def test_wrong_channel_and_wrong_release_link_are_rejected(self):
        self.index['channel']='stable';self.resign();shutil.copyfile(self.feed/'index-stable.json',self.feed/'index-testing.json');self.refused(contains='索引无效')

    def test_testing_release_cannot_enter_stable_channel(self):
        p=json.loads(self.profile.read_text());p['channel']='stable';self.profile.write_bytes(canonical(p));self.index['channel']='stable';self.resign();self.refused(contains='尚未完成发布者验收')

    def test_signed_write_outside_resource_allowlist_is_rejected(self):
        self.release['resources'][0]['relative']='MTGA.app/Contents/MacOS/MTGA';self.resign();self.refused(contains='禁止的文件')

    def test_duplicate_or_missing_layers_are_rejected(self):
        self.release['resources'][-1]=copy.deepcopy(self.release['resources'][0]);self.resign();self.refused(contains='资源层集合')

    def test_symbolic_link_and_multiple_active_card_files_are_rejected(self):
        path=self.game/self.paths['card'];path.unlink();path.symlink_to(self.bases['card'])
        r=self.invoke('preview',ok=False);self.assertNotEqual(r.returncode,0);self.assertIn('符号链接',r.stderr)
        path.unlink();shutil.copyfile(self.bases['card'],path)
        (path.parent/('Raw_CardDatabase_'+'c'*32+'.mtga')).write_bytes(b'extra')
        self.refused(contains='多份活动资源')

    def test_changed_protected_binary_blocks_all_writes(self):
        (self.game/'MTGA.app/Contents/Frameworks/GameAssembly.dylib').write_bytes(b'changed')
        self.refused('update',contains='完整性不匹配')

    def test_tampered_patch_is_rejected_before_install(self):
        patch=self.feed/self.release['resources'][0]['patch']['name'];patch.write_bytes(b'bad')
        self.refused('update',contains='完整性不匹配')

    def malformed_delta(self,data):
        row=self.release['resources'][0];patch=self.feed/row['patch']['name'];patch.write_bytes(data);row['patch']['fingerprint']=fingerprint(patch);self.resign();self.refused('update')

    def test_copy_bounds_and_trailing_delta_are_rejected(self):
        size=self.release['resources'][0]['output']['bytes']
        self.malformed_delta(b'MTGADL01'+struct.pack('<QI',size,1)+b'\0'+struct.pack('<QI',2**63,100))

    def test_literal_compression_tail_is_rejected(self):
        row=self.release['resources'][0];data=b'x'*100;row['output']={'sha256':'0'*64,'bytes':100}
        packed=zlib.compress(data)+b'ignored trailing data'
        self.malformed_delta(b'MTGADL01'+struct.pack('<QI',100,1)+b'\1'+struct.pack('<II',100,len(packed))+packed)

    def test_setup_runs_without_python_or_git(self):
        state=self.root/'fresh install'
        r=self.invoke('setup','--profile',str(self.profile),'--state',str(state),base=False,env={'PATH':'/usr/bin:/bin'})
        self.assertIn('安装完成',r.stdout);self.assertTrue((state/'mtga-cn').exists())
        r=subprocess.run([str(state/'mtga-cn'),'help'],text=True,capture_output=True,env={'PATH':'/usr/bin:/bin'},timeout=10)
        self.assertEqual(r.returncode,0);self.assertIn('中文维护',r.stdout)

    def test_bundled_profile_can_install_after_folder_move(self):
        from package_installer import package as installer
        kit=installer(BINARY,self.profile,self.root/'kit',self.feed)
        moved=self.root/'moved kit';kit.rename(moved);state=self.root/'install-moved'
        self.invoke('setup','--profile',str(moved/'profile.json'),'--state',str(state),base=False)
        profile=json.loads((state/'profile.json').read_text())
        self.assertEqual(Path(profile['feed']).resolve(),(moved/'feed').resolve())
        result=subprocess.run([str(state/'mtga-cn'),'preview','--state',str(state),'--steam-library',str(self.library)],text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_publisher_reconstruction_rejects_tampered_delta(self):
        row=self.release['resources'][0];patch_path=self.feed/row['patch']['name']
        verify_delta(self.bases['card'],patch_path,row['output'])
        patch_path.write_bytes(patch_path.read_bytes()+b'junk')
        with self.assertRaises(ValueError):verify_delta(self.bases['card'],patch_path,row['output'])

    def test_acceptance_cannot_be_reused_for_changed_build(self):
        with self.assertRaises(ValueError):validate_acceptance({'schema':'MTGA_CN_PUBLISHER_ACCEPTANCE_V1','subject_sha256':'0'*64},{'candidate':'new'})


if __name__=='__main__':unittest.main()
