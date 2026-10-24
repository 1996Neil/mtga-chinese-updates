"""Bounded v1.2 regression: no game resources are built or written."""
import json
from pathlib import Path
import unittest
from common import safe_format, format_metadata, adapt_verified_format

class IndentTests(unittest.TestCase):
    def test_state_indent_does_not_nest_under_bold(self):
        en='<b><indent=5%>• Rules</b><color #CCCCCC> detail'
        zh='<b><indent=5%>• 规则</b><color #CCCCCC> 说明'
        self.assertFalse(safe_format(en,zh))
    def test_actual_batch_only_one_unblocked(self):
        rows=json.loads((Path(__file__).parent/'fixtures/round1-format.json').read_text())
        passed=[]
        for row in rows:
            if not safe_format(row['english'],row['chinese']):passed.append(row['key'])
        self.assertEqual(passed,['Codex/ThePlayBlade_FindMatch'])
    def test_changed_indent_attributes_remain_blocked(self):
        self.assertTrue(safe_format('<b><indent=5%>A</b>','<b><indent=8%>甲</b>'))
    def test_tag_reorder_remains_blocked(self):
        self.assertTrue(safe_format('<b><indent=5%>A</b>','<indent=5%><b>甲</b>'))
    def test_same_invalid_bold_italic_nesting_remains_blocked(self):
        self.assertTrue(safe_format('<b><i><indent=5%>A</b></i>','<b><i><indent=5%>甲</b></i>'))
    def test_icu_braces_not_waived(self):
        self.assertTrue(safe_format('<b><indent=5%>A</b>','<b><indent=5%>甲{</b>'))
    def test_missing_parameter_not_waived(self):
        self.assertTrue(safe_format('<b><indent=5%>{0}</b>','<b><indent=5%>甲</b>'))
    def test_changed_critical_link_not_waived(self):
        self.assertTrue(safe_format('<b><indent=5%><link=a>A</link></b>','<b><indent=5%><link=b>甲</link></b>'))
    def test_invalid_indent_value_not_waived(self):
        self.assertTrue(safe_format('<b><indent=oops>A</b>','<b><indent=oops>甲</b>'))
    def test_strikeout_cannot_be_removed(self):
        self.assertTrue(safe_format('Destroy <s>[attacking]</s> creature.','消灭［攻击］生物。'))
    def test_old_strict_metadata_stays_available(self):
        self.assertFalse(format_metadata('<b><indent=5%>A</b>')['balanced'])
        self.assertTrue(format_metadata('<b><indent=5%>A</b>',tmp_indent_state=True)['balanced'])

class ICURepairTests(unittest.TestCase):
    def test_actual_case_repairs_syntax_only(self):
        rows=json.loads((Path(__file__).parent/'fixtures/round1-format.json').read_text())
        row=next(r for r in rows if r['key']=='615435')
        adapted,repair=adapt_verified_format(row['english'],row['chinese'])
        self.assertEqual(adapted,row['chinese'].replace('}另外{','}other{'))
        self.assertEqual(repair,'restored-ICU-other-selector');self.assertFalse(safe_format(row['english'],adapted))
    def test_never_replace_plain_prose(self):
        zh='{a}另外{b}'
        self.assertEqual(adapt_verified_format('{a} also {b}',zh),(zh,None))
    def test_never_replace_branch_body_prose(self):
        zh='{n, plural, one {{a}另外{b}} other {其他}}'
        self.assertEqual(adapt_verified_format('{n, plural, one {{a} also {b}} other {Others}}',zh),(zh,None))
    def test_placeholder_loss_still_blocks(self):
        zh='{n, plural, one {一个}另外{数个}}'
        en='{n, plural, one {One {value}} other {Many}}'
        self.assertEqual(adapt_verified_format(en,zh),(zh,None))
    def test_no_repair_for_changed_selector(self):
        zh='{n, plural, =2 {一个}另外{数个}}'
        self.assertEqual(adapt_verified_format('{n, plural, =1 {One} other {Many}}',zh),(zh,None))
    def test_duplicate_other_refused(self):
        zh='{n, plural, one {一}other{其他}另外{再次}}'
        self.assertEqual(adapt_verified_format('{n, plural, one {One} other {Others}}',zh),(zh,None))
    def test_broken_source_never_admits(self):
        zh='{n, plural, one {一}另外{其他}}'
        self.assertEqual(adapt_verified_format('{broken',zh),(zh,None))

if __name__=='__main__':unittest.main(verbosity=2)
