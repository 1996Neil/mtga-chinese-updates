"""Native channel migration preserves installed resources and rollback records."""
import copy
import json
from tests_subscriber import SubscriberTests
from publisher import canonical


class ChannelPromotionTests(SubscriberTests):
    def prepare_promotion(self):
        self.invoke('setup')
        self.invoke('update')
        self.before = self.game_hashes()
        self.journal_bytes = (self.state / 'installation.json').read_bytes()
        self.old_profile = (self.state / 'profile.json').read_bytes()
        self.old_trust = (self.state / 'trust.json').read_bytes()
        self.release['acceptance']['status'] = 'accepted'
        self.release['acceptance']['evidence_sha256'] = 'a' * 64
        self.index['channel'] = 'stable'
        self.resign()
        incoming = json.loads(self.profile.read_text())
        incoming['channel'] = 'stable'
        self.profile.write_bytes(canonical(incoming))

    def test_promotion_preserves_backups_and_supports_repeat_setup_and_rollback(self):
        self.prepare_promotion()
        self.invoke('setup')
        for name in ('profile.json', 'trust.json'):
            self.assertEqual(json.loads((self.state / name).read_text())['channel'], 'stable')
        self.assertEqual((self.state / 'profile.json.before-stable').read_bytes(), self.old_profile)
        self.assertEqual((self.state / 'trust.json.before-stable').read_bytes(), self.old_trust)
        self.assertEqual((self.state / 'installation.json').read_bytes(), self.journal_bytes)
        self.assertEqual(self.game_hashes(), self.before)
        self.invoke('setup')
        self.invoke('check')
        self.invoke('rollback')
        self.assertEqual(json.loads((self.state / 'installation.json').read_text())['state'], 'rolled-back')

    def test_bad_signature_does_not_change_profile_or_trust(self):
        self.prepare_promotion()
        path = self.feed / 'index-stable.json'
        envelope = json.loads(path.read_text())
        envelope['signature'] = 'A' * 88
        path.write_bytes(canonical(envelope))
        result = self.invoke('setup', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.state / 'profile.json').read_bytes(), self.old_profile)
        self.assertEqual((self.state / 'trust.json').read_bytes(), self.old_trust)
        self.assertEqual(self.game_hashes(), self.before)

    def test_changed_feed_is_rejected_before_profile_update(self):
        self.prepare_promotion()
        incoming = json.loads(self.profile.read_text())
        incoming['feed'] += '-other'
        self.profile.write_bytes(canonical(incoming))
        self.assertNotEqual(self.invoke('setup', ok=False).returncode, 0)
        self.assertEqual((self.state / 'profile.json').read_bytes(), self.old_profile)
        self.assertEqual((self.state / 'trust.json').read_bytes(), self.old_trust)

    def test_interrupted_pair_update_can_be_retried(self):
        self.prepare_promotion()
        result = self.invoke('setup', ok=False, env={'MTGA_TEST_INTERRUPT_CHANNEL_PROMOTION': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.invoke('setup')
        self.invoke('check')
        self.assertEqual((self.state / 'installation.json').read_bytes(), self.journal_bytes)
        self.assertEqual(self.game_hashes(), self.before)

    def test_active_installation_blocks_promotion(self):
        self.prepare_promotion()
        journal = json.loads(self.journal_bytes)
        journal['state'] = 'installing'
        (self.state / 'installation.json').write_bytes(canonical(journal))
        self.assertNotEqual(self.invoke('setup', ok=False).returncode, 0)
        self.assertEqual((self.state / 'profile.json').read_bytes(), self.old_profile)
        self.assertEqual((self.state / 'trust.json').read_bytes(), self.old_trust)


def load_tests(loader, tests, pattern):
    suite = __import__('unittest').TestSuite()
    for name in ChannelPromotionTests.__dict__:
        if name.startswith('test_'):
            suite.addTest(ChannelPromotionTests(name))
    return suite
