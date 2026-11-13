"""Owner-receipt (post-hoc) acceptance policy for stable publication."""
import json
from pathlib import Path
import tempfile
import unittest

import publisher

SUBJECT = {'client_version': 'fixture', 'steam_build': '1', 'inputs': ['a']}
REQUIRED = ('chinese_display', 'font_rendering', 'english_import', 'chinese_import')


def receipt(**over):
    checks = {name: False for name in publisher.CHECKS}
    for name in REQUIRED:
        checks[name] = True
    value = {'schema': 'MTGA_CN_PUBLISHER_ACCEPTANCE_V1',
             'subject_sha256': publisher.hashlib.sha256(publisher.canonical(SUBJECT)).hexdigest(),
             'verification_mode': 'post_hoc', 'owner_verified_at': None,
             'checks': checks, 'tester_quote': '自己进游戏看了一眼，能用',
             'scope': '本机 arm64，中文界面与牌表导入', 'architectures': ['arm64']}
    value.update(over)
    return value


class OwnerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def policy(self, **block):
        value = {'schema': 'MTGA_CN_RELEASE_POLICY_V1', 'owner_authorization': 'x',
                 'routine_text_updates': {}, 'owner_receipt_release': {
                     'enabled': True, 'verification_mode': 'post_hoc',
                     'required_checks': list(REQUIRED), 'independent_review': 'best_effort',
                     'index_validity_days': 180, **block}}
        path = self.root / 'release-policy.json'
        path.write_text(json.dumps(value))
        return path

    def test_post_hoc_receipt_needs_only_the_policy_checks(self):
        arches = publisher.validate_acceptance(receipt(), SUBJECT, self.policy())
        self.assertEqual(arches, ['arm64'])

    def test_post_hoc_requires_an_enabled_policy(self):
        with self.assertRaisesRegex(ValueError, '先发后验'):
            publisher.validate_acceptance(receipt(), SUBJECT, self.policy(enabled=False))
        with self.assertRaisesRegex(ValueError, '先发后验'):
            publisher.validate_acceptance(receipt(), SUBJECT, self.root / 'missing.json')

    def test_missing_required_check_is_rejected(self):
        value = receipt()
        value['checks']['font_rendering'] = False
        with self.assertRaisesRegex(ValueError, 'font_rendering'):
            publisher.validate_acceptance(value, SUBJECT, self.policy())

    def test_strict_mode_still_requires_every_check(self):
        value = receipt(verification_mode='in_game')
        with self.assertRaisesRegex(ValueError, '全部通过'):
            publisher.validate_acceptance(value, SUBJECT, self.policy())

    def test_unknown_mode_and_incomplete_check_set_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '模式无效'):
            publisher.validate_acceptance(receipt(verification_mode='whatever'), SUBJECT, self.policy())
        value = receipt()
        del value['checks']['restart_repeat']
        with self.assertRaisesRegex(ValueError, '检查项集合'):
            publisher.validate_acceptance(value, SUBJECT, self.policy())

    def test_receipt_still_needs_a_quote_scope_and_architecture(self):
        for field in ('tester_quote', 'scope', 'architectures'):
            value = receipt(**{field: '' if field != 'architectures' else []})
            with self.assertRaises(ValueError):
                publisher.validate_acceptance(value, SUBJECT, self.policy())

    def test_post_hoc_does_not_bind_to_another_subject(self):
        value = receipt(subject_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, '不属于'):
            publisher.validate_acceptance(value, SUBJECT, self.policy())

    def test_index_validity_follows_policy_and_defaults_to_thirty_days(self):
        self.assertEqual(publisher.index_validity_days(self.policy()), 180)
        self.assertEqual(publisher.index_validity_days(self.root / 'missing.json'), 30)
        self.assertEqual(publisher.index_validity_days(self.policy(index_validity_days=365)), 365)
        with self.assertRaisesRegex(ValueError, '有效期'):
            publisher.index_validity_days(self.policy(index_validity_days=0))

    def test_shipped_policy_is_enabled(self):
        block = publisher.owner_receipt_policy()
        self.assertIsNotNone(block)
        self.assertEqual(block['verification_mode'], 'post_hoc')
        self.assertEqual(set(block['required_checks']), set(REQUIRED))


if __name__ == '__main__':
    unittest.main()
