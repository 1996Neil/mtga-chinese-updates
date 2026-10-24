"""Synthetic temporary writes only; the final pin test reads historical evidence."""
import copy
import shutil
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch
import official_base as b
import detect as d
import installer as i
from common import fingerprint, save_json, read_json

class BaseFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.state=self.root/'state';self.game=self.root/'game';self.run=self.state/'runs/initial';self.official=self.run/'official'
        self.game.mkdir();self.official.mkdir(parents=True)
        self.snap={'client_version':'A','steam_build':'1','data_versions':{'Data':'A','GRP':'1'},'files':{},'official_entries':{},'manifests':{}}
        for layer in (*b.LAYERS,'executable'):
            path=self.game/layer;path.write_text('official-A-'+layer);shutil.copyfile(path,self.official/layer)
            self.snap['files'][layer]={'relative':layer,**fingerprint(path)}
            if layer in b.MANIFEST_LAYERS:self.snap['official_entries'][layer]={'fingerprint':fingerprint(path)}
        manifest=self.game/'manifest';manifest.write_text('official-manifest-A');shutil.copyfile(manifest,self.official/'manifest')
        self.snap['manifests']['manifest']=fingerprint(manifest);save_json(self.run/'snapshot.json',self.snap)
        self.pin_release=b.release(self.snap)
        self.pins={layer:{'official_base_path':str(self.official/layer),'fingerprint':fingerprint(self.official/layer),'authority':'synthetic-reviewed-pin','evidence':{'fixture':True}} for layer in b.OPAQUE}
        self.patcher=patch.object(b,'opaque_baseline',side_effect=self.pin);self.patcher.start();self.journal=self.state/'installation.json'
    def tearDown(self):self.patcher.stop();self.temp.cleanup()
    def pin(self,snapshot,state):
        if b.release(snapshot)!=self.pin_release:raise RuntimeError('No trusted opaque base for this exact release')
        return copy.deepcopy(self.pins)
    def inspect(self,game=None):
        s=copy.deepcopy(self.snap)
        for row in s['files'].values():row.update(fingerprint(self.game/row['relative']))
        for rel in s['manifests']:s['manifests'][rel]=fingerprint(self.game/rel)
        return s
    def diagnose(self):return b.diagnose(self.inspect(),self.game,self.state)
    def prepare(self,name):
        run=self.state/'runs'/name;run.mkdir()
        with patch.object(d,'GAME',self.game),patch.object(d,'STATE',self.state),patch.object(d,'detect',side_effect=self.inspect):d.snapshot_official(self.inspect(),run)
        report={'run_id':name,'candidate_files':{}}
        for layer in b.LAYERS:
            candidate=run/layer;candidate.write_text(name+'-'+layer)
            report['candidate_files'][layer]={'relative':layer,'path':str(candidate),**fingerprint(candidate)}
        return run,report
    def install_patch(self,name):
        run,report=self.prepare(name)
        tx=i.create_transaction(report,run,self.game,self.root/'backups',self.inspect,lambda:None,self.state)
        i.apply_transaction(tx,self.journal,'installed',self.game,self.inspect,lambda:None);self.run=run
        return tx

class OfficialBaseTests(BaseFixture):
    def test_clean_release_current_manifest_and_pinned_opaque(self):
        result=self.diagnose();self.assertTrue(result['safe_to_rebuild'])
        self.assertEqual({r['layer'] for r in result['layers']},set(b.LAYERS))
        self.assertTrue(all(Path(r['official_base_path']).parent==self.game for r in result['layers']))
    def test_repeated_patch_build_original_and_rollback_previous_patch(self):
        self.install_patch('patch1');patch1=self.inspect();tx=self.install_patch('patch2')
        self.assertTrue(self.diagnose()['safe_to_rebuild']);self.assertEqual(tx['schema'],'MTGA_CN_TRANSACTION_V2')
        for row in tx['files']:
            self.assertEqual(Path(row['official_base_path']).read_text(),'official-A-'+row['layer'])
            self.assertEqual(Path(row['rollback_before_path']).read_text(),'patch1-'+row['layer'])
        shutil.rmtree(self.run/'official')
        i.restore_transaction(tx,self.journal,self.game,self.inspect,lambda:None);self.assertEqual(self.inspect(),patch1)
    def test_legacy_read_only_and_not_promoted(self):
        tx=self.install_patch('patch1');tx['schema']='MTGA_CN_TRANSACTION_V1'
        for row in tx['files']:
            for key in ('official_base_path','official_base','rollback_before_path','rollback_before'):row.pop(key)
        save_json(self.journal,tx);before=fingerprint(self.journal);result=self.diagnose()
        self.assertTrue(result['safe_to_rebuild']);self.assertEqual(result['rollback']['status'],'legacy_transaction_without_official_base')
        self.assertEqual(fingerprint(self.journal),before)
        i.restore_transaction(tx,self.journal,self.game,self.inspect,lambda:None);self.assertEqual(self.inspect(),self.snap)
    def test_missing_official_never_falls_back_to_backup(self):
        self.install_patch('patch1');(self.run/'official/card').unlink();r=self.diagnose()
        self.assertFalse(r['safe_to_rebuild']);self.assertTrue(r['rollback']['valid'])
    def test_patched_opaque_and_self_report_not_official(self):
        self.install_patch('patch1')
        for layer in b.OPAQUE:(self.run/'official'/layer).write_text('patch-not-original')
        save_json(self.run/'report.json',{'official_asset_fingerprints':{x:fingerprint(self.run/'official'/x) for x in b.OPAQUE}})
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_missing_backup_blocks_without_writes(self):
        tx=self.install_patch('patch1');Path(tx['files'][0]['before_path']).unlink();before=self.inspect()
        self.assertFalse(self.diagnose()['safe_to_rebuild']);self.assertEqual(self.inspect(),before)
    def test_manifest_drift_blocks(self):
        self.install_patch('patch1');(self.game/'manifest').write_text('unexpected');self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_unknown_current_resource_blocks(self):
        self.install_patch('patch1');(self.game/'resources').write_text('unknown');self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_unknown_new_release_opaque_stops_raw_matches(self):
        self.snap['client_version']='B';self.snap['steam_build']='2';r=self.diagnose()
        self.assertFalse(r['safe_to_rebuild']);self.assertEqual(sum(x['status']=='verified' for x in r['layers']),3)
    def test_new_release_with_verified_pin_accepts_current(self):
        self.install_patch('patch1');self.snap['client_version']='B';self.snap['steam_build']='2';self.snap['data_versions']['Data']='B'
        for layer in b.LAYERS:
            path=self.game/layer;path.write_text('official-B-'+layer);self.snap['files'][layer].update(fingerprint(path))
            if layer in b.MANIFEST_LAYERS:self.snap['official_entries'][layer]['fingerprint']=fingerprint(path)
            else:self.pins[layer]['fingerprint']=fingerprint(path)
        self.pin_release=b.release(self.snap);self.assertTrue(self.diagnose()['safe_to_rebuild'])
    def test_new_release_retaining_patch_blocks(self):
        self.install_patch('patch1');self.snap['data_versions']['GRP']='2';self.pin_release=b.release(self.snap)
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_symlink_official_rejected(self):
        self.install_patch('patch1');path=self.run/'official/card';path.unlink();path.symlink_to(self.official/'card')
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_run_escape_cycle_candidate_never_followed(self):
        tx=self.install_patch('patch1');tx['run_dir']=str(self.root/'backups')
        for row in tx['files']:row['official_base_path']=str(self.journal)
        save_json(self.journal,tx);self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_unfinished_transaction_stops(self):
        tx=self.install_patch('patch1');tx['state']='installing';save_json(self.journal,tx);self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_rollback_alias_mismatch_blocks(self):
        tx=self.install_patch('patch1');tx['files'][0]['rollback_before_path']=str(self.game/'card');before=self.inspect()
        with self.assertRaisesRegex(RuntimeError,'aliases disagree'):i.restore_transaction(tx,self.journal,self.game,self.inspect,lambda:None)
        self.assertEqual(before,self.inspect())
    def test_all_layers_resolve_before_copy(self):
        self.install_patch('patch1');(self.run/'official/bootstrap_fonts').unlink();destination=self.state/'runs/rejected'
        with patch.object(d,'GAME',self.game),patch.object(d,'STATE',self.state),patch.object(d,'detect',side_effect=self.inspect):
            with self.assertRaises(RuntimeError):d.snapshot_official(self.inspect(),destination)
        self.assertFalse(destination.exists())
    def test_diagnostic_no_writes(self):
        self.install_patch('patch1');before={str(p):fingerprint(p) for p in self.root.rglob('*') if p.is_file()};self.diagnose()
        self.assertEqual(before,{str(p):fingerprint(p) for p in self.root.rglob('*') if p.is_file()})

    def test_rolled_back_patch_recognized_but_before_never_source(self):
        self.install_patch('patch1');tx=self.install_patch('patch2')
        i.restore_transaction(tx,self.journal,self.game,self.inspect,lambda:None)
        result=self.diagnose();self.assertTrue(result['safe_to_rebuild'])
        self.assertTrue(all('/official/' in r['official_base_path'] for r in result['layers']))

    def test_unknown_schema_cannot_claim_rebuild_safe(self):
        tx=self.install_patch('patch1');tx['schema']='UNKNOWN';save_json(self.journal,tx)
        result=self.diagnose();self.assertFalse(result['safe_to_rebuild'])
        self.assertEqual(result['rollback']['status'],'invalid_transaction')
    def test_unknown_state_blocks_even_when_current_is_official(self):
        tx=self.install_patch('patch1');tx['state']='UNKNOWN';save_json(self.journal,tx)
        for layer in b.LAYERS:shutil.copyfile(self.official/layer,self.game/layer)
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_v2_requires_explicit_independent_metadata(self):
        tx=self.install_patch('patch1')
        for field in ('rollback_before','rollback_before_path','official_base','official_base_path'):
            broken=copy.deepcopy(tx);broken['files'][0].pop(field);save_json(self.journal,broken)
            self.assertFalse(self.diagnose()['safe_to_rebuild'],field)
    def test_duplicate_or_mismatched_layer_metadata_blocks(self):
        tx=self.install_patch('patch1')
        bad=copy.deepcopy(tx);bad['files'][0]=copy.deepcopy(bad['files'][1]);save_json(self.journal,bad)
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
        bad=copy.deepcopy(tx);bad['files'][0]['before']['sha256']='0'*64;save_json(self.journal,bad)
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
    def test_official_drift_during_backup_prevents_transaction(self):
        run,report=self.prepare('race');calls=[]
        def inspect(game):
            calls.append(1)
            if len(calls)==2:(run/'official/card').write_text('changed-during-backup')
            return self.inspect()
        before=self.inspect()
        with self.assertRaisesRegex(RuntimeError,'Official snapshot corrupted'):
            i.create_transaction(report,run,self.game,self.root/'backups',inspect,lambda:None,self.state)
        self.assertFalse(list((self.root/'backups').glob('*/manifest.json')))
        self.assertEqual(self.inspect(),before)
    def test_candidate_drift_during_backup_prevents_transaction(self):
        run,report=self.prepare('race');calls=[]
        def inspect(game):
            calls.append(1)
            if len(calls)==2:(run/'card').write_text('changed-candidate')
            return self.inspect()
        with self.assertRaisesRegex(RuntimeError,'Candidate drift'):
            i.create_transaction(report,run,self.game,self.root/'backups',inspect,lambda:None,self.state)
        self.assertFalse(list((self.root/'backups').glob('*/manifest.json')))


class PinChainTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('MTGA_RUN_LOCAL_INTEGRATION') == '1',
                         'Local capture evidence required; set MTGA_RUN_LOCAL_INTEGRATION=1 to verify it')
    def test_real_pinned_capture_chain_read_only(self):
        snapshot=read_json(b.STATE/'runs'/b.PIN_RUN/'snapshot.json');rows=b.opaque_baseline(snapshot)
        self.assertEqual(set(rows),set(b.OPAQUE));self.assertTrue(all(r['authority']=='reviewed-local-capture-not-publisher-signature' for r in rows.values()))
    def test_changed_review_root_rejected(self):
        with patch.object(b,'PIN_REVIEW',{'sha256':'0'*64,'bytes':0}):
            with self.assertRaisesRegex(RuntimeError,'review changed'):b.opaque_baseline({})
