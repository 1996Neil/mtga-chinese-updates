import base64
import copy
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import watch_mtga as w

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def resource(layer):
    return {'layer': layer, 'relative': layer + '.mtga', 'input': {'sha256': 'a'*64, 'bytes': 3}}


class WatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.key = Path(cls.temp.name) / 'private.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'Ed25519', '-out', str(cls.key)], check=True, capture_output=True)
        pub = subprocess.check_output(['openssl', 'pkey', '-in', str(cls.key), '-pubout', '-outform', 'DER'])[-32:]
        cls.pub = base64.b64encode(pub).decode()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def envelope(self, obj):
        data = json.dumps(obj).encode()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'payload';p.write_bytes(data)
            sig = subprocess.check_output(['openssl','pkeyutl','-sign','-rawin','-inkey',str(self.key),'-in',str(p)])
        return json.dumps({'payload': base64.b64encode(data).decode(), 'signature': base64.b64encode(sig).decode()}).encode()

    def feed_fixture(self):
        release = {'schema':'MTGA_CN_RELEASE_V1','id':'2026.64.50-25919252-r1','client_version':'2026.64.50',
                   'steam_build':'25919252','revision':1,'platform':'macOS-Steam',
                   'resources':[resource(x) for x in ('card','client','font_bundle','resources','bootstrap_fonts')]}
        raw = self.envelope(release)
        ref = {k:release[k] for k in ('id','client_version','steam_build','revision')}
        ref['manifest'] = {'name':release['id']+'.json','fingerprint':{'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}}
        index = {'schema':'MTGA_CN_INDEX_V1','channel':'testing','sequence':1,'expires_at':'2026-11-13T00:00:00Z','releases':[ref]}
        config = {'feed':'https://raw.githubusercontent.com/1996Neil/mtga-chinese-updates/main/feed','channel':'testing','public_key':self.pub}
        return config, index, release, raw

    def test_real_signature_and_tamper(self):
        raw = self.envelope({'a':1});self.assertEqual(w.verified_envelope(raw,self.pub),{'a':1})
        d=json.loads(raw);d['payload']=base64.b64encode(b'{"a":2}').decode()
        with self.assertRaises(ValueError):w.verified_envelope(json.dumps(d).encode(),self.pub)

    def test_wrong_trusted_key(self):
        with self.assertRaises(ValueError):w.verified_envelope(self.envelope({'a':1}),base64.b64encode(b'0'*32).decode())

    def test_feed_baseline_tracks_actual_new_release_without_config_target(self):
        config,index,release,raw=self.feed_fixture()
        with patch.object(w,'fetch',side_effect=[self.envelope(index),raw]):
            actual=w.baseline_target(config,NOW)
        self.assertEqual(actual['steam_build'],'25919252');self.assertTrue(actual['signature_verified'])

    def test_expired_index_fails(self):
        config,index,release,raw=self.feed_fixture();index['expires_at']='2020-01-01T00:00:00Z'
        with patch.object(w,'fetch',return_value=self.envelope(index)):
            with self.assertRaisesRegex(ValueError,'expired'):w.baseline_target(config,NOW)

    def test_manifest_tamper_fails_before_parse(self):
        config,index,release,raw=self.feed_fixture()
        with patch.object(w,'fetch',side_effect=[self.envelope(index),raw+b' ']):
            with self.assertRaisesRegex(ValueError,'fingerprint'):w.baseline_target(config,NOW)

    def test_duplicate_release_identity_fails(self):
        config,index,release,raw=self.feed_fixture();index['releases']*=2
        with patch.object(w,'fetch',return_value=self.envelope(index)):
            with self.assertRaisesRegex(ValueError,'Duplicate'):w.baseline_target(config,NOW)

    def test_semantic_version_numeric_sort(self):
        refs=[{'client_version':v,'steam_build':'10','revision':1} for v in ('2026.9.0','2026.10.0')]
        self.assertEqual(max(refs,key=w.release_order)['client_version'],'2026.10.0')

    def test_recent_completed_notice_retained_ordinary_outage_filtered(self):
        incidents={'incidents':[{'id':'a','name':'Game Update','status':'resolved','created_at':NOW.isoformat()},
                                {'id':'b','name':'Login outage','status':'resolved','created_at':NOW.isoformat()}]}
        windows={'scheduled_maintenances':[]}
        with patch.object(w,'fetch',side_effect=[json.dumps(incidents).encode(),json.dumps(windows).encode()]):
            rows=w.statuspage_items()
        self.assertEqual([r['id'] for r in rows],['a']);self.assertEqual(w.relevant(rows[0],NOW,timedelta(hours=24)),'新公告')

    def test_upcoming_notice_is_not_deployed_version(self):
        self.assertIn('不代表',w.relevant({'starts_at':NOW+timedelta(hours=3)},NOW,timedelta(hours=24)))
        self.assertIsNone(w.relevant({'announced_at':NOW-timedelta(hours=25)},NOW,timedelta(hours=24)))

    def notice(self, **updates):
        item = {'id':'event1','name':'Game Update','status':'scheduled',
                'announced_at':NOW-timedelta(hours=1), 'starts_at':NOW+timedelta(hours=2),
                'ends_at':NOW+timedelta(hours=5), 'updated_at':NOW-timedelta(hours=1),
                'body':'Small download required. ~3 hour deployment.'}
        item.update(updates)
        return item

    def test_preflight_shows_beijing_window_duration_and_relative_wait(self):
        notice=w.notice_finding(self.notice(),NOW,timedelta(hours=24))
        self.assertEqual(notice['key'],'notice-event1')
        for text in ('2026-10-23 10:00（北京时间）','2026-10-23 13:00（北京时间）',
                     '3 小时','5.0 小时','Small download','不能据此确定实际资源变化'):
            self.assertIn(text,notice['text'])

    def test_completion_after_old_announcement_has_separate_marker(self):
        item=self.notice(status='completed',announced_at=NOW-timedelta(days=7),
                         starts_at=NOW-timedelta(hours=4),ends_at=NOW-timedelta(hours=1),updated_at=NOW)
        done=w.notice_finding(item,NOW,timedelta(hours=24))
        self.assertEqual(done['key'],'notice-completed-event1')
        self.assertIn('不证明汉化相关资源已变化',done['text'])
        old=[{'number':1,'title':'[MTGA] notice','body':'<!-- mtga-watch:notice-event1 -->'}]
        with patch.object(w,'github_call',return_value={'number':2,'title':done['title'],'html_url':'url','body':'<!-- mtga-watch:notice-completed-event1 -->'}) as call:
            self.assertIn('created',w.notify_issue(done,done['text'],old)['status'])
            self.assertEqual(w.notify_issue(done,done['text'],old)['status'],'deduplicated')
            self.assertEqual(call.call_count,1)

    def test_old_completion_does_not_resend_forever(self):
        self.assertIsNone(w.notice_finding(self.notice(status='completed',updated_at=NOW-timedelta(days=3)),NOW,timedelta(hours=24)))

    def test_missing_end_does_not_invent_wait(self):
        notice=w.notice_finding(self.notice(ends_at=None),NOW,timedelta(hours=24))
        self.assertIn('无法可靠给出几小时后可更新',notice['text'])
        self.assertNotIn('距预计结束',notice['text'])

    def test_overdue_uncompleted_maintenance_does_not_claim_ready(self):
        notice=w.notice_finding(self.notice(starts_at=NOW-timedelta(hours=5),ends_at=NOW-timedelta(hours=2)),NOW,timedelta(hours=24))
        self.assertIn('尚未确认完成',notice['text'])
        self.assertNotIn('官方已宣布维护完成',notice['text'])

    def test_active_maintenance_kept_without_recent_announcement(self):
        notice=w.notice_finding(self.notice(status='in_progress',announced_at=NOW-timedelta(days=4),
            starts_at=NOW-timedelta(days=3),ends_at=NOW-timedelta(days=2)),NOW,timedelta(hours=24))
        self.assertIn('官方维护正在进行',notice['text'])

    def test_official_parser_preserves_end_update_and_body(self):
        raw={'id':'e','name':'Game Update','status':'completed','created_at':NOW.isoformat(),
             'updated_at':NOW.isoformat(),'scheduled_for':NOW.isoformat(),
             'scheduled_until':(NOW+timedelta(hours=3)).isoformat(),
             'incident_updates':[{'body':'Official details'}]}
        with patch.object(w,'fetch',side_effect=[b'{"incidents":[]}',json.dumps({'scheduled_maintenances':[raw]}).encode()]):
            row=w.statuspage_items()[0]
        self.assertEqual(row['ends_at'],NOW+timedelta(hours=3))
        self.assertEqual(row['updated_at'],NOW);self.assertEqual(row['body'],'Official details')

    def test_closed_issue_deduplicates_by_marker(self):
        item=w.finding('build-2','new','detail')
        issues=[{'number':4,'state':'closed','title':'renamed','body':'<!-- mtga-watch:build-2 -->'}]
        with patch.object(w,'github_call') as call:
            self.assertEqual(w.notify_issue(item,'body',issues)['status'],'deduplicated');call.assert_not_called()

    def test_same_title_new_resource_generation_not_swallowed(self):
        item=w.finding('resources-new','same title','new generation')
        issues=[{'number':1,'title':item['title'],'body':'<!-- mtga-watch:resources-old -->','state':'closed'}]
        with patch.object(w,'github_call',return_value={'number':2,'title':item['title'],'html_url':'url'}) as call:
            self.assertIn('created',w.notify_issue(item,'detail',issues)['status']);self.assertEqual(call.call_count,1)

    def test_new_notice_id_with_reused_name_not_swallowed(self):
        item=w.finding('notice-new','Game Update','new')
        issues=[{'number':1,'title':item['title'],'body':'legacy notice','state':'closed'}]
        with patch.object(w,'github_call',return_value={'number':2,'title':item['title'],'html_url':'url'}) as call:
            self.assertIn('created',w.notify_issue(item,'detail',issues)['status']);self.assertEqual(call.call_count,1)

    def test_old_seed_not_reported_as_newer_official_resource(self):
        result=w.compare_resources({'release_id':'new','steam_build':'20'}, {'seed_steam_build':'10'})
        self.assertIn('seed-stale',result[0]['key'])

    def test_vdf_rejects_wrong_os_duplicate_and_trailing_data(self):
        base='"2141910" {"depots" {"branches" {"public" {"buildid" "12"}} "2141913" {"config" {"oslist" "macos"} "manifests" {"public" {"gid" "34"}}}}}'
        self.assertEqual(w.parse_vdf(base),('12','34'))
        for text in (base.replace('macos','windows'),base+' "extra"',base.replace('"buildid" "12"','"buildid" "12" "buildid" "13"')):
            with self.assertRaises(ValueError):w.parse_vdf(text)

    def test_issue_history_paginates_all_states(self):
        first=[{'number':i,'title':'other'} for i in range(100)]
        second=[{'number':101,'title':'[MTGA] done','state':'closed','user':{'login':'github-actions[bot]'}}]
        with patch.object(w,'github_call',side_effect=[first,second]) as call:
            self.assertEqual(len(w.all_watch_issues()),1);self.assertIn('state=all',call.call_args_list[0].args[1])

    def test_legacy_existing_build_issue_deduplicates(self):
        item=w.finding('steam-build-2','new','detail','[MTGA] old')
        with patch.object(w,'github_call') as call:
            self.assertEqual(w.notify_issue(item,'body',[{'number':1,'title':'[MTGA] old'}])['status'],'deduplicated');call.assert_not_called()

    def test_issue_bot_route_does_not_claim_email_receipt(self):
        item=w.finding('n','test','detail')
        with patch.object(w,'github_call',return_value={'number':8,'title':item['title'],'html_url':'url'}) as call:
            actual=w.notify_issue(item,'detail',[])
        self.assertIn('unverified',actual['status']);self.assertTrue(call.call_args.args[2]['body'].startswith('@1996Neil'))

    def test_same_build_resource_change_detected(self):
        baseline={'release_id':'old','steam_build':'10','resources':[resource(x) for x in w.LAYERS]}
        assets={x:{'name':x+'.mtga','sha256':'a'*64,'bytes':3} for x in w.LAYERS}
        observed={'seed_steam_build':'10','assets':assets,'observed_against_release':'old'}
        self.assertEqual(w.compare_resources(baseline,observed),[])
        assets['client']['sha256']='b'*64
        self.assertEqual(len(w.compare_resources(baseline,observed)),1)

    def test_same_build_new_publication_never_reports_old_frozen_locale_as_new(self):
        baseline={'release_id':'new-r3','steam_build':'10','resources':[resource(x) for x in w.LAYERS]}
        observed={'seed_steam_build':'10','observed_against_release':'old-r2',
                  'assets':{x:{'name':x+'.mtga','sha256':'b'*64,'bytes':3} for x in w.LAYERS}}
        result=w.compare_resources(baseline,observed)
        self.assertEqual(len(result),1);self.assertTrue(result[0]['key'].startswith('resource-seed-publication-'))

    def test_manifest_gzip_and_content_address(self):
        plain=json.dumps({'Assets':[{'Name':'Raw_ClientLocalization_'+'a'*32+'.mtga','Length':12,'sha256':base64.b64encode(b'a'*32).decode()}]}).encode()
        name='Manifest_Localization_'+hashlib.md5(plain).hexdigest()+'.mtga'
        with patch.object(w,'fetch',return_value=gzip.compress(plain)):
            self.assertEqual(w.manifest(name)['client']['bytes'],12)
        with patch.object(w,'fetch',return_value=b'{}'):
            with self.assertRaisesRegex(ValueError,'content-address'):w.manifest(name)

    def test_bad_metadata_urls_and_paths_rejected(self):
        for url in ('http://assets.mtgarena.wizards.com/a','https://example.com/x','https://user:pass@assets.mtgarena.wizards.com/a','https://assets.mtgarena.wizards.com/a?token=x'):
            with self.assertRaises(ValueError):w.validate_url(url)
        with self.assertRaises(ValueError):w.manifest('../anything')

    def test_localization_coverage_never_claims_global_latest(self):
        with patch.object(w,'fetch',return_value=b'a'*32),patch.object(w,'manifest',side_effect=[{'card':{},'font_bundle':{}},{'client':{}}]):
            result=w.resource_generation({'external_name':'External_2026.64.50.5990.1332174-macos.mtga','localization_manifest':'Manifest_Localization_'+'b'*32+'.mtga','steam_build':'1','client_version':'2026.64.50','published_release_id':'old'})
        self.assertIn('UNVERIFIED',result['independent_localization_latest'])

    def test_source_failure_is_not_silent_no_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);config=p/'config.json';config.write_text(json.dumps({'resource_probe':{}}))
            with patch.object(w.sys,'argv',['watch','--dry-run','--config',str(config),'--report',str(p/'report.json')]),patch.object(w,'baseline_target',side_effect=OSError),patch.object(w,'statuspage_items',return_value=[]),patch.object(w,'resource_generation',side_effect=OSError),patch.dict(os.environ,{'MTGA_NOTIFICATION_TEST':'false'}):
                self.assertEqual(w.main(),1)
            d=json.loads((p/'report.json').read_text());self.assertEqual(d['status'],'DEGRADED');self.assertEqual(len(d['errors']),2)


if __name__=='__main__':
    unittest.main()
