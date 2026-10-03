"""Pure config tests and fail-closed gate: no SDK install or user config write."""
import copy
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.jianying_local import setup_mcp
from scripts.jianying_local.runtime import Settings, ToolError


class SetupTests(unittest.TestCase):
    def original(self):
        return b"# preserve this comment\nmodel = 'fixture-model'\n[mcp_servers.peer]\ncommand='fixture-peer'\n[mcp_servers.peer.env]\nFIXTURE_TOKEN='unchanged-fixture-not-a-real-secret'\n"

    def test_additive_config_preserves_bytes_and_other_settings(self):
        raw=self.original();entry=setup_mcp.service_entry()
        after,block=setup_mcp.prepare_config(raw,entry)
        self.assertTrue(after.startswith(raw))
        self.assertEqual(setup_mcp.without_service(tomllib.loads(raw.decode())),setup_mcp.without_service(tomllib.loads(after.decode())))
        self.assertEqual(entry,tomllib.loads(after.decode())["mcp_servers"]["jianying-local"])

    def test_conflict_never_replaces_existing_registration(self):
        after,_=setup_mcp.prepare_config(self.original(),setup_mcp.service_entry())
        with self.assertRaises(ToolError) as ctx:setup_mcp.prepare_config(after,setup_mcp.service_entry())
        self.assertEqual("config_conflict",ctx.exception.code)

    def test_rollback_removes_only_owned_service_and_keeps_later_peer(self):
        entry=setup_mcp.service_entry();after,block=setup_mcp.prepare_config(self.original(),entry)
        later=after+b"\n[mcp_servers.new_peer]\ncommand='new-peer-added-later'\n"
        removed=setup_mcp.remove_block(later,block,entry)
        parsed=tomllib.loads(removed.decode())
        self.assertNotIn("jianying-local",parsed["mcp_servers"])
        self.assertEqual("new-peer-added-later",parsed["mcp_servers"]["new_peer"]["command"])
        self.assertEqual("unchanged-fixture-not-a-real-secret",parsed["mcp_servers"]["peer"]["env"]["FIXTURE_TOKEN"])

    def test_changed_service_block_refuses_rollback(self):
        entry=setup_mcp.service_entry();after,block=setup_mcp.prepare_config(self.original(),entry)
        altered=after.replace(b"startup_timeout_sec = 20",b"startup_timeout_sec = 30")
        with self.assertRaises(ToolError) as ctx:setup_mcp.remove_block(altered,block,entry)
        self.assertEqual("config_changed",ctx.exception.code)

    def test_native_gate_prevents_install_or_config_writes(self):
        with tempfile.TemporaryDirectory(prefix="jy-setup-gate-") as task_tmp:
            root=Path(task_tmp);native=root/"native";native.mkdir()
            settings=Settings(native,root/"work",Settings().skill_root,False)
            with patch.object(setup_mcp.venv.EnvBuilder,"create") as installer,patch.object(setup_mcp,"atomic_write") as writer:
                with self.assertRaises(ToolError) as ctx:setup_mcp.install(settings)
                self.assertEqual("native_acceptance_required",ctx.exception.code)
                with self.assertRaises(ToolError):setup_mcp.register(settings)
                installer.assert_not_called();writer.assert_not_called()


if __name__=="__main__":unittest.main()
