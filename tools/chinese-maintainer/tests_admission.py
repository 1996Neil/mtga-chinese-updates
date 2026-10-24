"""Fixed regressions adapted from the user-authorized Pro review (482f862).
Synthetic databases only. The two original format strings are frozen fixtures.
The official-base test intentionally characterizes an unfixed, fail-closed limit.
"""
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
M = ROOT / 'tools/chinese-maintainer'
sys.path.insert(0, str(M))
import common
import registry as r
import provenance as p
import raw
import detect as d

SELECTION = json.loads((M / 'fixtures/admission-selection.json').read_text())
OBSERVATIONS = []


def record(name, **data):
    OBSERVATIONS.append({'name': name, **data})


def new_registry():
    c = sqlite3.connect(':memory:')
    c.executescript(r.SCHEMA)
    p.migrate(c)
    return c


def add_source(c, ident):
    c.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?)',
              (ident, 'synthetic-unused-path', 'synthetic-hash', 0, 'fixture', '{}'))


def seed_legacy(c, key='42', text='抓一张牌。', status='verified', priority=200,
                english='Draw a card.', use=None, source='accepted'):
    add_source(c, source)
    r.add(c, 'card', key, english, text, source, status, priority,
          use or ['Cards.AbilityTextId'], 'fixture')


def seed_snapshot_rows(c, rows):
    for row in rows:
        for source in row['sources']:
            add_source(c, source['source_id'])
            r.add(c, row['layer'], row['key'], source['english'], source['chinese'],
                  source['source_id'], source['status'], source['priority'],
                  json.loads(source['field_use']), 'fixture')


def minimal_official(root, rows=(), identities=None):
    """Real production readers, but tiny synthetic databases, not copied game data."""
    root.mkdir(parents=True, exist_ok=True)
    paths = {'card': root / 'official-card.sqlite', 'client': root / 'official-client.sqlite'}
    with sqlite3.connect(paths['card']) as c:
        c.executescript('''
CREATE TABLE Versions(Type TEXT PRIMARY KEY,Version TEXT);
CREATE TABLE Localizations_enUS(LocId INTEGER,Formatted INTEGER,Loc TEXT,PRIMARY KEY(LocId,Formatted));
CREATE TABLE Localizations_jaJP(LocId INTEGER,Formatted INTEGER,Loc TEXT,PRIMARY KEY(LocId,Formatted));
CREATE TABLE Cards(GrpId INTEGER PRIMARY KEY,TitleId INTEGER,AltTitleId INTEGER,InterchangeableTitleId INTEGER,FlavorTextId INTEGER,ReminderTextId INTEGER,TypeTextId INTEGER,SubtypeTextId INTEGER,AbilityIds TEXT,HiddenAbilityIds TEXT,ExpansionCode TEXT,CollectorNumber TEXT,IsToken INTEGER,IsPrimaryCard INTEGER,IsDigitalOnly INTEGER,IsRebalanced INTEGER,LinkedFaceType INTEGER,LinkedFaceGrpIds TEXT);
CREATE TABLE Abilities(Id INTEGER PRIMARY KEY,TextId INTEGER,EmbeddedActiveAbilityIds TEXT,HiddenAbilityIds TEXT,ReferencedAbilityIds TEXT,ModalChildIds TEXT);
CREATE TABLE Prompts(Id INTEGER PRIMARY KEY,LocId INTEGER);
CREATE TABLE Enums(Id INTEGER PRIMARY KEY,Type TEXT,LocId INTEGER);
CREATE TABLE ProtectedTable(Id INTEGER PRIMARY KEY,Payload TEXT);
''')
        c.execute("INSERT INTO Versions VALUES ('Data','fixture')")
        c.execute("INSERT INTO ProtectedTable VALUES (1,'must remain unchanged')")
        c.execute("INSERT INTO Localizations_enUS VALUES (900,1,'Opt')")
        c.execute("INSERT INTO Localizations_jaJP VALUES (900,1,'fixture-other-language')")
        identities = identities or [{'GrpId': 100, 'TitleId': 900, 'ExpansionCode': 'SYN',
                'CollectorNumber': '1','IsToken': 0,'IsPrimaryCard': 1,
                'IsDigitalOnly': 0,'IsRebalanced': 0,'LinkedFaceType': 0,'LinkedFaceGrpIds': ''}]
        cols = [x[1] for x in c.execute('PRAGMA table_info(Cards)')]
        for identity in identities:
            card = {col: ('' if col in ('AbilityIds','HiddenAbilityIds','ExpansionCode','CollectorNumber','LinkedFaceGrpIds') else 0) for col in cols}
            card.update(identity)
            bindings = []
            for i, row in enumerate(rows):
                if row['layer'] == 'card' and 'Cards.AbilityTextId' in row['field_use']:
                    bindings.append(f'{700+i}:{row["key"]}')
            card['AbilityIds'] = ','.join(bindings)
            c.execute(f'INSERT INTO Cards VALUES ({",".join("?" for _ in cols)})', [card[col] for col in cols])
            if card['TitleId'] != 900:
                c.execute('INSERT OR IGNORE INTO Localizations_enUS VALUES (?,1,?)', (card['TitleId'],'Synthetic Title'))
        for index,row in enumerate(rows):
            if row['layer'] != 'card': continue
            key = int(row['key'])
            c.execute('INSERT INTO Localizations_enUS VALUES (?,1,?)', (key,row['english']))
            if 'Prompts.LocId' in row['field_use']:
                c.execute('INSERT INTO Prompts VALUES (?,?)',(index+1,key))
            if 'Abilities.TextId' in row['field_use']:
                c.execute('INSERT INTO Abilities VALUES (?,?,"","","","")',(index+1,key))
    with sqlite3.connect(paths['client']) as c:
        c.executescript('CREATE TABLE Loc(Key TEXT PRIMARY KEY,enUS TEXT,jaJP TEXT);CREATE TABLE ProtectedTable(Id INTEGER PRIMARY KEY,Payload TEXT);')
        c.execute("INSERT INTO ProtectedTable VALUES (1,'must remain unchanged')")
        for row in rows:
            if row['layer']=='client': c.execute('INSERT INTO Loc VALUES (?,?,?)',(row['key'],row['english'],'untouched-other-language'))
    return paths


class DecisionAndEvidenceProbes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.c = new_registry()
        self.ctx = {'english': 'Draw a card.', 'field_use': ['Cards.AbilityTextId'],
                    'identity': {'cards': [{'GrpId': 1, 'TitleId': 1}]}}
    def tearDown(self):
        self.c.close(); self.temp.cleanup()
    def source_evidence(self, kind='official', english=None, text='抓一张牌。', identity=None):
        blob = self.root / f'doc-{len(list(self.root.iterdir()))}.txt'
        blob.write_text('SYNTHETIC source for a software test; no actual official-source claim.')
        doc = p.append(self.c,'provenance_documents',{
            'source_type':kind,'cache_path':str(blob),'fingerprint':common.fingerprint(blob)})
        row = {'layer':'card','key':'42','source_english':english or self.ctx['english'],
               'translation':text,'identity':identity or self.ctx['identity'],
               'field_use':self.ctx['field_use'],'language':'zh-Hans','document_id':doc}
        p.append(self.c,'provenance_translations',row,layer='card',key='42')
        self.c.commit()
        return doc
    def audit(self, run='baseline', activate=True):
        with patch.object(p,'contexts',return_value={('card','42'):self.ctx}):
            return p.audit(self.c,{},'fixture-v2',run,activate=activate)
    def decision(self, run='preview'):
        return r.decide(self.c,run,'card','42',self.ctx['english'],self.ctx['field_use'])
    def test_fixed_identical_community_evidence_demotes_verified_legacy(self):
        seed_legacy(self.c); before=self.decision()
        self.source_evidence('community'); self.audit(); after=self.decision('after')
        self.assertEqual(before[1]['status'],'verified');self.assertEqual(after[1]['status'],'verified')
        self.assertEqual(after[0],'抓一张牌。')
        record('identical-community-demotion', before=before, after=after)
    def test_fixed_obsolete_official_evidence_demotes_verified_current(self):
        seed_legacy(self.c); before=self.decision()
        self.source_evidence(english='Old text.',text='旧文字。'); self.audit();after=self.decision('after')
        self.assertEqual(after[0],'抓一张牌。');self.assertEqual(after[1]['status'],'verified')
        record('obsolete-evidence-demotion',before=before,after=after)
    def test_fixed_wrong_identity_evidence_demotes_verified_current(self):
        seed_legacy(self.c);before=self.decision()
        self.source_evidence(identity={'cards':[{'GrpId':99,'TitleId':99}]});self.audit();after=self.decision('after')
        self.assertEqual(after[1]['status'],'verified')
        record('unrelated-identity-demotion',before=before,after=after)
    def test_fixed_lower_candidate_conflict_vetoes_accepted_and_matching_official(self):
        seed_legacy(self.c);seed_legacy(self.c,text='摸一张卡。',status='candidate',priority=100,source='donor')
        before=self.decision();self.source_evidence();self.audit();after=self.decision('after')
        self.assertEqual(before[1]['status'],'verified');self.assertEqual(after[1]['status'],'verified')
        self.assertEqual(after[1]['status'],'verified')
        record('lower-candidate-veto',before=before,after=after)
    def test_guard_manual_priority_is_preserved_in_existing_merger(self):
        seed_legacy(self.c,priority=300,source='synthetic-manual-priority');self.source_evidence('community')
        self.audit();after=self.decision()
        self.assertEqual(after[1]['status'],'verified')
    def test_repeated_identical_evaluation_reuses_audit_event(self):
        self.source_evidence();a=self.audit();b=self.audit()
        self.assertEqual(a['registered_evaluations'][0]['id'],b['registered_evaluations'][0]['id'])
        self.assertTrue(b['registered_evaluations'][0]['reused_review'])
        record('registered-translation-cache-positive',reused=True)
    def test_fixed_audit_only_documents_observations_do_not_enter_queue_source_index(self):
        seed_legacy(self.c,status='candidate',priority=100,source='donor')
        self.decision('baseline')
        blob=self.root/'audit.txt';blob.write_text('synthetic audit-only bytes')
        doc=p.append(self.c,'provenance_documents',{'schema':'v1.2-round1-audit-only-source',
             'source_type':'official','cache_path':str(blob),'fingerprint':common.fingerprint(blob),
             'admission':'evidence only; no translation selection or human approval'})
        p.append(self.c,'provenance_observations',{'schema':'v1.2-round1-review-observation',
             'layer':'card','key':'42','associated_source_document_ids':[doc]})
        self.c.commit();report=self.audit()
        self.assertEqual(p.load_rows(self.c),[])
        self.assertEqual(report['categories']['unresearched_external'],0)
        self.assertEqual(report['categories']['official_chinese'],0)
        record('audit-only-index-gap',documents=1,observations=1,registered_translations=0,
               categories=report['categories'])
    def test_fixed_irrelevant_audit_document_invalidates_admission_fingerprint(self):
        before=p.evidence_fingerprint(self.c)
        blob=self.root/'audit.txt';blob.write_text('synthetic audit-only bytes')
        p.append(self.c,'provenance_documents',{'schema':'v1.2-round1-audit-only-source',
             'cache_path':str(blob),'fingerprint':common.fingerprint(blob),'source_type':'community'})
        after=p.evidence_fingerprint(self.c)
        self.assertEqual(before,after)
        record('unconsumed-audit-invalidates-admission',before=before,after=after)
    def test_fixed_missing_unconsumed_audit_cache_blocks_all_provenance_guard(self):
        blob=self.root/'audit.txt';blob.write_text('synthetic audit-only bytes')
        p.append(self.c,'provenance_documents',{'schema':'v1.2-round1-audit-only-source',
             'cache_path':str(blob),'fingerprint':common.fingerprint(blob),'source_type':'community'})
        p.guard(self.c);blob.unlink()
        p.guard(self.c)


class RealFixtureChainProbes(unittest.TestCase):
    def test_positive_two_real_repairs_through_actual_raw_writer_validator_and_f0_stage(self):
        rows=[x for x in SELECTION['B'] if x['key'] in {'615435','Codex/ThePlayBlade_FindMatch'}]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();official=minimal_official(root/'official',rows)
            c=new_registry();seed_snapshot_rows(c,rows)
            seed_legacy(c,key='900',text='抉择',english='Opt',use=['Cards.TitleId'])
            run='fixture_v12';out=root/'output';out.mkdir()
            results={}
            for layer in ('card','client'):
                data=raw.build_localization(official[layer],out/f'{layer}.sqlite',layer,c,run)
                self.assertEqual(data['validation']['status'],'PASS')
                results[layer]=data
            resolution=json.loads((M/'fixtures/admission-format-resolutions.json').read_text())
            for row in resolution['records']:
                self.assertEqual(results[row['layer']]['updates'][row['key']],row['derived_translation'])
            names=raw.build_names(official['card'],out/'card.sqlite',out/'final-card.sqlite',c,run,official['card'])
            self.assertEqual(names['validation']['status'],'PASS')
            with common.database(out/'final-card.sqlite') as db:
                self.assertEqual(db.execute('SELECT Loc FROM Localizations_enUS WHERE LocId=900 AND Formatted=0').fetchone()[0],'Opt')
                self.assertEqual(db.execute('SELECT Loc FROM Localizations_enUS WHERE LocId=900 AND Formatted=1').fetchone()[0],'抉择')
            record('two-real-format-samples-raw-chain',keys=[row['key'] for row in rows],
                localization={k:v['validation']['status'] for k,v in results.items()},
                name_stage=names['validation']['status'],actual_game_candidate=False,native_client_test=False)
            c.close()
    def test_guard_unapproved_icu_candidate_not_promoted(self):
        row=next(x for x in SELECTION['B'] if x['key']=='615435')
        c=new_registry();seed_legacy(c,key=row['key'],english=row['english'],text=row['sources'][0]['chinese'],
             status='candidate',priority=100,use=row['field_use'])
        text,meta=r.decide(c,'fixture','card',row['key'],row['english'],row['field_use'])
        self.assertEqual(meta['status'],'candidate');self.assertEqual(text,row['english']);c.close()
    def test_guard_version_or_use_drift_still_blocks_two_repairs(self):
        for row in SELECTION['B']:
            if row['key'] not in {'615435','Codex/ThePlayBlade_FindMatch'}:continue
            with self.subTest(key=row['key']):
                c=new_registry();seed_snapshot_rows(c,[row])
                a=r.decide(c,'v_new',row['layer'],row['key'],row['english']+' Changed.',row['field_use'])
                b=r.decide(c,'v_new2',row['layer'],row['key'],row['english'],['Different.Use'])
                self.assertNotEqual(a[1]['status'],'verified');self.assertNotEqual(b[1]['status'],'verified');c.close()
    def test_guard_validator_rejects_restoring_bad_icu_after_write(self):
        row=next(x for x in SELECTION['B'] if x['key']=='615435')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();official=minimal_official(root/'official',[row])
            c=new_registry();seed_snapshot_rows(c,[row]);out=root/'output.sqlite'
            built=raw.build_localization(official['card'],out,'card',c,'fixture')
            with sqlite3.connect(out) as db:
                db.execute('UPDATE Localizations_enUS SET Loc=? WHERE LocId=? AND Formatted=1',
                    (row['sources'][0]['chinese'],int(row['key'])))
            with self.assertRaises(RuntimeError):raw.validate_db(official['card'],out,'card',built['updates'])
            c.close()
    def test_frozen_queue_stays_historical_while_same_core_replays_40_rows(self):
        rows=sum((SELECTION[batch] for batch in ('A','B','C')),[])
        c=new_registry();seed_snapshot_rows(c,rows)
        baseline='frozen_accepted_run';preview='scratch_new_decisions'
        for row in rows:
            c.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)',
                (baseline,row['layer'],row['key'],row['status'],None,row['reason'],row['english'],row['english'],json.dumps(row['field_use'])))
        c.commit()
        ctx={(x['layer'],x['key']):x['context'] for x in rows}
        with patch.object(p,'contexts',return_value=ctx):report=p.audit(c,{},'fixture',baseline,activate=True)
        produced=[r.decide(c,preview,x['layer'],x['key'],x['english'],x['field_use'])[1] for x in rows]
        current_pending=sum(x['status'] in ('candidate','needs-review') for x in produced)
        self.assertEqual(report['pending_total'],40);self.assertEqual(current_pending,38)
        record('40-row-historical-vs-effective-queue',historical_audit_count=report['pending_total'],
            actual_same_code_decisions_pending=current_pending,eligible=sum(x['status']=='verified' for x in produced),
            full_1278_registry_available=False)
        c.close()

class OfficialBaseProbe(unittest.TestCase):
    def test_legacy_before_alone_is_never_official(self):
        from tests_base import BaseFixture
        fixture = BaseFixture(); fixture.setUp()
        try:
            fixture.install_patch('patch1')
            # Erase the sole official copy; intact rollback bytes must NOT fill it.
            (fixture.run/'official/card').unlink()
            result = fixture.diagnose()
            self.assertFalse(result['safe_to_rebuild'])
            self.assertTrue(result['rollback']['valid'])
        finally:
            fixture.tearDown()


class AdmissionScopeTests(unittest.TestCase):
    def test_preflight_input_drift_rejects_report(self):
        import preflight
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();state=root/'state';state.mkdir()
            approvals=root/'reviewed-corrections';approvals.mkdir()
            run=state/'runs'/'frozen';run.mkdir(parents=True)
            official=run/'official';official.mkdir()
            for layer in ('card','client','resources'):(official/layer).write_text('synthetic')
            snapshot={'files':{x:{'relative':x} for x in ('card','client','resources')},
                      'official_entries':{x:{'fingerprint':common.fingerprint(official/x)} for x in ('card','client')}}
            common.save_json(run/'snapshot.json',snapshot)
            common.save_json(run/'report.json',{'run_id':'frozen','client_version':'v',
                'official_asset_fingerprints':{'resources':common.fingerprint(official/'resources')}})
            c=new_registry()
            with sqlite3.connect(state/'registry.sqlite') as dst:c.backup(dst)
            c.close();destination=state/'preflights'/'new'
            with patch.object(preflight,'STATE',state),patch.object(preflight,'ROOT',root), \
                 patch.object(preflight,'replay',return_value={}), \
                 patch('official_base.cached_run',return_value={k:{'official_base_path':str(official/k)} for k in ('card','client','resources')}), \
                 patch('reporting.code_fingerprints',side_effect=[{'code':'before'},{'code':'after'}]):
                with self.assertRaisesRegex(RuntimeError,'inputs changed'):
                    preflight.run(run,destination)
            self.assertFalse(destination.exists())
    def test_approval_identity_first_and_previously_imported(self):
        approval = json.loads(next((M/'reviewed-corrections').glob('*.json')).read_text())
        # Preserve the real approved wording/identity; synthetic files replace the
        # machine-specific evidence only inside this software test.
        entry=copy.deepcopy(approval['records'][0]);entry['evidence']={}
        row={'layer':'card','key':entry['key'],'english':entry['english'],'field_use':entry['field_use']}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();corrections=root/'approvals';corrections.mkdir()
            common.save_json(corrections/'approval.json',{**approval,'records':[entry]})
            good=minimal_official(root/'good',[row],entry['identity']['cards'])
            c=new_registry()
            self.assertEqual(r.import_corrections(c,corrections,good,'v'),1)
            self.assertEqual(r.decide(c,'run','card',entry['key'],entry['english'],entry['field_use'])[1]['status'],'verified')
            count=c.execute('select count(*) from provenance_reviews').fetchone()[0]
            self.assertEqual(r.import_corrections(c,corrections,good,'v'),1)
            self.assertEqual(c.execute('select count(*) from provenance_reviews').fetchone()[0],count)
            from preflight import replay
            for index,identities in enumerate([
                [{**x,'IsRebalanced':1} for x in entry['identity']['cards']],
                [{**x,'IsDigitalOnly':1} for x in entry['identity']['cards']],
                [{**x,'TitleId':12345} for x in entry['identity']['cards']],
                entry['identity']['cards'] + [{**entry['identity']['cards'][0],'GrpId':999}],
                [{**x,'LinkedFaceGrpIds':'123'} for x in entry['identity']['cards']]]):
                bad=minimal_official(root/str(index),[row],identities)
                # Prior verified row remains stored, but becomes ineligible.
                self.assertEqual(r.import_corrections(c,corrections,bad,'v'),0)
                text,meta=r.decide(c,'run','card',entry['key'],entry['english'],entry['field_use'])
                self.assertNotEqual(meta['status'],'verified');self.assertEqual(text,entry['english'])
                # Even a frozen verified row with unchanged English/use must
                # enter preflight again when its explicit identity scope changes.
                c.execute("UPDATE decisions SET status='verified' WHERE run_id='run'")
                result=replay(c,bad,'run','v',corrections)
                checked=next(x for x in result['records'] if x['key']==entry['key'])
                self.assertNotEqual(checked['status'],'verified')
                fresh=new_registry()
                self.assertEqual(r.import_corrections(fresh,corrections,bad,'v'),0);fresh.close()
            c.close()
    def test_approved_dependency_hash_still_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();blob=root/'proof';blob.write_text('evidence')
            entry={'layer':'card','key':'42','english':'Draw a card.','chinese':'抓一张牌。',
                   'field_use':['Cards.AbilityTextId'],'evidence':{'cache_path':str(blob),'fingerprint':common.fingerprint(blob)}}
            folder=root/'approval';folder.mkdir()
            common.save_json(folder/'a.json',{'review_kind':'human','reviewed_by':'synthetic-test','source_version':'v','records':[entry]})
            official=minimal_official(root/'official',[entry]);c=new_registry()
            r.import_corrections(c,folder,official,'v');blob.write_text('tampered')
            with self.assertRaisesRegex(RuntimeError,'evidence changed'):
                r.decide(c,'v','card','42',entry['english'],entry['field_use'])
            c.close()
    def test_preflight_and_persisted_decide_are_identical_and_no_history_writes(self):
        from preflight import replay
        rows=[x for x in SELECTION['B'] if x['key'] in {'615435','Codex/ThePlayBlade_FindMatch'}]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();official=minimal_official(root/'official',rows)
            corrections=root/'empty';corrections.mkdir();c=new_registry();seed_snapshot_rows(c,rows)
            for row in rows:
                c.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)',
                    ('frozen',row['layer'],row['key'],'needs-review',None,'format-unsafe:x',row['english'],row['english'],json.dumps(row['field_use'])))
            c.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)',('frozen','card','999','verified','old','ok','Removed','旧',json.dumps(['x'])))
            c.commit();before=list(c.execute('SELECT * FROM decisions'))
            result=replay(c,official,'frozen','v',corrections)
            self.assertEqual(list(c.execute('SELECT * FROM decisions')),before)
            self.assertIn(['card','900'],result['new_keys']);self.assertIn(['card','999'],result['removed_keys'])
            self.assertEqual(result['migration_eligible'],2);self.assertEqual(result['unavailable'],1)
            ctx=p.contexts(official)
            for record_ in result['records']:
                if record_['status']=='unavailable':continue
                layer,key=record_['layer'],record_['key'];context=ctx[layer,key]
                text,meta=r.decide(c,'production',layer,key,context['english'],context['field_use'])
                self.assertEqual(text,record_['translation'])
                for field in ('status','source','reason','migration_eligible','admission_scope'):self.assertEqual(meta[field],record_[field])
            c.close()
    def test_indirect_ability_consumers_and_shared_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            row={'layer':'card','key':'42','english':'Draw a card.','field_use':['Abilities.TextId']}
            official=minimal_official(root,[row])
            with sqlite3.connect(official['card']) as c:
                c.execute("UPDATE Cards SET AbilityIds='2'")
                c.execute("INSERT INTO Abilities VALUES (2,0,'','','1','')")
            context=p.contexts(official)['card','42']
            self.assertEqual([x['GrpId'] for x in context['identity']['cards']],[100])

class MergeAdversarialTests(unittest.TestCase):
    setUp = DecisionAndEvidenceProbes.setUp
    tearDown = DecisionAndEvidenceProbes.tearDown
    source_evidence = DecisionAndEvidenceProbes.source_evidence
    audit = DecisionAndEvidenceProbes.audit
    decision = DecisionAndEvidenceProbes.decision
    def test_inapplicable_legacy_use_does_not_veto_current(self):
        seed_legacy(self.c)
        seed_legacy(self.c,text='抓两张牌。',source='wrong-use',use=['Cards.FlavorTextId'])
        text,meta=self.decision()
        self.assertEqual((text,meta['status']),('抓一张牌。','verified'))
        self.assertTrue(meta['inapplicable_sources'])
    def test_same_trust_official_conflict_blocks(self):
        seed_legacy(self.c);self.source_evidence(text='抓两张牌。');self.audit()
        self.assertEqual(self.decision()[1]['status'],'needs-review')
    def test_two_eligible_official_conflicts_block(self):
        self.source_evidence(text='抓一张牌。');self.source_evidence(text='抓两张牌。');self.audit()
        self.assertEqual(self.decision()[1]['status'],'needs-review')
    def test_selected_source_tamper_blocks(self):
        ident=self.source_evidence();self.audit()
        doc=json.loads(self.c.execute('select payload from provenance_documents where id=?',(ident,)).fetchone()[0])
        Path(doc['cache_path']).write_text('tampered')
        with self.assertRaises(RuntimeError):self.audit()
    def test_observation_chinese_classification_and_bad_cache(self):
        seed_legacy(self.c,status='candidate');self.decision('baseline')
        blob=self.root/'audit';blob.write_text('source')
        doc=p.append(self.c,'provenance_documents',{'schema':'v1.2-round1-audit-only-source',
            'source_type':'official','cache_path':str(blob),'fingerprint':common.fingerprint(blob),'metadata':{'language':'en'}})
        p.append(self.c,'provenance_observations',{'schema':'v1.2-round1-review-observation',
            'layer':'card','key':'42','associated_source_document_ids':[doc],
            'result':{'current_english_hash':p.hash_text(self.ctx['english']),'identity':self.ctx['identity'],'field_use':self.ctx['field_use']}})
        self.c.commit();first=self.audit();blob.unlink();second=self.audit()
        self.assertEqual(first['categories']['official_chinese'],0)
        self.assertFalse(second['queue'][0]['research_evidence'][0]['cache_valid'])
        blob.mkdir()
        self.assertFalse(self.audit()['queue'][0]['research_evidence'][0]['cache_valid'])
        blob.rmdir();target=self.root/'target';target.write_text('source');blob.symlink_to(target)
        self.assertFalse(self.audit()['queue'][0]['research_evidence'][0]['cache_valid'])
        self.assertEqual(p.load_rows(self.c),[])
