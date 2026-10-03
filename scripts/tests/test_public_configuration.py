"""Portable startup configuration and an unaccepted fresh installation."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.jianying_local import core, mcp_server, setup_mcp
from scripts.jianying_local.runtime import PROJECT, Settings, ToolError, canonical, file_stamp, load_settings, profile


class ConfigurationTests(unittest.TestCase):
    def write(self, folder, value):
        path = folder / "local.json"
        path.write_bytes(canonical(value))
        return path

    def test_defaults_use_system_directories_and_separate_work(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local"), "CODEX_HOME": str(root / "codex"), "JY_SKILL_ROOT": str(root / "backend")}), patch.dict(os.environ, {"JIANYING_LOCAL_CONFIG": ""}):
                settings = load_settings()
            self.assertEqual(root / "local" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft", settings.drafts_root)
            self.assertEqual(root / "codex" / "config.toml", settings.codex_config)
            self.assertEqual(root / "backend", settings.skill_root)
            self.assertEqual(PROJECT / ".local" / "work", settings.work_root)

    def test_relative_config_paths_are_relative_to_config_not_cwd(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            settings = load_settings(self.write(root, {"work_root": "jobs", "skill_root": "backend"}))
            self.assertEqual(root / "jobs", settings.work_root)
            self.assertEqual(root / "backend", settings.skill_root)

    def test_explicit_cli_override_wins(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            settings = load_settings(self.write(root, {"work_root": "config-jobs"}), work_root=root / "cli-jobs")
            self.assertEqual(root / "cli-jobs", settings.work_root)

    def test_environment_expansion_and_startup_config_selection(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            config = self.write(root, {"work_root": "${JY_TEST_BASE}/jobs"})
            with patch.dict(os.environ, {"JY_TEST_BASE": str(root), "JIANYING_LOCAL_CONFIG": str(config)}):
                self.assertEqual(root / "jobs", load_settings().work_root)

    def test_unknown_config_field_and_canary_flag_rejected(self):
        for key in ("command", "canary", "arbitrary_decode_path"):
            with tempfile.TemporaryDirectory() as task_tmp:
                with self.assertRaises(ToolError) as error:
                    load_settings(self.write(Path(task_tmp), {key: "not-permitted"}))
                self.assertEqual("invalid_config", error.exception.code)

    def test_unresolved_variable_rejected(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            with self.assertRaises(ToolError):
                load_settings(self.write(Path(task_tmp), {"work_root": "${JIANYING_TEST_UNKNOWN_93731}/jobs"}))

    def test_invalid_path_type_and_shell_worker_rejected(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            for value in ({"work_root": ["not", "a", "path"]}, {"worker_python": str(root / "powershell.exe")}):
                with self.assertRaises(ToolError):
                    load_settings(self.write(root, value))

    def test_duplicate_keys_and_oversized_config_rejected(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            path = Path(task_tmp) / "local.json"
            for value in (b'{"work_root":"a","work_root":"b"}', b" " * 65537):
                path.write_bytes(value)
                with self.assertRaises(ToolError):
                    load_settings(path)

    def test_draft_and_work_roots_must_not_overlap(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            with self.assertRaises(ToolError):
                load_settings(self.write(root, {"drafts_root": "native", "work_root": "native/jobs"}))

    def test_startup_config_change_changes_plan_code_profile(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            config = self.write(root, {"skill_root": "backend", "work_root": "jobs"})
            settings = load_settings(config)
            before = profile(settings)
            config.write_bytes(config.read_bytes() + b"\n")
            self.assertNotEqual(before, profile(settings))

    def test_different_explicit_roots_have_different_profiles(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            settings = Settings(root / "native", root / "jobs", root / "backend")
            self.assertNotEqual(profile(settings), profile(replace(settings, work_root=root / "other")))

    def test_no_acceptance_is_shipped_and_production_stops(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            settings = Settings(root / "native", root / "jobs", root / "backend")
            self.assertIsNone(core.accepted(settings))
            with self.assertRaises(ToolError) as error:
                core.require_acceptance(settings)
            self.assertEqual("native_acceptance_required", error.exception.code)
            self.assertFalse((PROJECT / ".local" / "work" / "native_acceptance.json").exists())

    def test_registration_freezes_same_worker_and_all_roots(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            settings = load_settings(self.write(root, {"work_root": "jobs", "skill_root": "backend"}))
            entry = setup_mcp.service_entry(settings)
            self.assertIn(str(settings.worker_python), entry["args"])
            self.assertIn(str(settings.work_root), entry["args"])
            self.assertIn(str(settings.config_path), entry["args"])
            self.assertNotIn("--canary", entry["args"])

    def test_real_cli_uses_config_and_is_unaccepted(self):
        if os.name != "nt":
            self.skipTest("Native process status requires Windows")
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            config = self.write(root, {"drafts_root": "native", "work_root": "jobs", "skill_root": "backend", "codec_root": "codec"})
            result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(PROJECT / "scripts" / "jianying_local" / "cli.py"), "doctor", "--config", str(config)], capture_output=True, timeout=30)
            data = json.loads(result.stdout)
            self.assertTrue(data["ok"], data)
            self.assertEqual(str(root / "jobs"), data["result"]["work_root"])
            self.assertEqual("pending", data["result"]["native_acceptance"])
            self.assertFalse(data["result"]["backend_present"])

    def test_changed_mcp_startup_config_refuses_to_launch_worker(self):
        with tempfile.TemporaryDirectory() as task_tmp:
            root = Path(task_tmp)
            config = self.write(root, {"work_root": "jobs"})
            stamp = file_stamp(config, hash_bytes=True)
            config.write_bytes(config.read_bytes() + b"\n")
            with patch.object(mcp_server, "CONFIG_STAMP", stamp), patch.object(asyncio, "create_subprocess_exec") as child:
                result = asyncio.run(mcp_server.invoke("doctor", {}))
                self.assertEqual("stale_input", result["error"]["code"])
                child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
