"""Download selection against tiny signed fixtures; never real MTGA/network."""
from tests_cache import CacheTests


class DownloadRouteTests(CacheTests):
    def configure(self, mode='auto', **values):
        args = ['network', '--network-mode', mode]
        for name, value in values.items():
            args += ['--' + name, value]
        return self.invoke(*args)

    def test_auto_falls_back_to_proxy_and_reuses_it_for_remaining_files(self):
        self.configure(proxy='http://127.0.0.1:7897')
        before = self.game_hashes()
        self.call('route-direct-down')
        calls = self.requests()
        self.assertEqual([c['host'] for c in calls[:3]],
                         ['raw.githubusercontent.com', 'api.github.com', 'raw.githubusercontent.com'])
        self.assertTrue(all(c['proxy'] == '' for c in calls[:2]))
        self.assertTrue(all(c['proxy'] == 'http://127.0.0.1:7897' for c in calls[2:]))
        self.assertEqual(before, self.game_hashes())
        self.assertFalse((self.state / 'installation.json').exists())

    def test_channel_switch_resumes_verified_http_prefix(self):
        self.configure(proxy='http://127.0.0.1:7897')
        self.call('route-direct-cut')
        patches = [r for r in self.requests() if r['name'].endswith('.delta')]
        self.assertEqual(patches[0]['offset'], 0)
        self.assertEqual(patches[0]['proxy'], '')
        self.assertGreater(patches[1]['offset'], 0)
        self.assertEqual(patches[1]['proxy'], 'http://127.0.0.1:7897')
        self.assertTrue(self.cache('delta').exists())

    def test_direct_explicitly_ignores_inherited_proxy(self):
        self.configure('direct', proxy='http://127.0.0.1:7897')
        self.call(extra={'HTTPS_PROXY': 'http://127.0.0.1:9999'})
        self.assertTrue(all(r['proxy'] == '' and r['noproxy'] == '*' for r in self.requests()))

    def test_proxy_mode_never_falls_back_to_direct(self):
        self.configure('proxy', proxy='socks5h://127.0.0.1:1080')
        self.assert_failure_unchanged('route-proxy-down')
        self.assertTrue(all(r['proxy'] == 'socks5h://127.0.0.1:1080' for r in self.requests()))

    def test_mirror_uses_same_signed_files_without_changing_trust(self):
        profile = self.profile.read_bytes()
        before = self.game_hashes()
        self.configure('mirror', mirror='https://mirror.example/mtga')
        self.call()
        self.assertTrue(all(r['host'] == 'mirror.example' for r in self.requests()))
        for request in self.requests():
            original = ('raw.githubusercontent.com/owner/repo/main/feed/'
                        if request['name'].endswith('.json')
                        else 'github.com/owner/repo/releases/download/v1/')
            self.assertEqual(request['path'], '/mtga/' + original + request['name'])
        self.assertEqual(profile, self.profile.read_bytes())
        self.assertEqual(before, self.game_hashes())

    def test_mirror_tampering_stops_instead_of_trying_other_sources(self):
        self.configure('mirror', mirror='https://mirror.example/mtga')
        self.assert_failure_unchanged('route-mirror-tamper')
        self.assertEqual(len(self.requests()), 1)

    def test_unconfigured_mirror_and_credential_urls_are_not_saved(self):
        for args in [
                ('--network-mode', 'mirror'),
                ('--proxy', 'http://user:password@127.0.0.1:7897'),
                ('--proxy', 'file:///tmp/socket'),
                ('--mirror', 'http://mirror.example'),
                ('--mirror', 'https://user:password@mirror.example'),
                ('--mirror', 'https://mirror.example/a/../b')]:
            result = self.invoke('network', *args, ok=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.state / 'network.json').exists())

    def test_network_settings_do_not_require_game_and_setup_preserves_them(self):
        self.configure(proxy='http://127.0.0.1:7897')
        settings = (self.state / 'network.json').read_bytes()
        self.invoke('setup')
        self.assertEqual(settings, (self.state / 'network.json').read_bytes())
        launcher = (self.state / 'MTGA 中文维护.command').read_text()
        self.assertIn('7 下载设置', launcher)
        self.assertIn('--network-mode auto', launcher)
        (self.state / 'network.json').write_text('invalid')
        result = self.invoke('status')
        self.assertIn('尚无', result.stdout)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromNames([
        name for name in DownloadRouteTests.__dict__ if name.startswith('test_')
    ], DownloadRouteTests)
