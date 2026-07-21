import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class WorkerMemoryTest(unittest.TestCase):
    def test_linux_rss_uses_current_proc_value_not_inherited_high_water_mark(self):
        # Linux keeps ru_maxrss across execve.  A recycled worker must ignore that
        # historical maximum and use the current resident pages from /proc instead.
        high_water = mock.Mock(ru_maxrss=9_999_999)
        with mock.patch.object(link_books.sys, "platform", "linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data="100 7 0 0 0 0 0\n")), \
                mock.patch.object(link_books.os, "sysconf", return_value=4096), \
                mock.patch.object(link_books.resource, "getrusage", return_value=high_water):
            self.assertEqual(link_books.rss_bytes(), 7 * 4096)

    def test_zero_progress_over_cap_fails_instead_of_exec_loop(self):
        with self.assertRaisesRegex(RuntimeError, "zero-progress recycle loop"):
            link_books.recycle_needed(current_rss=101, cap=100, processed=0)

    def test_recycle_only_after_progress(self):
        self.assertFalse(link_books.recycle_needed(current_rss=100, cap=100, processed=0))
        self.assertTrue(link_books.recycle_needed(current_rss=101, cap=100, processed=1))


if __name__ == "__main__":
    unittest.main()
