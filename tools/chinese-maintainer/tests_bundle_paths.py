"""CRC output restrictions follow configured state without accepting escapes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bundle_crc
import common


class BundleOutputPathsTests(unittest.TestCase):
    def test_configured_cache_state_is_accepted_but_outside_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / 'cache/state'
            state.mkdir(parents=True)
            with patch.object(common, 'STATE', state):
                path = state / 'runs/new/fonts.mtga'
                self.assertEqual(bundle_crc.tmp_output(path), path)
                with self.assertRaises(RuntimeError):
                    bundle_crc.tmp_output(root / 'outside.mtga')
                with self.assertRaises(RuntimeError):
                    bundle_crc.tmp_output(state)

    def test_symlink_and_traversal_are_rejected_even_if_destination_is_inside(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve()
            (state / 'real').mkdir()
            (state / 'alias').symlink_to(state / 'real')
            with patch.object(common, 'STATE', state):
                for path in (state / 'alias/fonts.mtga', state / 'real/../fonts.mtga'):
                    with self.assertRaises(RuntimeError):
                        bundle_crc.tmp_output(path)


if __name__ == '__main__':
    unittest.main()
