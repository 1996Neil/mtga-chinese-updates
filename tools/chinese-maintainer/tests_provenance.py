"""v1.1 tests; real Arena sample plus adversarial admission/migration cases."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from common import fingerprint, save_json
import provenance as p
from registry import SCHEMA, add, decide

FIXTURE=json.loads((Path(__file__).parent/'fixtures/arena-of-glory-source.json').read_text())

class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.executescript(SCHEMA);p.migrate(self.c)
        self.ctx={'english':FIXTURE['current_english'],'identity':FIXTURE['identity'],'field_use':FIXTURE['field_use']}
        self.row={'translation':FIXTURE['donor_chinese'],'source_english':FIXTURE['current_english'],
                  'identity':FIXTURE['identity'],'field_use':FIXTURE['field_use'],'language':'zh-Hans'}
        self.doc={'source_type':'official'}
    def tearDown(self):self.c.close()
    def test_official_unchanged_is_automatic_not_human(self):
        r=p.evaluate(self.row,self.ctx,self.doc)
        self.assertEqual(r['status'],'verified');self.assertFalse(r['requires_review'])
    def test_real_arena_old_official_oracle_clarification_not_auto_approved(self):
        row=copy.deepcopy(self.row);row['source_english']=FIXTURE['old_english'];row['translation']=FIXTURE['official_chinese']
        row['oracle']={'text':FIXTURE['current_english'],'client_english':FIXTURE['current_english'],'classification':'template-clarification'}
        r=p.evaluate(row,self.ctx,self.doc,self.doc)
        self.assertEqual(r['classification'],'official-template-clarification');self.assertEqual(r['status'],'needs-review')
        self.assertTrue(any(x['after'].strip()=='any of' for x in r['changes']))
    def test_real_semantic_change_is_review_not_assertion(self):
        ctx=copy.deepcopy(self.ctx);ctx['english']=ctx['english'].replace('until end of turn','permanently')
        r=p.evaluate(self.row,ctx,self.doc)
        self.assertEqual(r['classification'],'possible-semantic-change');self.assertEqual(r['status'],'needs-review')
    def test_community_cannot_become_official_or_win_conflict(self):
        self.assertEqual(p.evaluate(self.row,self.ctx,{'source_type':'community'})['status'],'needs-review')
        self.assertEqual(p.evaluate(self.row,self.ctx,self.doc,conflicts=True)['reason'],'conflicting-source-translations')
    def test_identity_and_special_faces_must_match(self):
        row=copy.deepcopy(self.row);row['identity']['cards'][0]['TitleId']=123
        self.assertEqual(p.evaluate(row,self.ctx,self.doc)['reason'],'identity-or-face-mismatch')
    def test_unavailable_source_does_not_pass(self):
        self.assertEqual(p.evaluate(self.row,self.ctx,self.doc,unavailable=True)['status'],'needs-review')
    def test_hant_is_not_automatically_converted_to_hans(self):
        self.row['language']='zh-Hant';self.assertEqual(p.evaluate(self.row,self.ctx,self.doc)['reason'],'language-variant-review')
    def test_same_review_content_idempotent_history_append_only(self):
        e={'status':'verified','english_hash':p.hash_text(self.ctx['english']),'review_kind':'automatic-source-check'}
        a=p.append(self.c,'provenance_reviews',e,translation_id='example')
        self.assertEqual(a,p.append(self.c,'provenance_reviews',e,translation_id='example'))
        self.assertEqual(self.c.execute('select count(*) from provenance_reviews').fetchone()[0],1)
        with self.assertRaises(sqlite3.IntegrityError):self.c.execute('delete from provenance_reviews')
    def test_ai_pack_cannot_claim_human_approval(self):
        with tempfile.TemporaryDirectory() as t:
            a=Path(t).resolve()/'pack.json';b=Path(t).resolve()/'review.json'
            save_json(a,{'schema':'MTGA_SOURCE_PACK_V1','review_kind':'human'})
            save_json(b,{'kind':'independent-source-verification','status':'PASS','pack_fingerprint':fingerprint(a),'reviewer':'AI independent source reviewer'})
            with self.assertRaisesRegex(RuntimeError,'human approval'):p.import_pack(self.c,a,b,Path(t).resolve()/'cache')
    def test_digital_and_rebalanced_never_take_paper_authority(self):
        self.ctx['identity']=copy.deepcopy(self.ctx['identity']);self.ctx['identity']['cards'][0]['IsRebalanced']=1
        self.row['identity']=self.ctx['identity']
        self.assertEqual(p.evaluate(self.row,self.ctx,self.doc)['reason'],'digital-client-authority-review')
    def test_only_layout_normalization_numbers_and_conditions_not_normalized(self):
        self.assertEqual(p.classify('Pay 1.\n Draw.','Pay 1. Draw.')['classification'],'layout-only')
        self.assertEqual(p.classify('Pay 1. Draw.','Pay 2. Draw.')['classification'],'possible-semantic-change')
        self.assertEqual(p.classify('A; B','A: B')['classification'],'possible-semantic-change')
    def test_backup_and_migration_keep_legacy_data(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t).resolve()/'registry.sqlite';c=sqlite3.connect(path);c.executescript(SCHEMA)
            c.execute("insert into sources values ('old','file','hash',1,'v','{}')");c.commit()
            before=list(c.execute('select * from sources'));p.migrate(c)
            backups=list((Path(t).resolve()/'registry-backups').glob('*.sqlite'));self.assertEqual(len(backups),1)
            with sqlite3.connect(backups[0]) as b:self.assertEqual(list(b.execute('select * from sources')),before)
            p.migrate(c);self.assertEqual(list(c.execute('select * from sources')),before);c.close()
    def test_mana_or_placeholder_changes_block(self):
        self.row['translation']=self.row['translation'].replace('{oRoR}','{oR}')
        self.assertEqual(p.evaluate(self.row,self.ctx,self.doc)['reason'],'format-unsafe')
    def test_official_input_mismatch_to_oracle_blocks(self):
        self.row['oracle']={'text':'new rules','client_english':'different English'}
        self.assertEqual(p.evaluate(self.row,self.ctx,self.doc)['reason'],'client-oracle-version-mismatch')
    def test_source_hash_integrity(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t).resolve()/'source';path.write_text('original')
            p.append(self.c,'provenance_documents',{'cache_path':str(path),'fingerprint':fingerprint(path)})
            p.guard(self.c);path.write_text('changed')
            with self.assertRaises(RuntimeError):p.guard(self.c)
    def test_legacy_decide_still_works_without_active_provenance(self):
        self.c.execute("insert into sources values ('old','file','hash',1,'v','{}')")
        add(self.c,'card','1','Draw','抓牌','old','verified',200,['Cards.AbilityTextId'],'v')
        text,meta=decide(self.c,'v_run','card','1','Draw',['Cards.AbilityTextId'])
        self.assertEqual((text,meta['status']),('抓牌','verified'))
    def test_bound_source_review_required(self):
        with tempfile.TemporaryDirectory() as t:
            a=Path(t).resolve()/'pack.json';b=Path(t).resolve()/'review.json';save_json(a,{});save_json(b,{'kind':'human'})
            with self.assertRaises(RuntimeError):p.import_pack(self.c,a,b)

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.executescript(SCHEMA);p.migrate(self.c)
        self.ctx={'english':FIXTURE['current_english'],'identity':FIXTURE['identity'],'field_use':FIXTURE['field_use']}
    def tearDown(self):self.c.close()
    def seed(self, kind='official', key='1160075', suffix='', text=None):
        doc=p.append(self.c,'provenance_documents',{'source_type':kind,'label':suffix,'role':'oracle-current'})
        row={'layer':'card','key':key,'source_english':self.ctx['english'],'translation':text or FIXTURE['donor_chinese'],
             'identity':self.ctx['identity'],'field_use':self.ctx['field_use'],'language':'zh-Hans','document_id':doc,
             'oracle':{'document_id':doc,'text':self.ctx['english'],'client_english':self.ctx['english'],'hash':p.hash_text(self.ctx['english'])}}
        ident=p.append(self.c,'provenance_translations',row,layer='card',key=key)
        self.c.commit();return doc,ident
    def audit(self, **kw):
        with patch.object(p,'guard'),patch.object(p,'contexts',return_value={('card','1160075'):self.ctx}):
            return p.audit(self.c,{},'v2','prior',**kw)
    def test_real_audit_reuses_review_event(self):
        self.seed();a=self.audit();b=self.audit()
        self.assertFalse(a['registered_evaluations'][0]['reused_review']);self.assertTrue(b['registered_evaluations'][0]['reused_review'])
        self.assertTrue(b['registered_evaluations'][0]['reused_selection'])
        self.assertEqual(a['registered_evaluations'][0]['id'],b['registered_evaluations'][0]['id'])
    def test_selection_cache_skips_repeated_semantic_evaluation(self):
        self.seed();self.audit()
        with patch.object(p, 'evaluate', side_effect=AssertionError('cache miss')):
            report = self.audit()
        self.assertEqual(report['registered_evaluations'][0]['status'], 'verified')
        self.assertTrue(report['registered_evaluations'][0]['reused_selection'])
    def test_selection_cache_invalidates_when_source_observation_changes(self):
        doc,_=self.seed();self.audit()
        p.append(self.c,'provenance_observations',{'document_id':doc,'status':'unavailable','at':'changed'})
        with patch.object(p, 'evaluate', wraps=p.evaluate) as evaluate:
            report=self.audit()
        self.assertGreater(evaluate.call_count, 0)
        self.assertEqual(report['registered_evaluations'][0]['reason'],'source-unavailable-or-changed')
    def test_new_version_stable_source_reuses_translation_without_semantic_review(self):
        self.seed();a=self.audit();self.assertEqual(a['registered_evaluations'][0]['status'],'verified')
        with patch.object(p,'guard'),patch.object(p,'contexts',return_value={('card','1160075'):self.ctx}):
            b=p.audit(self.c,{},'v3','prior')
        self.assertEqual(b['registered_evaluations'][0]['status'],'verified');self.assertFalse(b['registered_evaluations'][0]['requires_review'])
    def test_oracle_actual_difference_blocks_even_with_claimed_client_match(self):
        doc,ident=self.seed();row=p.load_rows(self.c)[0][1];row['oracle']['text']=self.ctx['english'].replace('haste','flying')
        self.assertEqual(p.evaluate(row,self.ctx,{'source_type':'official'})['reason'],'client-oracle-version-mismatch')
    def test_partial_refresh_cannot_forget_old_unavailable(self):
        doc,_=self.seed();p.append(self.c,'provenance_observations',{'document_id':doc,'status':'unavailable'});self.c.commit()
        with patch.object(p,'refresh_documents',return_value=(set(),[])):
            report=self.audit(refresh=True)
        self.assertEqual(report['registered_evaluations'][0]['reason'],'source-unavailable-or-changed')
    def test_observation_state_changes_admission_fingerprint(self):
        doc,_=self.seed();a=p.evidence_fingerprint(self.c)
        p.append(self.c,'provenance_observations',{'document_id':doc,'status':'unavailable','at':'one'});b=p.evidence_fingerprint(self.c)
        self.assertNotEqual(a,b)
        p.append(self.c,'provenance_observations',{'document_id':doc,'status':'unavailable','at':'two'})
        self.assertEqual(b,p.evidence_fingerprint(self.c))
    def test_replace_cannot_change_or_reinsert_history(self):
        ident=p.append(self.c,'provenance_reviews',{'status':'needs-review'},translation_id='t')
        original=self.c.execute('select rowid,* from provenance_reviews').fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute('INSERT OR REPLACE INTO provenance_reviews VALUES (?,?,?)',(ident,'t','{"status":"verified"}'))
        self.c.execute('INSERT OR REPLACE INTO provenance_reviews VALUES (?,?,?)',original[1:])
        self.assertEqual(original,self.c.execute('select rowid,* from provenance_reviews').fetchone())
    def test_same_official_and_community_text_deterministic_selection(self):
        self.seed('official');self.seed('community',suffix='community');self.audit(activate=True)
        result=p.select(self.c,'card','1160075',self.ctx['english'],self.ctx['field_use'])
        self.assertEqual(result[2],'verified')
    def test_migration_failure_is_atomic_and_retryable(self):
        c=sqlite3.connect(':memory:');c.executescript(SCHEMA)
        # Authorizer interrupts the second CREATE TABLE, simulating DDL failure.
        count=[0]
        def fail(action,a,b,db,trigger):
            if action==sqlite3.SQLITE_CREATE_TABLE:
                count[0]+=1
                if count[0]==2:return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(fail)
        with self.assertRaises(sqlite3.DatabaseError):p.migrate(c)
        c.set_authorizer(None)
        self.assertFalse(c.execute("select name from sqlite_master where name like 'provenance_%'").fetchall())
        p.migrate(c);self.assertEqual(c.execute('select count(*) from provenance_migrations').fetchone()[0],1);c.close()
    def test_source_failure_returns_gap_and_preserves_translation(self):
        doc,_=self.seed()
        # Simulates a registered official source that cannot be fetched.
        with patch.object(p,'official_url',return_value=True):
            with patch.object(p.urllib.request,'urlopen',side_effect=OSError('rate limited')):
                # seed document deliberately has no URL: inject a valid payload in a separate fresh document.
                real=p.append(self.c,'provenance_documents',{'source_type':'official','role':'oracle-current','url':'https://magic.wizards.com/example'})
                bad,events=p.refresh_documents(self.c,[real])
        self.assertIn(real,bad);self.assertEqual(events[0]['status'],'unavailable')
        self.assertEqual(len(p.load_rows(self.c)),1)
    def test_obsolete_translation_does_not_block_new_exact_official_source(self):
        doc,_=self.seed()
        row=copy.deepcopy(p.load_rows(self.c)[0][1]);row['source_english']='Obsolete rules';row['translation']='旧规则'
        row.pop('oracle');p.append(self.c,'provenance_translations',row,layer='card',key='1160075');self.c.commit()
        report=self.audit(activate=True)
        result=p.select(self.c,'card','1160075',self.ctx['english'],self.ctx['field_use'])
        self.assertEqual(result[2],'verified')
        self.assertTrue(any(e['other_version_or_identity_evidence'] for e in report['registered_evaluations']))
    def test_legacy_registry_input_tables_unchanged_by_source_audit(self):
        self.seed();before={t:list(self.c.execute('select * from '+t)) for t in ('localization','decisions','card_names','sources')}
        self.audit();after={t:list(self.c.execute('select * from '+t)) for t in before};self.assertEqual(before,after)

if __name__=='__main__':unittest.main(verbosity=2)
