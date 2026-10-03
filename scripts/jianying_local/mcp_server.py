"""Thin stdio adapter. No scanning/editor/network actions during startup.

Install the pinned official SDK in its separate venv AFTER native acceptance.
It is intentionally not a dependency of the existing video environment.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
CLI = Path(__file__).with_name("cli.py")
from scripts.jianying_local.runtime import CONFIG_KEYS, ToolError, check_stamp, file_stamp, load_settings

STARTUP_SETTINGS = load_settings()
WORKER = STARTUP_SETTINGS.worker_python
CONFIG_STAMP = file_stamp(STARTUP_SETTINGS.config_path, hash_bytes=True) if STARTUP_SETTINGS.config_path else None
TOOLS = {"doctor", "list_drafts", "inspect_draft", "plan_draft", "apply_plan", "verify_draft"}
ENV_KEYS = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC"}


def worker_env():
    result = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEYS}
    result.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return result


async def invoke(operation: str, arguments: dict) -> dict:
    if operation not in TOOLS:
        return {"ok": False, "error": {"code": "unknown_operation", "message": "Unsupported operation"}}
    try:
        if CONFIG_STAMP is not None:
            check_stamp(CONFIG_STAMP)
        data = json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("Request exceeds 32 MiB")
        command = [str(WORKER), "-I", "-B", "-X", "utf8", str(CLI), operation]
        if STARTUP_SETTINGS.config_path:
            command += ["--config", str(STARTUP_SETTINGS.config_path)]
        for key in sorted(CONFIG_KEYS):
            command += ["--" + key.replace("_", "-"), str(getattr(STARTUP_SETTINGS, key))]
        process = await asyncio.create_subprocess_exec(*command,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=str(PROJECT), env=worker_env(), creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
        peak = {}
        async def monitor():
            from scripts.jianying_local.codec import memory_for_pid
            while process.returncode is None:
                sample = memory_for_pid(process.pid)
                for k,v in (sample or {}).items(): peak[k] = max(peak.get(k,0),v)
                await asyncio.sleep(.03)
        memory_task = asyncio.create_task(monitor())
        try:
            output, error = await asyncio.wait_for(process.communicate(data), timeout=900)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            # Publication may have reached the index; never automatically retry a write under a new ID.
            if operation == "apply_plan":
                return {"ok": False, "error": {"code": "worker_interrupted", "message": "Check this plan's persisted receipt; publication may have completed. Do not create a new request blindly."}}
            raise
        finally:
            memory_task.cancel()
            try: await memory_task
            except asyncio.CancelledError: pass
        if len(output) > 32 * 1024 * 1024:
            raise ValueError("Worker output exceeded limit")
        result = json.loads(output.decode("utf-8"))
        if not isinstance(result, dict) or type(result.get("ok")) is not bool or (process.returncode != 0 and result["ok"]):
            raise ValueError("Worker did not return a valid result envelope")
        return {**result, "transport": {"type": "stdio", "server_pid": os.getpid(), "worker_pid": process.pid,
                                      "worker_peak_bytes": peak}}
    except ToolError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.reason}}
    except (ValueError, OSError, asyncio.TimeoutError) as exc:
        return {"ok": False, "error": {"code": "worker_error", "message": str(exc)}}


def create_server():
    # Lazy import lets standard-library-only tests exercise the adapter without installing SDK.
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    server = FastMCP("jianying-local", instructions=(
        "Windows local Jianying verified saved-draft tools. Create new drafts/copies only; never overwrite originals. "
        "Run doctor, inspect, plan then apply the explicit preview hash. Keep Jianying fully exited for writes. "
        "Production writes require human-tested native compatibility. Use patch for local edits/relink/SRT with explicit companion policies. No ASR, downloading, uploads, arbitrary commands or export. "
        "Integer microseconds; explicit local music, volume and speech intervals. File validation is not editor acceptance."))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read)
    async def doctor() -> dict:
        """Read runtime/editor closure/schema/compatibility status; no footage scan."""
        return await invoke("doctor", {})

    @server.tool(annotations=read)
    async def list_drafts(limit: int = 100) -> dict:
        """Read native catalog identities and paths, maximum 1000 rows."""
        return await invoke("list_drafts", {"limit": limit})

    @server.tool(annotations=read)
    async def inspect_draft(name: str, limit: int = 100, cursor: dict | None = None, audio_analysis: list[dict] | None = None) -> dict:
        """Read a named saved draft, tracks/segment IDs/media and structure support."""
        args = {"name":name,"limit":limit}
        if cursor is not None: args["cursor"] = cursor
        if audio_analysis is not None: args["audio_analysis"] = audio_analysis
        return await invoke("inspect_draft", args)

    @server.tool(annotations=write)
    async def plan_draft(request: dict) -> dict:
        """Persist v2 preview only. create: reviewed clips; enrich: music/transitions; patch: explicit local ops/companion policies, relinks/SRT/music/cuts. All writes create separate copies."""
        return await invoke("plan_draft", request)

    @server.tool(annotations=write)
    async def apply_plan(plan_id: str, expected_plan_sha256: str) -> dict:
        """Publish only this immutable preview to a new draft/copy; never rebuild a failed/edited result."""
        return await invoke("apply_plan", {"plan_id": plan_id, "expected_plan_sha256": expected_plan_sha256})

    @server.tool(annotations=read)
    async def verify_draft(name: str, plan_id: str | None = None, validation: str = "exact") -> dict:
        """Validate files/references; optional exact planned boundary and original-file fingerprints. No claim of editor playback."""
        args = {"name": name}
        if plan_id is not None: args["plan_id"] = plan_id
        args["validation"] = validation
        return await invoke("verify_draft", args)

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Trusted local startup configuration")
    for key in sorted(CONFIG_KEYS):
        parser.add_argument("--" + key.replace("_", "-"), type=Path)
    args = parser.parse_args()
    STARTUP_SETTINGS = load_settings(args.config, **{key: getattr(args, key) for key in CONFIG_KEYS})
    WORKER = STARTUP_SETTINGS.worker_python
    CONFIG_STAMP = file_stamp(STARTUP_SETTINGS.config_path, hash_bytes=True) if STARTUP_SETTINGS.config_path else None
    create_server().run(transport="stdio")
