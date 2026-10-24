import json, os, subprocess, time, unittest
from pathlib import Path
from publisher import canonical
from tests_subscriber import SubscriberTests, BINARY

class CacheTests(SubscriberTests):
    def setUp(self):
        super().setUp()
        for row in self.release['resources']:
            row['patch']['url']='https://github.com/owner/repo/releases/download/v1/'+row['patch']['name']
        self.resign()
        p=json.loads(self.profile.read_text());p['feed']='https://raw.githubusercontent.com/owner/repo/main/feed';self.profile.write_bytes(canonical(p))
        fixture=Path(__file__).with_name('fake_curl_fixture.py').resolve();fixture.chmod(0o755)
        self.log=self.root/'requests.jsonl'
        self.env={'MTGA_TEST_CURL':str(fixture),'MTGA_FIXTURE_FEED':str(self.feed),'MTGA_FIXTURE_LOG':str(self.log)}
    def call(self,mode='normal',ok=True,command='preview',extra=None):
        return self.invoke(command,ok=ok,env={**self.env,'MTGA_FIXTURE_MODE':mode,**(extra or {})})
    def requests(self):return [json.loads(x) for x in self.log.read_text().splitlines()]
    def cache(self,suffix):
        fp=self.release['resources'][0]['patch']['fingerprint']
        return self.state/'patch-cache'/(fp['sha256']+'-'+str(fp['bytes'])+'.'+suffix)
    def assert_failure_unchanged(self,mode):
        before=self.game_hashes();r=self.call(mode,ok=False);self.assertNotEqual(r.returncode,0);self.assertEqual(before,self.game_hashes());return r
    def test_preview_to_update_reuses_verified_bytes_and_refreshes_metadata(self):
        self.call();self.log.write_text('');self.call(command='update')
        calls=self.requests();self.assertEqual(len(calls),2);self.assertTrue(all(x['name'].endswith('.json') for x in calls))
        self.assertTrue((self.state/'installation.json').exists())
    def test_interrupted_transfer_resumes_exact_prefix(self):
        self.assert_failure_unchanged('disconnect');size=self.cache('partial').stat().st_size;self.assertGreater(size,0)
        self.log.write_text('');self.call();self.assertEqual(self.requests()[2]['offset'],size);self.assertTrue(self.cache('delta').exists())
    def test_connection_failure_preserves_existing_prefix(self):
        self.assert_failure_unchanged('disconnect');prefix=self.cache('partial').read_bytes()
        self.assert_failure_unchanged('empty');self.assertEqual(self.cache('partial').read_bytes(),prefix);self.call()
    def test_ignored_invalid_or_unsatisfiable_range_falls_back_once(self):
        for mode in ('range200','range416','wrongrange','wrongrange_cut'):
            with self.subTest(mode=mode):
                for f in (self.state/'patch-cache').glob('*'):f.unlink()
                self.assert_failure_unchanged('disconnect');self.log.write_text('');self.call(mode)
                patch=[x for x in self.requests() if x['name'].endswith('.delta')]
                self.assertGreater(patch[0]['offset'],0);self.assertEqual(patch[1]['offset'],0);self.assertEqual(len(patch),6)
    def test_corrupt_cache_refetched_and_bad_download_discarded(self):
        self.call();p=self.cache('delta');p.write_bytes(b'X'*p.stat().st_size)
        self.assert_failure_unchanged('corrupt');self.assertFalse(p.exists());self.assertFalse(self.cache('partial').exists());self.call()
    def test_oversized_partial_discarded_and_bad_prefix_never_applied(self):
        self.call('disconnect',ok=False);p=self.cache('partial');p.write_bytes(b'X'*1000000);self.log.write_text('');self.call()
        self.assertEqual(self.requests()[2]['offset'],0)
        self.cache('delta').unlink();p.write_bytes(b'X'*10);self.assert_failure_unchanged('normal');self.assertFalse(p.exists())
    def test_symlinks_and_hardlinks_are_rejected(self):
        self.call();p=self.cache('delta');other=self.root/'other';other.write_bytes(p.read_bytes());p.unlink();p.symlink_to(other)
        self.assert_failure_unchanged('normal');p.unlink();os.link(other,p);self.assert_failure_unchanged('normal')
    def test_lru_budget_counts_orphan_and_partial_files(self):
        self.call('disconnect',ok=False);directory=self.state/'patch-cache'
        orphan=directory/('f'*64+'-50000.delta');orphan.write_bytes(b'x'*50000);os.utime(orphan,(1,1))
        self.call(extra={'MTGA_TEST_CACHE_BUDGET':'70000'});self.assertFalse(orphan.exists())
        self.assertLess(sum(p.stat().st_size for p in directory.iterdir()),70000)
    def test_killed_parent_child_retains_lock_until_child_exit(self):
        started=self.root/'started';release=self.root/'release'
        env={**os.environ,'MTGA_TEST_ASSUME_STOPPED':'1',**self.env,'MTGA_FIXTURE_MODE':'wait','MTGA_FIXTURE_STARTED':str(started),'MTGA_FIXTURE_RELEASE':str(release)}
        args=[str(BINARY),'preview','--state',str(self.state),'--profile',str(self.profile),'--steam-library',str(self.library)]
        with (self.root/'process.log').open('w') as out:
            parent=subprocess.Popen(args,env=env,stdout=out,stderr=out)
            try:
                deadline=time.monotonic()+10
                while not started.exists() and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue(started.exists());parent.kill();parent.wait(timeout=5)
                r=self.call(ok=False);self.assertNotEqual(r.returncode,0);self.assertIn('另一项维护操作',r.stderr)
            finally:
                release.touch()
                if parent.poll() is None:parent.kill();parent.wait()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            r=self.call(ok=False)
            if r.returncode==0:break
            time.sleep(.1)
        self.assertEqual(r.returncode,0,r.stderr)

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(CacheTests(name) for name in CacheTests.__dict__ if name.startswith('test_'))
if __name__=='__main__':unittest.main()
