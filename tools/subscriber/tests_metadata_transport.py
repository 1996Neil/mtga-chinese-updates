"""Anonymous same-repository metadata fallback; no native process or network."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import github_publish as publisher


class MetadataTransportTests(unittest.TestCase):
    URL = 'https://raw.githubusercontent.com/owner/project/main/feed/index-stable.json'
    API = 'https://api.github.com/repos/owner/project/contents/feed/index-stable.json?ref=main'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'metadata.json'

    def response(self, code, data=b'fixture'):
        def invoke(argv, **kwargs):
            self.path.write_bytes(data)
            return subprocess.CompletedProcess(argv, code, '', 'fixture network error' if code else '')
        return invoke

    def test_fallback_identity_fixed_ref_path_and_no_credentials(self):
        codes = iter((35, 0))
        commands = []

        def invoke(argv, **kwargs):
            commands.append(argv)
            self.path.write_bytes(b'fixture')
            return subprocess.CompletedProcess(argv, next(codes), '', '')

        with patch.object(publisher.subprocess, 'run', side_effect=invoke):
            publisher.download(self.URL, self.path, 1024)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][-1], self.URL)
        self.assertEqual(commands[1][-1], self.API)
        self.assertIn('Accept: application/vnd.github.raw+json', commands[1])
        for argv in commands:
            self.assertIn('--disable', argv)
            self.assertIn('--proto', argv)
            self.assertIn('=https', argv)
            self.assertNotIn('--insecure', argv)
            self.assertNotIn('-k', argv)
            self.assertFalse(any('Authorization' in arg or 'token' in arg for arg in argv))

    def test_exact_commit_download_pointer_supported(self):
        commit = 'a' * 40
        url = f'https://raw.githubusercontent.com/team/repo/{commit}/download.json'
        self.assertEqual(publisher.derived_metadata_api(url, 2 * 1024 * 1024),
                         f'https://api.github.com/repos/team/repo/contents/download.json?ref={commit}')

    def test_malformed_urls_and_resources_never_get_fallback(self):
        bad = (
            self.URL.replace('https://', 'http://'),
            self.URL.replace('raw.githubusercontent.com', 'raw.githubusercontent.com.evil'),
            self.URL.replace('raw.githubusercontent.com/', 'user@raw.githubusercontent.com/'),
            self.URL.replace('.com/', '.com:443/'),
            self.URL + '?token=secret', self.URL + '#fragment',
            self.URL.replace('/main/', '/feature%2Fbranch/'),
            self.URL.replace('/main/', '/../'),
            self.URL.replace('index-stable.json', '../index-stable.json'),
            self.URL.replace('index-stable.json', '%69ndex-stable.json'),
            self.URL.replace('/feed/index-stable.json', '/private.json'),
            self.URL.replace('/feed/index-stable.json', '/feed/patch.delta'),
            'https://github.com/owner/project/releases/download/tag/card.full',
        )
        for url in bad:
            with self.subTest(url=url):
                self.assertIsNone(publisher.derived_metadata_api(url, 1024))
        self.assertIsNone(publisher.derived_metadata_api(self.URL, 2 * 1024 * 1024 + 1))
        self.assertIsNone(publisher.derived_metadata_api(self.URL, True))
        self.assertIsNone(publisher.derived_metadata_api(self.URL, 0))

    def test_primary_success_never_falls_back(self):
        with patch.object(publisher.subprocess, 'run', side_effect=self.response(0)) as call:
            publisher.download(self.URL, self.path, 1024)
        self.assertEqual(call.call_count, 1)

    def test_http_and_size_failures_never_fall_back(self):
        for code in (22, 63):
            with self.subTest(code=code):
                with patch.object(publisher.subprocess, 'run', side_effect=self.response(code)) as call:
                    with self.assertRaises(RuntimeError):
                        publisher.download(self.URL, self.path, 1024)
                self.assertEqual(call.call_count, 1)

    def test_every_allowed_transport_failure_can_retry_same_source(self):
        for code in (5, 6, 7, 18, 28, 35, 52, 55, 56, 60):
            with self.subTest(code=code):
                results = iter((code, 0))

                def invoke(argv, **kwargs):
                    self.path.write_bytes(b'fixture')
                    return subprocess.CompletedProcess(argv, next(results), '', '')

                with patch.object(publisher.subprocess, 'run', side_effect=invoke) as call:
                    publisher.download(self.URL, self.path, 1024)
                self.assertEqual(call.call_count, 2)

    def test_api_error_is_not_silently_successful(self):
        with patch.object(publisher.subprocess, 'run',
                          side_effect=self.response(35)) as call:
            with self.assertRaises(RuntimeError):
                publisher.download(self.URL, self.path, 1024)
        self.assertEqual(call.call_count, 2)

    def test_successful_oversized_body_is_still_rejected(self):
        with patch.object(publisher.subprocess, 'run', side_effect=self.response(0, b'x' * 1025)):
            with self.assertRaisesRegex(ValueError, '大小限制'):
                publisher.download(self.URL, self.path, 1024)

    def test_resource_transport_retains_existing_path(self):
        url = 'https://github.com/owner/project/releases/download/tag/card.full'
        with patch.object(publisher, 'run') as run:
            publisher.download(url, self.path, 4096)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], url)
        self.assertNotIn('--header', argv)


if __name__ == '__main__':
    unittest.main()
