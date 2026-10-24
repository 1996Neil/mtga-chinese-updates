"""Presentation regressions: explain release blocks without changing decisions."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cli
from user_messages import base_message, error_message


def diagnostic(safe=False):
    return {'release': {'client_version': '2026.64.0', 'steam_build': '25763111'},
            'layers': [{'layer': 'card', 'status': 'verified'},
                       {'layer': 'resources', 'status': 'verified' if safe else 'unknown'}],
            'safe_to_rebuild': safe,
            'errors': [] if safe else ['No trusted opaque base for this exact release']}


class MessagesTests(unittest.TestCase):
    def test_new_release_block_explains_action_without_dumping_json_or_mutating_evidence(self):
        result = diagnostic()
        before = copy.deepcopy(result)
        message = error_message(RuntimeError('Official base unresolved: ' + json.dumps(result)))
        self.assertIn('2026.64.0', message)
        self.assertIn('请维护者', message)
        self.assertIn('反复按 3 不会解决', message)
        self.assertIn('构建已停止，未安装', message)
        self.assertNotIn('"safe_to_rebuild"', message)
        self.assertEqual(result, before)

    def test_good_base_does_not_claim_candidate_or_client_acceptance(self):
        message = base_message(diagnostic(True))
        self.assertIn('可以按 3', message)
        self.assertIn('不代表候选已构建', message)

    def test_failed_install_never_claims_no_files_changed(self):
        message = error_message(OSError('injected disk write failure'))
        self.assertIn('injected disk write failure', message)
        self.assertNotIn('未安装', message)
        self.assertNotIn('不修改游戏', message)

    def test_diagnose_json_contract_and_human_mode_keep_same_guard(self):
        result = diagnostic()
        for flags in ([], ['--human']):
            output = io.StringIO()
            with patch('sys.argv', ['mtga-cn', 'diagnose-base', *flags]), \
                 patch('cli.detect', return_value={}), \
                 patch('cli.stable_signature', return_value='same'), \
                 patch('official_base.diagnose', return_value=result), \
                 contextlib.redirect_stdout(output):
                cli.main()
            if flags:
                self.assertIn('暂时不能构建', output.getvalue())
            else:
                self.assertEqual(json.loads(output.getvalue()), result)

    def test_human_diagnosis_still_rejects_resource_drift(self):
        with patch('sys.argv', ['mtga-cn', 'diagnose-base', '--human']), \
             patch('cli.detect', return_value={}), \
             patch('cli.stable_signature', side_effect=['before', 'after']), \
             patch('official_base.diagnose', return_value=diagnostic()):
            with self.assertRaisesRegex(RuntimeError, 'Resources changed during diagnosis'):
                cli.main()

    def test_menu_routes_diagnosis_guide_and_update_failure_without_installing(self):
        source = Path(__file__).resolve().parents[2] / 'bin/MTGA 汉化与双语导入维护.command'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'menu.command'
            script.write_bytes(source.read_bytes())
            fake = root / 'mtga-cn'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$(dirname "$0")/calls"\n'
                            'if [ "$1" = update ]; then echo "构建已停止，未安装。"; exit 1; fi\n')
            fake.chmod(0o755)
            result = subprocess.run(['zsh', str(script)], input='2\n\n6\n\n3\n\n0\n',
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['diagnose-base --human', 'guide', 'update'])
            self.assertIn('操作未完成（退出码 1）', result.stdout)
            self.assertIn('构建失败无需为此回滚', result.stdout)


if __name__ == '__main__':
    unittest.main()
