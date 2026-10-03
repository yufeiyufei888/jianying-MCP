"""Fast Windows process checks must remain fail-closed, without launching a shell."""
import ctypes
import os
import unittest
from unittest.mock import Mock, patch

from scripts.jianying_local.runtime import ToolError, app_pids


@unittest.skipUnless(os.name == "nt", "Windows system API only")
class ProcessSnapshotTests(unittest.TestCase):
    def kernel(self, rows, ending=18):
        sequence = iter(rows)
        def step(handle, pointer):
            try:
                pid, name = next(sequence)
                pointer._obj.th32ProcessID = pid
                pointer._obj.szExeFile = name
                return 1
            except StopIteration:
                ctypes.set_last_error(ending)
                return 0
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot = Mock(return_value=123)
        kernel.Process32FirstW = Mock(side_effect=step)
        kernel.Process32NextW = Mock(side_effect=step)
        kernel.CloseHandle = Mock(return_value=1)
        return kernel

    def test_case_insensitive_all_editor_instances_sorted(self):
        kernel = self.kernel([(7, "JianyingPro.exe"), (2, "other.exe"), (4, "JIANYINGPRO.EXE")])
        with patch("ctypes.WinDLL", return_value=kernel):
            self.assertEqual([4, 7], app_pids())
        kernel.CloseHandle.assert_called_once_with(123)

    def test_empty_snapshot_only_normal_end_is_success(self):
        with patch("ctypes.WinDLL", return_value=self.kernel([])):
            self.assertEqual([], app_pids())
        with patch("ctypes.WinDLL", return_value=self.kernel([], ending=5)):
            with self.assertRaises(ToolError):
                app_pids()

    def test_failed_snapshot_never_looks_like_closed_editor(self):
        for handle in (None, 0, ctypes.c_void_p(-1).value):
            kernel = self.kernel([])
            kernel.CreateToolhelp32Snapshot.return_value = handle
            with patch("ctypes.WinDLL", return_value=kernel):
                with self.assertRaises(ToolError):
                    app_pids()
            kernel.CloseHandle.assert_not_called()

    def test_mid_enumeration_failure_stops_and_closes_own_handle(self):
        kernel = self.kernel([(5, "other.exe")], ending=5)
        with patch("ctypes.WinDLL", return_value=kernel):
            with self.assertRaises(ToolError):
                app_pids()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_invalid_pid_and_close_failure_rejected(self):
        with patch("ctypes.WinDLL", return_value=self.kernel([(0, "JianyingPro.exe")])):
            with self.assertRaises(ToolError):
                app_pids()
        kernel = self.kernel([])
        kernel.CloseHandle.return_value = 0
        with patch("ctypes.WinDLL", return_value=kernel):
            with self.assertRaises(ToolError):
                app_pids()

    def test_real_process_snapshot_starts_no_shell(self):
        with patch("subprocess.run") as shell:
            result = app_pids()
        self.assertIsInstance(result, list)
        self.assertTrue(all(type(pid) is int and pid > 0 for pid in result))
        shell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
