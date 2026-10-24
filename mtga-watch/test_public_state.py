"""Migration/state tests use synthetic metadata only, no messages or network."""
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import watch_mtga as w
from migrate_watch_state import migrate

STAMP = '2026-10-24T00:00:00+00:00'


def seed(keys=()):
    return {'schema':w.STATE_SCHEMA,'repository':w.REPO,'seen_keys':list(keys)}


def report():
    return {'checked_at':STAMP,'run_url':'https://github.com/'+w.REPO+'/actions/runs/1',
            'status':'CHECKED_WITH_LOCALIZATION_DISCOVERY_LIMIT','errors':[],
            'notifications':[],'probes':{},'human_notifications_sent':False}


class PublicStateTests(unittest.TestCase):
    def test_migration_preserves_markers_closed_history_and_legacy_build_without_bodies(self):
        issues=[{'number':1,'state':'closed','title':'[MTGA] old',
                 'body':'private owner text <!-- mtga-watch:notice-one -->'},
                {'number':2,'state':'closed','title':'[MTGA] 新版本 build 123（当前固定 100）','body':'old'},
                {'number':3,'title':'other','body':'<!-- mtga-watch:must-not-import -->'},
                {'number':4,'title':'[MTGA] PR','pull_request':{},'body':'<!-- mtga-watch:not-an-issue -->'}]
        state=migrate(issues,STAMP)
        self.assertEqual(state['seen_keys'],['notice-one','steam-build-123'])
        text=json.dumps(state)
        self.assertNotIn('private owner text',text);self.assertNotIn('\"number\":',text)
        self.assertFalse(state['human_notifications_sent'])

    def test_migration_reuses_both_notice_stages_and_resource_generations(self):
        state=migrate([{'title':'[MTGA] a','body':'<!-- mtga-watch:notice-id -->\n<!-- mtga-watch:notice-completed-id -->\n<!-- mtga-watch:resources-abc -->'}],STAMP)
        self.assertEqual(set(state['seen_keys']),{'notice-id','notice-completed-id','resources-abc'})
        findings=[w.finding(k,'public','public detail') for k in state['seen_keys']]
        next_state,events=w.advance_state(state,findings,report())
        self.assertTrue(all(e['status']=='previously-observed' for e in events))
        self.assertEqual(next_state['seen_keys'],state['seen_keys'])

    def test_state_is_not_reset_or_truncated_on_invalid_schema_or_keys(self):
        for value in ({},seed(['a','a']),seed(['../unsafe']),seed(['a']*(w.MAX_SEEN_KEYS+1)),{**seed(), 'repository':'other/repo'}):
            with self.subTest(value=str(value)[:40]),self.assertRaises(ValueError):w.validate_state(value)

    def test_no_notification_and_failed_notification_do_not_acknowledge_events(self):
        item=w.finding('new-event','new','detail');state=seed(['old'])
        after,events=w.advance_state(state,[item],report())
        self.assertEqual(after['seen_keys'],['old']);self.assertEqual(events[0]['status'],'new-observation')
        self.assertFalse(after['human_notifications_sent'])
        after,_=w.advance_state(state,[item],report(),['new-event'])
        self.assertEqual(after['seen_keys'],['new-event','old'])
        with self.assertRaises(ValueError):w.advance_state(state,[item],report(),['never-observed'])

    def test_remote_state_uses_main_and_fixed_path_not_legacy_private_repository(self):
        raw=json.dumps(seed(['one'])).encode()
        value={'type':'file','encoding':'base64','path':w.STATE_PATH,'sha':'a'*40,
               'content':base64.b64encode(raw).decode()}
        with patch.object(w,'github_call',return_value=value) as call:
            state,sha=w.remote_state()
        self.assertEqual(state['seen_keys'],['one']);self.assertEqual(sha,'a'*40)
        call.assert_called_once_with('GET','contents/mtga-watch/state.json?ref=main')
        self.assertEqual(w.REPO,'1996Neil/mtga-chinese-updates')

    def test_public_visitor_cannot_suppress_notification_with_forged_marker(self):
        rows=[{'title':'[MTGA] event','body':'<!-- mtga-watch:new-event -->','user':{'login':'public-visitor'}},
              {'title':'[MTGA] event','body':'<!-- mtga-watch:real-event -->','user':{'login':'github-actions[bot]'}},
              {'title':'[MTGA] event','body':'<!-- mtga-watch:owner-event -->','user':{'login':'1996Neil'}}]
        with patch.object(w,'github_call',return_value=rows):
            actual=w.all_watch_issues()
        self.assertEqual([x['user']['login'] for x in actual],['github-actions[bot]','1996Neil'])

    def test_missing_remote_state_is_failure_not_empty_dedup(self):
        with patch.object(w,'github_call',side_effect=urllib.error.HTTPError('url',404,'missing',{},None)):
            with self.assertRaises(urllib.error.HTTPError):w.remote_state()
        with patch.object(w,'github_call',return_value={'type':'symlink'}):
            with self.assertRaises(ValueError):w.remote_state()

    def test_cas_state_update_is_one_fixed_file_and_sha_required(self):
        with patch.object(w,'github_call',return_value={}) as call:
            w.persist_state(seed(['old']),'b'*40)
        method,path,payload=call.call_args.args
        self.assertEqual((method,path),('PUT','contents/mtga-watch/state.json'))
        self.assertEqual(payload['sha'],'b'*40);self.assertEqual(payload['branch'],'main')
        self.assertEqual(json.loads(base64.b64decode(payload['content']))['seen_keys'],['old'])
        with self.assertRaises(ValueError):w.persist_state(seed(),None)

    def test_cas_conflict_is_not_force_retried(self):
        with patch.object(w,'github_call',side_effect=urllib.error.HTTPError('url',409,'conflict',{},None)) as call:
            with self.assertRaises(urllib.error.HTTPError):w.persist_state(seed(),'b'*40)
            self.assertEqual(call.call_count,1)

    def run_main(self, args, *, config_extra=None, state=None, notify_result=None, notify_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config.json';state_path=root/'state.json';dest=root/'report.json'
            config.write_text(json.dumps({'resource_probe':{'steam_build':'1'},'maintainer_notifications':'issues',**(config_extra or {})}))
            state_path.write_text(json.dumps(state or seed()))
            findings=[w.finding('new-event','public event','public detail')]
            with patch.object(w.sys,'argv',['watch',*args,'--state-file',str(state_path),'--config',str(config),'--report',str(dest)]), \
                 patch.object(w,'baseline_target',return_value={'release_id':'r1','steam_build':'1'}), \
                 patch.object(w,'statuspage_items',return_value=[]), \
                 patch.object(w,'resource_generation',return_value={'public':'fixture'}), \
                 patch.object(w,'compare_resources',return_value=findings), \
                 patch.object(w,'remote_state',return_value=(state or seed(),'a'*40)) as read, \
                 patch.object(w,'all_watch_issues',return_value=[]) as history, \
                 patch.object(w,'notify_issue',return_value=notify_result or {'status':'bot_issue_created_email_receipt_unverified'},side_effect=notify_error) as notify, \
                 patch.object(w,'persist_state') as save:
                exit_code=w.main()
                return exit_code,json.loads(dest.read_text()),read.call_count,history.call_count,notify.call_count,save.call_args

    def test_dry_run_never_reads_private_history_sends_or_persists_even_notify_flag(self):
        code,result,reads,history,notify,saved=self.run_main(['--dry-run','--notify-maintainer'])
        self.assertEqual(code,0);self.assertEqual((reads,history,notify),(0,0,0));self.assertIsNone(saved)
        self.assertFalse(result['source_private']);self.assertFalse(result['human_notifications_sent'])
        self.assertEqual(result['deduplication']['seen_after'],0)

    def test_schedule_style_enabled_notification_acknowledges_only_successful_new_events(self):
        code,result,reads,history,notify,saved=self.run_main(['--notify-maintainer'])
        self.assertEqual((code,reads,history,notify),(0,1,1,1));self.assertEqual(saved.args[0]['seen_keys'],['new-event'])
        self.assertTrue(result['human_notifications_sent']);self.assertTrue(result['deduplication']['committed'])
        code,result,_,history,notify,saved=self.run_main(['--notify-maintainer'],state=seed(['new-event']))
        self.assertEqual((code,history,notify),(0,0,0));self.assertEqual(saved.args[0]['seen_keys'],['new-event'])
        self.assertFalse(result['human_notifications_sent'])

    def test_default_no_notify_and_failed_notification_are_not_mislabeled_delivered(self):
        code,result,_,history,notify,saved=self.run_main([])
        self.assertEqual((code,history,notify),(0,0,0));self.assertEqual(saved.args[0]['seen_keys'],[])
        code,result,_,history,notify,saved=self.run_main(['--notify-maintainer'],notify_error=RuntimeError('fail'))
        self.assertEqual(code,1);self.assertEqual(saved.args[0]['seen_keys'],[])
        self.assertFalse(result['human_notifications_sent']);self.assertEqual(saved.args[0]['status'],'DEGRADED')


if __name__ == '__main__':
    unittest.main()
