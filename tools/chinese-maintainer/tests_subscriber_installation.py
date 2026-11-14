"""Synthetic subscriber recognition; no game, real key, network or state migration."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from tests_base import BaseFixture
from common import fingerprint, read_json, save_json
import official_base as base
import subscriber_installation as subscriber

sys.path.insert(0, str(base.ROOT.parent / 'subscriber'))


class SubscriberInstallationTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.snap['game'] = str(self.game)
        self.subscriber = self.root / 'subscriber'
        self.subscriber.mkdir()
        self.public = self.root / 'public'
        self.public.write_text(read_json(base.ROOT.parent / 'subscriber/profile.json')['public_key'])
        self.client = self.root / 'client'
        self.client.write_bytes(b'synthetic-verifier-not-executed')
        env = patch.dict(os.environ, {
            'MTGA_SUBSCRIBER_STATE': str(self.subscriber),
            'MTGA_PUBLISHER_CLIENT': str(self.client), 'MTGA_PUBLIC_KEY_FILE': str(self.public)})
        env.start()
        self.addCleanup(env.stop)
        self.record = {
            'schema': 'MTGA_CN_SUBSCRIBER_TX_V1', 'state': 'installed',
            'game': str(self.game), 'release_id': 'A-1-r1', 'revision': 1,
            'client_version': 'A', 'steam_build': '1', 'rows': [],
            'protected_files': [
                {'relative': row['relative'], 'fingerprint': base.fp(row)}
                for name, row in self.snap['files'].items() if name not in base.LAYERS] +
                [{'relative': name, 'fingerprint': fp} for name, fp in self.snap['manifests'].items()],
        }
        self.release = {
            'schema': 'MTGA_CN_RELEASE_V1', 'id': 'A-1-r1', 'revision': 1,
            'client_version': 'A', 'steam_build': '1',
            'acceptance': {'status': 'accepted'}, 'resources': [],
            'protected_files': copy.deepcopy(self.record['protected_files']),
        }
        for layer in base.LAYERS:
            original = base.fp(self.snap['files'][layer])
            (self.game / layer).write_text('signed-subscriber-output-' + layer)
            after = fingerprint(self.game / layer)
            self.record['rows'].append({'layer': layer, 'relative': layer,
                                        'original': original, 'before': original, 'after': after})
            self.release['resources'].append({'layer': layer, 'relative': layer,
                                             'input': original, 'output': after})
        self.path = subscriber.manifest_path(self.state, self.record)
        save_json(self.path, {'fixture': 'opaque envelope, verification mocked'})
        self.persist()
        save_json(self.state / 'base-approvals/A-1.json', {
            'run': str(self.run), 'schema': 'synthetic-already-reviewed-capture'})
        verifier = patch.object(subscriber, 'verify_manifest',
                                side_effect=lambda *args: copy.deepcopy(self.release))
        verifier.start()
        self.addCleanup(verifier.stop)

    def persist(self):
        save_json(self.subscriber / 'installation.json', self.record)

    def test_signed_outputs_use_reviewed_capture_without_migrating_either_journal(self):
        journal = fingerprint(self.subscriber / 'installation.json')
        result = self.diagnose()
        self.assertTrue(result['safe_to_rebuild'], result)
        self.assertTrue(all(Path(row['official_base_path']).is_relative_to(self.official)
                            for row in result['layers']))
        self.assertEqual(fingerprint(self.subscriber / 'installation.json'), journal)
        self.assertFalse((self.state / 'installation.json').exists())

    def test_unknown_bytes_or_changed_active_manifest_do_not_become_official(self):
        (self.game / 'resources').write_text('unrecognized')
        self.assertFalse(self.diagnose()['safe_to_rebuild'])
        (self.game / 'resources').write_text('signed-subscriber-output-resources')
        (self.game / 'manifest').write_text('new official manifest generation')
        self.assertFalse(self.diagnose()['safe_to_rebuild'])

    def test_original_backup_is_never_used_when_reviewed_original_is_missing(self):
        backup = self.subscriber / 'transactions/original-card'
        backup.parent.mkdir()
        backup.write_bytes((self.official / 'card').read_bytes())
        (self.official / 'card').unlink()
        self.assertFalse(self.diagnose()['safe_to_rebuild'])

    def test_signature_failure_blocks_recognition(self):
        with patch.object(subscriber, 'verify_manifest', side_effect=RuntimeError('bad signature')):
            result = self.diagnose()
        self.assertFalse(result['safe_to_rebuild'])
        self.assertIn('bad signature', result['errors'])

    def test_other_game_build_partial_install_and_duplicate_rows_are_rejected(self):
        for field, value in (('game', str(self.root / 'other-game')), ('steam_build', '2'),
                             ('state', 'installing'), ('schema', 'unknown')):
            old = self.record[field]
            self.record[field] = value
            self.persist()
            self.assertFalse(self.diagnose()['safe_to_rebuild'], field)
            self.record[field] = old
        self.record['rows'][0] = copy.deepcopy(self.record['rows'][1])
        self.persist()
        self.assertFalse(self.diagnose()['safe_to_rebuild'])

    def test_signed_original_must_match_independently_trusted_capture(self):
        self.record['rows'][0]['original'] = {'sha256': '0' * 64, 'bytes': 1}
        self.release['resources'][0]['input'] = copy.deepcopy(self.record['rows'][0]['original'])
        self.persist()
        result = self.diagnose()
        self.assertFalse(result['safe_to_rebuild'])
        self.assertIn('differs from trusted official base', result['layers'][0]['reason'])

    def test_clean_official_update_does_not_consult_stale_subscriber_metadata(self):
        for layer in base.LAYERS:
            (self.game / layer).write_bytes((self.official / layer).read_bytes())
        self.record['state'] = 'installing'
        self.persist()
        with patch.object(subscriber, 'recognize', side_effect=AssertionError('stale journal consulted')):
            self.assertTrue(self.diagnose()['safe_to_rebuild'])
        with patch('github_publish.download', side_effect=AssertionError('network invoked')):
            subscriber.prepare({}, self.inspect(), self.state)

    def test_diagnostics_are_offline_and_leave_all_files_unchanged(self):
        before = {str(p): fingerprint(p) for p in self.root.rglob('*') if p.is_file()}
        with patch('github_publish.download', side_effect=AssertionError('network invoked')):
            self.assertTrue(self.diagnose()['safe_to_rebuild'])
        self.assertEqual(before, {str(p): fingerprint(p) for p in self.root.rglob('*') if p.is_file()})

    def test_symlinked_journal_is_rejected(self):
        journal = self.subscriber / 'installation.json'
        target = self.subscriber / 'other.json'
        journal.rename(target)
        journal.symlink_to(target)
        self.assertFalse(self.diagnose()['safe_to_rebuild'])

    def prepare_download_fixture(self):
        content = self.path.read_bytes()
        expected = fingerprint(self.path)
        self.path.unlink()
        index = {'schema': 'MTGA_CN_INDEX_V1', 'channel': 'stable',
                 'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(),
                 'releases': [{'id': self.record['release_id'], 'client_version': 'A',
                               'steam_build': '1', 'manifest': {
                                   'name': self.path.name, 'fingerprint': expected}}]}
        config = {'client': str(self.client), 'public_key_file': str(self.public)}

        def download(url, destination, *args):
            Path(destination).write_bytes(b'index' if url.endswith('index-stable.json') else content)

        def verify(path, *args):
            return index if Path(path).name == 'index-stable.json' else self.release

        return config, download, verify

    def test_only_opaque_layers_patched_still_prepare_signed_metadata(self):
        for layer in ('card', 'client', 'font_bundle'):
            (self.game / layer).write_bytes((self.official / layer).read_bytes())
        config, download, verify = self.prepare_download_fixture()
        with patch('github_publish.download', side_effect=download) as fetch, \
             patch.object(subscriber, 'verify_manifest', side_effect=verify):
            subscriber.prepare(config, self.inspect(), self.state)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(self.path.is_file())

    def test_interrupted_download_never_poison_final_manifest_cache(self):
        config, download, verify = self.prepare_download_fixture()

        def interrupted(url, destination, *args):
            download(url, destination, *args)
            if not url.endswith('index-stable.json'):
                Path(destination).write_bytes(b'partial')
                raise RuntimeError('transport interrupted')

        with patch.object(subscriber, 'verify_manifest', side_effect=verify):
            with patch('github_publish.download', side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, 'transport interrupted'):
                    subscriber.prepare(config, self.inspect(), self.state)
            self.assertFalse(self.path.exists())
            with patch('github_publish.download', side_effect=download):
                subscriber.prepare(config, self.inspect(), self.state)
        self.assertTrue(self.path.is_file())
