"""Actual SDK handshake/read/write canary, only after the human native gate."""
from __future__ import annotations

import asyncio
import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

sys.dont_write_bytecode=True
PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))
from scripts.jianying_local import core
from scripts.jianying_local.runtime import Settings, canonical, decode, load_settings, require, require_closed, write_new


async def smoke(settings):
    core.require_acceptance(settings)
    require_closed()
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from scripts.jianying_local.mcp_server import worker_env
    from scripts.jianying_local.setup_mcp import service_entry
    entry = service_entry(settings)
    parameters=StdioServerParameters(command=entry["command"],args=entry["args"],env=worker_env(),cwd=entry["cwd"])
    async with stdio_client(parameters) as (read,write):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=960)) as session:
            handshake=await session.initialize()
            listing=await session.list_tools()
            names=[t.name for t in listing.tools]
            require(set(names)==core.OPERATIONS,"tool_surface_mismatch","Only the six documented tools may be exposed")
            async def call(name,args):
                output=await session.call_tool(name,arguments=args)
                require(not output.isError,"mcp_tool_failed",f"SDK tool failure: {name}")
                data=output.structuredContent
                if not data:data=json.loads(output.content[0].text)
                require(data.get("ok"),"mcp_tool_failed",str(data.get("error")))
                return data
            diagnosis=await call("doctor",{})
            require(diagnosis["result"]["native_acceptance"]=="accepted","native_acceptance_required","Worker did not validate native gate")
            pid=diagnosis["transport"]["server_pid"]
            ram_cmd=f"Get-Process -Id {int(pid)} | Select-Object Id,WorkingSet64,PrivateMemorySize64,PeakWorkingSet64 | ConvertTo-Json -Compress"
            ram=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",ram_cmd],capture_output=True,timeout=15,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            require(ram.returncode==0,"memory_measure_failed","Cannot read this MCP process memory")
            memory=decode(ram.stdout)
            listing_result=await call("list_drafts",{"limit":20})
            gate=core.accepted(settings)
            source=next(d["name"] for d in gate["saved_drafts"] if d["feature"]=="music_and_transition")
            inspected=await call("inspect_draft",{"name":source,"limit":5})
            req={"request_id":str(uuid.uuid4()),"mode":"enrich","source_draft":source,
                 "name":"剪映MCP_副本写入试验_"+time.strftime("%Y%m%d_%H%M%S"),
                 "bgm":[{"path":str(settings.work_root/"test_assets"/"仅验收用_非发布BGM.wav"),"start_us":0,"duration_us":1_000_000,"volume":.1,
                         "fade_in_us":100000,"fade_out_us":100000}]}
            planned=await call("plan_draft",{"request":req})
            preview=planned["result"]
            applied=await call("apply_plan",{"plan_id":preview["plan_id"],"expected_plan_sha256":preview["plan_sha256"]})
            verify=await call("verify_draft",{"name":req["name"],"plan_id":preview["plan_id"]})
            result={"status":"passed","sdk":"1.26.0","code_profile":core.profile(settings),"app_version":core.app_version(settings),
                    "protocol_version":handshake.protocolVersion,"server":handshake.serverInfo.model_dump(),"tools":names,
                    "read_only_doctor":"passed","read_only_list_and_latest_inspect":"passed","native_test_write":"passed","file_boundary":verify["result"],
                    "test_draft":applied["result"]["draft_path"],"editor_acceptance":"pending_for_mcp_specific_copy",
                    "mcp_idle_process_memory_bytes":memory,
                    "worker_peak_memory_bytes":[v.get("transport",{}) for v in [diagnosis,listing_result,inspected,planned,applied,verify]],
                    "memory_boundary":"Actual idle adapter and sampled per-call worker peaks; native codec separately measured in snapshot receipts",
                    "measured_us":time.time_ns()//1000}
    write_new(settings.work_root/"mcp_smoke.json",canonical(result))
    return result


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    sys.stdout.buffer.write(canonical(asyncio.run(smoke(load_settings(parser.parse_args().config))))+b"\n")
