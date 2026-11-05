"""Native metadata fallback integration; synthetic HTTPS bytes, no real game writes."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest

from publisher import canonical
from tests_subscriber import SubscriberTests


class MetadataFallbackTests(SubscriberTests):
    def setUp(self):
        super().setUp()
        for row in self.release['resources']:
            row['patch']['url'] = 'https://github.com/owner/repo/releases/download/v1/' + row['patch']['name']
        self.resign()
        profile = json.loads(self.profile.read_text())
        profile['feed'] = 'https://raw.githubusercontent.com/owner/repo/main/feed'
        self.profile.write_bytes(canonical(profile))
        fixture = Path(__file__).with_name('fake_curl_fixture.py').resolve()
        fixture.chmod(0o755)
        self.log = self.root / 'metadata-requests.jsonl'
        self.environment = {'MTGA_TEST_CURL': str(fixture), 'MTGA_FIXTURE_FEED': str(self.feed),
                            'MTGA_FIXTURE_LOG': str(self.log)}

    def call(self, mode='normal', command='preview', ok=True):
        return self.invoke(command, ok=ok, env=self.environment | {'MTGA_FIXTURE_MODE': mode})

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def unchanged_failure(self, mode):
        before = self.game_hashes()
        result = self.call(mode, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.game_hashes())
        self.assertFalse((self.state / 'installation.json').exists())
        return result

    def test_primary_metadata_success_uses_no_fallback(self):
        before = self.game_hashes()
        self.call()
        self.assertEqual(before, self.game_hashes())
        self.assertFalse(any(row['host'] == 'api.github.com' for row in self.requests()))

    def test_primary_transport_failure_uses_same_signed_index_and_manifest_via_api(self):
        before = self.game_hashes()
        self.call('metadata-primary-down')
        requests = self.requests()
        metadata = [row for row in requests if row['name'].endswith('.json')]
        self.assertEqual([row['host'] for row in metadata],
                         ['raw.githubusercontent.com', 'api.github.com',
                          'raw.githubusercontent.com', 'api.github.com'])
        self.assertTrue(all(row['query'] == 'ref=main' for row in metadata if row['host'] == 'api.github.com'))
        self.assertTrue(all(row['headers'] == ['Accept: application/vnd.github.raw+json']
                            for row in metadata if row['host'] == 'api.github.com'))
        self.assertTrue(all(row['host'] == 'github.com' for row in requests if row['name'].endswith('.delta')))
        self.assertEqual(before, self.game_hashes())
        self.assertFalse((self.state / 'installation.json').exists())
        for row in self.release['resources']:
            outputs = list((self.state / 'transactions').glob('*/after-' + row['layer']))
            self.assertEqual(len(outputs), 1)

    def test_api_tampering_failures_and_body_bounds_never_modify_game(self):
        for mode in ('metadata-api-tamper', 'metadata-api-fails', 'metadata-api-oversized',
                     'metadata-api-manifest-tamper'):
            with self.subTest(mode=mode):
                self.unchanged_failure(mode)

    def test_successful_malformed_primary_and_http_or_size_failures_never_fallback(self):
        for mode in ('metadata-primary-malformed', 'metadata-primary-http-error',
                     'metadata-primary-oversized'):
            with self.subTest(mode=mode):
                if self.log.exists():
                    self.log.write_text('')
                self.unchanged_failure(mode)
                self.assertFalse(any(row['host'] == 'api.github.com' for row in self.requests()))

    def test_expiry_and_signature_checks_still_apply_to_api_raw_body(self):
        original = copy.deepcopy(self.index)
        self.index['expires_at'] = '2020-01-01T00:00:00Z'
        self.resign()
        expired = self.unchanged_failure('metadata-primary-down')
        self.assertIn('过期', expired.stderr)
        self.index = original
        self.resign()
        path = self.feed / 'index-testing.json'
        envelope = json.loads(path.read_text())
        envelope['signature'] = 'A' * 88
        path.write_bytes(canonical(envelope))
        result = self.unchanged_failure('metadata-primary-down')
        self.assertIn('签名', result.stderr)

    def test_fallback_route_is_exact_and_rejects_noncanonical_feeds_or_assets(self):
        base = 'https://raw.githubusercontent.com/owner/repo/main/feed'
        result = self.invoke('metadata-fallback-url', '--input', 'index-stable.json', '--profile', base,
                             base=False)
        self.assertEqual(result.stdout.strip(),
                         'https://api.github.com/repos/owner/repo/contents/feed/index-stable.json?ref=main')
        for feed, name in [
                (base + '/', 'index-stable.json'), (base + '?token=no', 'index-stable.json'),
                (base.replace('raw.githubusercontent.com', 'example.com'), 'index-stable.json'),
                (base.replace('/main/', '/heads/main/'), 'index-stable.json'),
                (base.replace('/main/', '/%6dain/'), 'index-stable.json'),
                (base.replace('https://', 'http://'), 'index-stable.json'),
                (base.replace('https://', 'https://user@'), 'index-stable.json'),
                (base, 'card.delta'), (base, 'font.full'), (base, '../index.json'),
                (base.replace('/feed', '/other'), 'index-stable.json')]:
            with self.subTest(feed=feed, name=name):
                result = self.invoke('metadata-fallback-url', '--input', name, '--profile', feed,
                                     base=False, ok=False)
                self.assertNotEqual(result.returncode, 0)

    def test_resource_failure_never_uses_contents_api(self):
        self.unchanged_failure('metadata-assets-down')
        self.assertTrue(any(row['name'].endswith('.delta') for row in self.requests()))
        self.assertFalse(any(row['host'] == 'api.github.com' and not row['name'].endswith('.json')
                             for row in self.requests()))

    def test_stable_setup_promotion_reuses_same_safe_metadata_transport(self):
        self.invoke('setup')
        self.release['acceptance'] = {'status': 'accepted', 'evidence_sha256': 'a' * 64,
                                      'scope': 'synthetic accepted fixture'}
        self.index['channel'] = 'stable'
        self.resign()
        incoming = json.loads(self.profile.read_text())
        incoming['channel'] = 'stable'
        self.profile.write_bytes(canonical(incoming))
        before = self.game_hashes()
        self.call('metadata-primary-down', command='setup')
        self.assertEqual(json.loads((self.state / 'profile.json').read_text())['channel'], 'stable')
        self.assertEqual(before, self.game_hashes())
        self.assertTrue(any(row['host'] == 'api.github.com' for row in self.requests()))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(MetadataFallbackTests(name) for name in MetadataFallbackTests.__dict__
                              if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
