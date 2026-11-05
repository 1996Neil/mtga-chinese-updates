"""A moved LocId must not drop a translation that is otherwise identical."""
import json
import unittest

from portable_registry import PortableBaseline


def context(english, use, cards=None):
    value = {'english': english, 'field_use': use}
    if cards is not None:
        value['identity'] = {'cards': cards}
    return value


class LocIdDriftFallbackTests(unittest.TestCase):
    def baseline(self):
        b = object.__new__(PortableBaseline)
        b.rows = {'card': {}, 'client': {}, 'ui': {}}
        b.titles = []
        b.title_rows = {647: {'title_id': 647, 'english': 'Forest', 'chinese': '树林', 'grp_id': 6993}}
        b.text_index = {}
        return b

    def test_title_identity_match_recovers_a_moved_locid(self):
        b = self.baseline()
        ctx = context('Forest', ['Cards.SubtypeTextId', 'Cards.TitleId', 'Enums.SubType'],
                      [{'GrpId': 6993, 'TitleId': 647}, {'GrpId': 7100, 'TitleId': 647}])
        self.assertEqual(b.check('card', '1250', ctx), ('树林', 'portable-title-identity-match'))

    def test_title_match_needs_the_same_printing(self):
        b = self.baseline()
        ctx = context('Forest', ['Cards.TitleId'], [{'GrpId': 1, 'TitleId': 647}])
        self.assertEqual(b.check('card', '1250', ctx)[0], None)

    def test_title_match_needs_the_same_english(self):
        b = self.baseline()
        ctx = context('Forests', ['Cards.TitleId'], [{'GrpId': 6993, 'TitleId': 647}])
        self.assertEqual(b.check('card', '1250', ctx)[0], None)

    def test_text_match_needs_exact_english_and_field_use(self):
        b = self.baseline()
        b.text_index[('card', 'Flying', json.dumps(['Cards.AbilityTextId'], sort_keys=True))] = {'飞行'}
        hit = context('Flying', ['Cards.AbilityTextId'])
        self.assertEqual(b.check('card', '999', hit), ('飞行', 'portable-text-context-match'))
        wrong_use = context('Flying', ['Cards.ReminderTextId'])
        self.assertIsNone(b.check('card', '999', wrong_use)[0])

    def test_two_agreeing_sources_are_required(self):
        b = self.baseline()
        signature = ('card', 'Flying', json.dumps(['Cards.AbilityTextId'], sort_keys=True))
        b.text_index[signature] = {'飞行', '飞翔'}
        self.assertIsNone(b.check('card', '999', context('Flying', ['Cards.AbilityTextId']))[0])

    def test_unknown_field_use_is_never_adopted(self):
        b = self.baseline()
        b.text_index[('card', 'Flying', json.dumps([]))] = {'飞行'}
        self.assertEqual(b.check('card', '999', context('Flying', [])),
                         (None, 'portable-unknown-field-use'))

    def test_a_known_locid_still_uses_the_strict_path(self):
        b = self.baseline()
        b.rows['card']['7'] = {'english': 'Flying', 'chinese': '飞行', 'field_use': ['Cards.AbilityTextId'],
                               'context_sha256': 'stale', 'disposition': 'translated'}
        ctx = context('Flying', ['Cards.AbilityTextId'], [{'GrpId': 1, 'TitleId': 2}])
        self.assertEqual(b.check('card', '7', ctx), (None, 'portable-identity-or-use-changed'))


if __name__ == '__main__':
    unittest.main()
