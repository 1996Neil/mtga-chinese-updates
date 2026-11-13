"""The metadata fallback may only ever read the same repo/ref/path."""
import unittest

import github_publish


class MetadataRouteTests(unittest.TestCase):
    def test_feed_and_download_metadata_map_to_the_same_repository_path(self):
        self.assertEqual(
            github_publish.derived_metadata_api(
                'https://raw.githubusercontent.com/1996Neil/mtga-chinese-updates/abc1234/feed/index-stable.json',
                2 * 1024 * 1024),
            'https://api.github.com/repos/1996Neil/mtga-chinese-updates/contents/feed/index-stable.json?ref=abc1234')
        self.assertEqual(
            github_publish.derived_metadata_api(
                'https://raw.githubusercontent.com/owner/repo/main/download.json', 4096),
            'https://api.github.com/repos/owner/repo/contents/download.json?ref=main')

    def test_release_assets_never_use_the_contents_api(self):
        self.assertIsNone(github_publish.derived_metadata_api(
            'https://github.com/owner/repo/releases/download/v1/card-abc.delta', 2 * 1024 * 1024))
        self.assertIsNone(github_publish.derived_metadata_api(
            'https://raw.githubusercontent.com/owner/repo/main/README.md', 4096))

    def test_other_hosts_paths_and_oversized_limits_are_rejected(self):
        for url in ('https://example.com/feed/index-stable.json',
                    'https://raw.githubusercontent.com/owner/repo/main/feed/nested/index.json',
                    'https://raw.githubusercontent.com/owner/repo/main/other.json',
                    'http://raw.githubusercontent.com/owner/repo/main/feed/index-stable.json'):
            self.assertIsNone(github_publish.derived_metadata_api(url, 4096), url)
        self.assertIsNone(github_publish.derived_metadata_api(
            'https://raw.githubusercontent.com/owner/repo/main/feed/index-stable.json', 8 * 1024 * 1024))


if __name__ == '__main__':
    unittest.main()
