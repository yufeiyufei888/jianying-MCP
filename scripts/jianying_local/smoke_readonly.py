"""Real SDK transport test against temporary synthetic metadata, never native drafts."""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from scripts.jianying_local.core import OPERATIONS
from scripts.jianying_local.runtime import canonical, require, write_new


async def smoke():
    require(importlib.metadata.version("mcp") == "1.26.0", "sdk_version", "Use the pinned mcp==1.26.0 interpreter")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from scripts.jianying_local.mcp_server import worker_env

    with tempfile.TemporaryDirectory(prefix="jianying-public-readonly-") as temp:
        root = Path(temp)
        native = root / "synthetic_catalog"
        folder = native / "synthetic"
        folder.mkdir(parents=True)
        doc = {"version": 360000, "id": "synthetic-draft", "name": "synthetic", "duration": 1_000_000,
               "fps": 30, "canvas_config": {"width": 1920, "height": 1080},
               "materials": {"effects": [{"id": "synthetic-effect", "type": "fixture"}]},
               "tracks": [{"id": "synthetic-track", "type": "effect", "segments": [{
                   "id": "synthetic-segment", "material_id": "synthetic-effect", "extra_material_refs": [],
                   "target_timerange": {"start": 0, "duration": 1_000_000}}]}]}
        meta = {"draft_name": "synthetic", "draft_id": doc["id"], "draft_fold_path": str(folder),
                "draft_root_path": str(native), "tm_duration": doc["duration"]}
        for path, value in [(folder / "draft_info.json", doc), (folder / "draft_meta_info.json", meta),
                            (native / "root_meta_info.json", {"all_draft_store": [meta], "draft_ids": 1, "root_path": str(native)})]:
            write_new(path, canonical(value))
        config = root / "startup.json"
        write_new(config, canonical({"drafts_root": str(native), "work_root": str(root / "work"),
                  "skill_root": str(root / "missing-backend"), "app_root": str(root / "missing-editor"),
                  "codec_root": str(root / "missing-codec"), "codex_config": str(root / "unused-config.toml"),
                  "worker_python": sys.executable}))
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        params = StdioServerParameters(command=sys.executable,
                 args=["-I", "-B", "-X", "utf8", str(PROJECT / "scripts/jianying_local/mcp_server.py"), "--config", str(config)],
                 cwd=str(PROJECT), env=worker_env())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                handshake = await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
                require(set(names) == OPERATIONS, "tool_surface", "Exactly six tools must be exposed")
                async def call(name, args):
                    reply = await session.call_tool(name, arguments=args)
                    data = reply.structuredContent or json.loads(reply.content[0].text)
                    require(not reply.isError and data.get("ok"), "readonly_call_failed", str(data.get("error")))
                    return data["result"]
                diagnosis = await call("doctor", {})
                require(diagnosis["native_acceptance"] == "pending", "fresh_install_gate", "The public copy must start unaccepted")
                catalog = await call("list_drafts", {"limit": 1})
                inspected = await call("inspect_draft", {"name": "synthetic", "limit": 1})
                verified = await call("verify_draft", {"name": "synthetic"})
                require(catalog["total"] == 1 and inspected["stats"]["segments"] == 1 and
                        verified["file_validation"] == "passed", "readonly_fixture", "Synthetic readback mismatch")
        after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        require(before == after, "readonly_mutation", "Readonly smoke must not write config, plans, media or drafts")
        return {"status": "passed", "sdk": "1.26.0", "protocol": handshake.protocolVersion,
                "tools": names, "readonly_calls": ["doctor", "list_drafts", "inspect_draft", "verify_draft"],
                "fixture_files_unchanged": True, "fresh_install_acceptance": "pending",
                "native_editor_test": "not_performed", "media_downloads": False, "registration": "not_performed"}


if __name__ == "__main__":
    sys.stdout.buffer.write(canonical(asyncio.run(smoke())) + b"\n")
