#!/usr/bin/env python3
"""Regression tests for delayed sysfs policy readback (no root required)."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

path = Path(__file__).resolve().parents[1] / "scripts" / "profile_dvfs.py"
spec = importlib.util.spec_from_file_location("profile_dvfs", path)
dvfs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dvfs)

class WriteTests(unittest.TestCase):
    def test_delayed_update(self):
        attr = Mock()
        with patch.object(dvfs, "read", side_effect=["2400000", "2400000", "1500000",
                                                    "1500000", "1500000"]) as read:
            dvfs.write(attr, 1500000, timeout=1, poll_interval=.001)
        attr.write_text.assert_called_once_with("1500000\n")
        self.assertEqual(read.call_count, 5)

    def test_transient_match_is_not_enough(self):
        attr = Mock()
        with patch.object(dvfs, "read", side_effect=["1500000", "2400000", "1500000",
                                                    "1500000", "1500000"]) as read:
            dvfs.write(attr, 1500000, timeout=1, poll_interval=.001)
        self.assertEqual(read.call_count, 5)

    def test_persistent_mismatch(self):
        with patch.object(dvfs, "read", return_value="2400000"):
            with self.assertRaisesRegex(RuntimeError, "Readback timeout"):
                dvfs.write(Mock(), 1500000, timeout=.01, poll_interval=.001)

if __name__ == "__main__":
    unittest.main()
