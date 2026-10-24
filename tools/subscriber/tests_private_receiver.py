"""Controller provenance plus real native processes on synthetic Steam trees only."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import private_receiver as r
import install_private_receiver as installer
from private_delivery import DeliveryError, fingerprint
import tests_subscriber as fixture_module
BINARY=fixture_module.BINARY


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.SubscriberTests('test_signature_tampering_is_rejected')
        self.fixture.setUp();f=self.fixture
        self.root=f.root;self.state=self.root/'receiver';self.state.mkdir()
        self.gh=self.root/'gh';self.gh.write_text('#!/bin/sh\nexit 99\n');self.gh.chmod(0o700)
        self.config={'schema':r.SCHEMA,'repository':r.REPOSITORY,'branch':r.BRANCH,
                     'workflow_path':r.WORKFLOW,'reviewed_source_commit':'1'*40,
                     'reviewed_blobs':{r.WORKFLOW:'2'*40,'entry.py':'3'*40,
                                       'data.py':'4'*40,'core.py':'5'*40},
                     'native_path':str(BINARY),'native_fingerprint':fingerprint(BINARY),
                     'gh_path':str(self.gh),'gh_fingerprint':fingerprint(self.gh),
                     'steam_library':str(f.library),'allow_game_update':True}
        r.save(self.state/'config.json',self.config)
        # Use real native signatures and deltas, but package them under the exact
        # flat data-only contract consumed by the cloud receiver.
        f.release.update(id='2026.64.0-25763111-r3',revision=3)
        for row in f.release['resources']:
            old=f.feed/row['patch']['name'];new=f.feed/(row['layer']+'.delta')
            old.rename(new);row['patch']['name']=new.name
        f.resign()
        f.index['sequence']=3;f.resign()
        names={x+'.delta' for x in r.LAYERS}|{'profile.json','index-testing.json',
                 '2026.64.0-25763111-r3.json','delivery.json','report.json'}
        for path in list(f.feed.iterdir()):
            if path.name not in names:path.unlink()
        r.save(f.feed/'profile.json',{'schema':'MTGA_CN_PROFILE_V1','feed':'./feed',
                                     'public_key':f.pub,'channel':'testing'})
        r.save(f.feed/'delivery.json',{'phase':'PRE_UPLOAD'})
        self.report=r.canonical({'synthetic':True})+b'\n'
        (f.feed/'report.json').write_bytes(self.report)
        self.package=self.root/'package.zip'
        with zipfile.ZipFile(self.package,'w',zipfile.ZIP_DEFLATED) as z:
            for path in f.feed.iterdir():
                path.chmod(0o600);z.write(path,path.name)
        outputs={row['layer']:{'relative':row['relative'],**row['output']}
                 for row in f.release['resources']}
        target={'app_id':2141910,'depot_id':2141913,'platform':'macos',
                'steam_branch':'public','client_version':'2026.64.0',
                'steam_build':'25763111','resource_generation':'synthetic'}
        self.descriptor={'schema':'MTGA_PRIVATE_DELIVERY_V1','phase':'FINALIZED',
            'repository':r.REPOSITORY,'branch':r.BRANCH,'workflow_path':r.WORKFLOW,
            'workflow_source_commit':'1'*40,'execution_source_commit':'6'*40,
            'run_id':'22','run_attempt':1,'artifact':{'id':'33',
            'name':'mtga-private-native-data-22',**fingerprint(self.package)},
            'target':target,'five_outputs':outputs,'report':{'commit':'7'*40,
            'path':'cloud-maintenance/results/unattended-22-1.json',
            'sha256':hashlib.sha256(self.report).hexdigest()},'public_key':f.pub,
            'archive_members':sorted(names),
            'member_fingerprints':{p.name:fingerprint(p) for p in f.feed.iterdir()},
            'candidate_identity':hashlib.sha256(r.canonical({'target':target,
                                                   'five_outputs':outputs})).hexdigest(),
            'effects':{'install':False,'publish':False,'schedule':False}}
        self.run={'id':22,'status':'completed','conclusion':'success','event':'push',
            'head_branch':r.BRANCH,'head_sha':'6'*40,'path':r.WORKFLOW,'run_attempt':1}
        a=self.descriptor['artifact']
        self.artifact={'id':33,'name':a['name'],'expired':False,'size_in_bytes':a['bytes'],
            'digest':'sha256:'+a['sha256'],'workflow_run':{'id':22,'head_sha':'6'*40}}
        self.tree={'truncated':False,'tree':[{'type':'blob','path':p,'sha':v}
                            for p,v in self.config['reviewed_blobs'].items()]}
        outer=self
        class SyntheticGitHub:
            def __init__(self,*args):pass
            def repository(self):return {'private':True}
            def contents(self,path,ref):
                raw=(r.canonical(outer.descriptor) if path=='delivery/latest.json'
                     else outer.report)
                return {'encoding':'base64','content':base64.b64encode(raw).decode()}
            def json(self,path):
                return outer.run if '/runs/' in path else (outer.artifact if '/artifacts/' in path else outer.tree)
            def archive(self,ident,dest,expected):shutil.copyfile(outer.package,dest)
        self.patches=[patch.object(r,'PrivateGitHub',SyntheticGitHub),
                      patch.dict(os.environ,{'MTGA_TEST_ASSUME_STOPPED':'1'})]
        for p in self.patches:p.start()
        self.receiver=r.Receiver(self.state)

    def tearDown(self):
        self.receiver.close()
        for p in reversed(self.patches):p.stop()
        self.fixture.tearDown()

    def test_provenance_rejects_wrong_run_artifact_source(self):
        r.validate_descriptor(self.descriptor,self.config)
        r.validate_producer(self.descriptor,self.run,self.artifact,self.tree,self.config)
        for obj,field,value in [(self.run,'conclusion','failure'),(self.run,'head_sha','8'*40),
            (self.run,'path','other.yml'),(self.run,'event','workflow_dispatch'),
            (self.artifact,'expired',True),(self.artifact,'digest','sha256:'+'0'*64),
            (self.artifact,'id',34),(self.tree,'truncated',True)]:
            old=obj[field];obj[field]=value
            with self.assertRaises(DeliveryError):
                r.validate_producer(self.descriptor,self.run,self.artifact,self.tree,self.config)
            obj[field]=old
        self.tree['tree'][0]['sha']='9'*40
        with self.assertRaisesRegex(DeliveryError,'CODE_DRIFT'):
            r.validate_producer(self.descriptor,self.run,self.artifact,self.tree,self.config)

    def test_report_and_candidate_identity_mismatch_rejected_before_native(self):
        self.report+=b'changed'
        with self.assertRaisesRegex(DeliveryError,'REPORT_FINGERPRINT'):self.receiver.pull()
        self.descriptor['candidate_identity']='0'*64
        with self.assertRaisesRegex(DeliveryError,'CANDIDATE_IDENTITY'):self.receiver.pull()
        self.assertFalse((self.state/'native-states').exists())

    def test_real_native_preview_install_recover_and_check_pointer_isolation(self):
        before=self.fixture.game_hashes();self.receiver.execute('preview')
        self.assertEqual(before,self.fixture.game_hashes())
        self.assertFalse((self.state/'installed.json').exists())
        self.receiver.execute('update');installed=(self.state/'installed.json').read_bytes()
        self.assertNotEqual(before,self.fixture.game_hashes())
        self.receiver.execute('check')
        self.assertEqual(installed,(self.state/'installed.json').read_bytes())
        self.receiver.github=None
        with patch.object(r,'PrivateGitHub',side_effect=AssertionError('offline recovery')):
            self.receiver.execute('status');self.receiver.execute('rollback')
        self.assertEqual(before,self.fixture.game_hashes())

    def test_native_crash_keeps_intent_and_offline_recovery(self):
        before=self.fixture.game_hashes()
        with patch.dict(os.environ,{'MTGA_TEST_CRASH_AFTER':'client'}):
            with self.assertRaisesRegex(DeliveryError,'NATIVE_UPDATE_FAILED'):
                self.receiver.execute('update')
        self.assertTrue((self.state/'pending.json').exists())
        with self.assertRaisesRegex(DeliveryError,'NEEDS_RECOVERY'):self.receiver.execute('check')
        self.receiver.execute('rollback');self.assertEqual(before,self.fixture.game_hashes())
        self.assertFalse((self.state/'pending.json').exists())

    def test_repackaged_same_content_preserves_original_recovery_pointer(self):
        before=self.fixture.game_hashes();self.receiver.execute('update')
        prior=(self.state/'installed.json').read_bytes()
        # Re-sign both envelopes with a fresh key, preserving candidate identity.
        f=self.fixture;f.private.unlink()
        f.pub=f.invoke('keygen','--output',str(f.private),base=False).stdout.strip()
        f.resign();r.save(f.feed/'profile.json',{'schema':'MTGA_CN_PROFILE_V1',
                            'feed':'./feed','public_key':f.pub,'channel':'testing'})
        self.descriptor['public_key']=f.pub
        for p in f.feed.iterdir():p.chmod(0o600)
        self.package.unlink()
        with zipfile.ZipFile(self.package,'w',zipfile.ZIP_DEFLATED) as z:
            for p in f.feed.iterdir():z.write(p,p.name)
        self.descriptor['artifact'].update(fingerprint(self.package))
        self.descriptor['member_fingerprints']={p.name:fingerprint(p) for p in f.feed.iterdir()}
        self.artifact.update(size_in_bytes=self.descriptor['artifact']['bytes'],
                             digest='sha256:'+self.descriptor['artifact']['sha256'])
        self.receiver.execute('update')
        self.assertEqual(prior,(self.state/'installed.json').read_bytes())
        self.receiver.execute('rollback');self.assertEqual(before,self.fixture.game_hashes())

    def test_interruption_before_native_journal_can_clear_verified_intent(self):
        active=self.receiver.pull();r.save(self.state/'pending.json',active)
        before=self.fixture.game_hashes();self.receiver.execute('rollback')
        self.assertEqual(before,self.fixture.game_hashes())
        self.assertFalse((self.state/'pending.json').exists())

    def test_unknown_resource_blocks_no_journal_recovery(self):
        active=self.receiver.pull();r.save(self.state/'pending.json',active)
        (self.fixture.game/self.fixture.paths['card']).write_bytes(b'unknown')
        before=self.fixture.game_hashes()
        with self.assertRaisesRegex(DeliveryError,'UNKNOWN_RESOURCE'):self.receiver.execute('rollback')
        self.assertEqual(before,self.fixture.game_hashes());self.assertTrue((self.state/'pending.json').exists())

    def test_missing_installed_journal_cannot_report_recovery_success(self):
        self.receiver.execute('update');active=r.read_json(self.state/'installed.json')
        (Path(active['native_state'])/'installation.json').unlink()
        before=self.fixture.game_hashes()
        with self.assertRaisesRegex(DeliveryError,'JOURNAL_MISSING'):self.receiver.execute('rollback')
        self.assertEqual(before,self.fixture.game_hashes())

    def test_corrupt_cache_or_journal_stops_recovery_before_writes(self):
        self.receiver.execute('update');active=r.read_json(self.state/'installed.json')
        journal_path=Path(active['native_state'])/'installation.json';journal=r.read_json(journal_path)
        journal['rows'][0]['relative']='../outside';r.save(journal_path,journal)
        before=self.fixture.game_hashes()
        with self.assertRaisesRegex(DeliveryError,'PATH_SCOPE'):self.receiver.execute('rollback')
        self.assertEqual(before,self.fixture.game_hashes())

    def test_new_game_build_blocks_pull_and_preserves_prior_records(self):
        self.receiver.execute('update');prior=(self.state/'installed.json').read_bytes()
        self.fixture.app.write_text(self.fixture.app.read_text().replace('25763111','25763112'))
        with self.assertRaisesRegex(DeliveryError,'CURRENT_GAME'):self.receiver.execute('check')
        self.assertEqual(prior,(self.state/'installed.json').read_bytes())

    def test_state_escape_and_receiver_lock_rejected(self):
        active=self.receiver.pull();active['native_state']=str(self.root/'outside')
        with self.assertRaisesRegex(DeliveryError,'ACTIVE_STATE_SCOPE'):self.receiver.bound_state(active)
        with self.assertRaisesRegex(DeliveryError,'RECEIVER_BUSY'):r.Receiver(self.state)

    def test_independent_entry_installs_and_refuses_existing_or_changed_source(self):
        desktop=self.root/'Cloud entry.command';state=self.root/'installed receiver'
        original=self.root/'Old entry.command';original.write_bytes(b'PRESERVE')
        result=installer.install(self.config,state,desktop)
        import subprocess
        self.assertEqual(subprocess.run(['/bin/zsh','-n',str(desktop)],capture_output=True).returncode,0)
        menu=subprocess.run(['/bin/zsh',str(desktop)],input='6\n0\n',text=True,capture_output=True)
        self.assertEqual(menu.returncode,0,menu.stderr);self.assertIn('全新版本资源自动定位尚未接通',menu.stdout)
        self.assertEqual(original.read_bytes(),b'PRESERVE')
        with self.assertRaisesRegex(DeliveryError,'ALREADY_EXISTS'):installer.install(self.config,state,desktop)
        (state/'private_receiver.py').write_text('raise RuntimeError("never execute")')
        refused=subprocess.run(['/bin/zsh',str(desktop)],input='\n',text=True,capture_output=True)
        self.assertNotEqual(refused.returncode,0);self.assertIn('文件发生变化',refused.stdout)
        self.assertNotIn('never execute',refused.stderr)


if __name__=='__main__':unittest.main()
