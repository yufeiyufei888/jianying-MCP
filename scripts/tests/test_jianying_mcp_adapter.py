"""No SDK installation: test only fixed worker command and exact tool surface."""
import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from scripts.jianying_local import mcp_server as adapter
from scripts.jianying_local.runtime import Settings


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_worker_is_real_cli_not_shell(self):
        result = await adapter.invoke("doctor", {})
        self.assertTrue(result["ok"],result)
        self.assertEqual((Settings().vendor / "script_file.py").is_file(), result["result"]["backend_present"])
        self.assertFalse(result["result"]["network"])

    async def test_no_arbitrary_commands_or_root_overrides(self):
        result=await adapter.invoke("powershell.exe", {"command":"anything"})
        self.assertEqual("unknown_operation",result["error"]["code"])
        result=await adapter.invoke("doctor", {"work_root":"/not-an-allowed-tool-argument"})
        self.assertEqual("invalid_input",result["error"]["code"])

    async def test_only_six_tool_definitions_and_no_sdk_or_scan_during_import(self):
        callbacks={}
        class FakeMCP:
            def __init__(self,*a,**kw):pass
            def tool(self,**kwargs):
                def decorate(fn): callbacks[fn.__name__]=(fn,kwargs); return fn
                return decorate
        class FakeAnnotations:
            def __init__(self,**kw):self.values=kw
        fast=types.ModuleType("mcp.server.fastmcp");fast.FastMCP=FakeMCP
        types_mod=types.ModuleType("mcp.types");types_mod.ToolAnnotations=FakeAnnotations
        with patch.dict(sys.modules,{"mcp.server.fastmcp":fast,"mcp.types":types_mod}),patch.object(adapter,"invoke") as worker:
            adapter.create_server()
            worker.assert_not_called()
        self.assertEqual(adapter.TOOLS,set(callbacks))
        self.assertTrue(callbacks["doctor"][1]["annotations"].values["readOnlyHint"])
        self.assertFalse(callbacks["plan_draft"][1]["annotations"].values["readOnlyHint"])
        self.assertFalse(callbacks["apply_plan"][1]["annotations"].values["destructiveHint"])

    async def test_worker_environment_does_not_forward_api_secrets(self):
        with patch.dict(os.environ,{"SECRET_API_KEY":"do-not-forward","PYTHONPATH":"injected","PATH":"runtime"}):
            env=adapter.worker_env()
            self.assertNotIn("SECRET_API_KEY",env)
            self.assertNotIn("PYTHONPATH",env)
            self.assertEqual("runtime",env["PATH"])


if __name__=="__main__":unittest.main()
