"""Explicit, gated SDK installation and additive global registration/rollback.

Does nothing on import. Never restores an old whole config over newer settings.
"""
from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import tomllib
import venv
from pathlib import Path

sys.dont_write_bytecode=True
PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))
from scripts.jianying_local import core
from scripts.jianying_local.runtime import CONFIG_KEYS, Settings, atomic_write, canonical, decode, digest, load_settings, require, safe_path, write_new, writer_lock

SERVER_NAME="jianying-local"
ENV=PROJECT/".venv-jianying-mcp"
SERVER=Path(__file__).with_name("mcp_server.py")
CONFIG=Settings().codex_config
BEGIN=b"# BEGIN jianying-local managed entry\n"
END=b"# END jianying-local managed entry\n"


def pipeline_identity(settings=None):
    settings = settings or Settings()
    site=settings.worker_python.parent.parent/"Lib"/"site-packages"
    packages=sorted((d.metadata.get("Name"),d.version) for d in importlib.metadata.Distribution.discover(path=[str(site)]))
    locks={str(p.relative_to(PROJECT)):digest(p.read_bytes()) for pattern in ("*requirements*.txt","uv.lock","pyproject.toml",".venv/pyvenv.cfg") for p in PROJECT.glob(pattern) if p.is_file()}
    return {"distributions":packages,"locks":locks,"backend_profile":core.profile(settings)}


def run(command):
    result=subprocess.run(command,capture_output=True,timeout=600,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    require(result.returncode==0,"setup_failed",result.stderr.decode("utf-8",errors="replace")[-3000:] or "Setup command failed")
    return result.stdout


def install(settings):
    core.require_acceptance(settings)
    before=pipeline_identity(settings)
    settings.work_root.mkdir(parents=True, exist_ok=True)
    log=settings.work_root/"mcp_install.json"
    if log.exists():
        receipt=decode(log.read_bytes())
        if receipt.get("status")=="installed":return receipt
        require(False,"previous_install_failure","Inspect the separate environment and failed receipt; no destructive reinstall")
    require(not ENV.exists(),"target_exists","Separate MCP environment already exists; no replacement")
    safe_path(ENV)
    receipt={"status":"installing","sdk":"mcp==1.26.0","environment":str(ENV),"started_us":time.time_ns()//1000}
    write_new(log,canonical(receipt))
    try:
        venv.EnvBuilder(with_pip=True).create(ENV)
        python=ENV/"Scripts"/"python.exe"
        run([str(python),"-I","-B","-m","pip","install","--no-cache-dir","--disable-pip-version-check","--only-binary=:all:","mcp==1.26.0"])
        actual=run([str(python),"-I","-B","-c","from importlib.metadata import version; print(version('mcp'))"]).decode().strip()
        require(actual=="1.26.0","sdk_mismatch","Unexpected SDK version")
        require(pipeline_identity(settings)==before,"pipeline_changed","Existing video environment/locks/backend changed during installation")
        frozen=run([str(python),"-I","-B","-m","pip","freeze","--all"])
        write_new(settings.work_root/"mcp_dependencies.lock.txt",frozen)
        receipt.update(status="installed",video_pipeline_unchanged=True,bytes=sum(p.stat().st_size for p in ENV.rglob("*") if p.is_file()),installed_us=time.time_ns()//1000)
        atomic_write(log,canonical(receipt))
        return receipt
    except Exception as exc:
        receipt.update(status="failed",error=str(exc),recovery="Retain this separate environment; no video environment/config/draft deletion")
        atomic_write(log,canonical(receipt));raise


def service_entry(settings=None):
    settings = settings or Settings()
    args = ["-I","-B","-X","utf8",str(SERVER)]
    if settings.config_path:
        args += ["--config", str(settings.config_path)]
    for key in sorted(CONFIG_KEYS):
        args += ["--" + key.replace("_", "-"), str(getattr(settings, key))]
    return {"command":str(ENV/"Scripts"/"python.exe"),"args":args,
            "cwd":str(PROJECT),"startup_timeout_sec":20,"tool_timeout_sec":960,"enabled":True,
            "enabled_tools":sorted(core.OPERATIONS),"default_tools_approval_mode":"writes"}


def without_service(doc):
    result=copy.deepcopy(doc)
    result.setdefault("mcp_servers",{}).pop(SERVER_NAME,None)
    return result


def prepare_config(before, entry):
    parsed=tomllib.loads(before.decode("utf-8-sig"))
    require(SERVER_NAME not in parsed.get("mcp_servers",{}),"config_conflict","Service already exists; no replacement")
    body=f"[mcp_servers.{SERVER_NAME}]\n"+"\n".join(f"{k} = {json.dumps(v,ensure_ascii=True)}" for k,v in entry.items())+"\n"
    block=BEGIN+body.encode("utf-8")+END
    output=before+(b"" if before.endswith(b"\n") else b"\n")+b"\n"+block
    after=tomllib.loads(output.decode("utf-8-sig"))
    require(without_service(parsed)==without_service(after) and after["mcp_servers"][SERVER_NAME]==entry,
            "config_boundary_violation","Another config setting or service changed")
    return output,block


def remove_block(current,block,entry):
    require(current.count(block)==1,"config_changed","Managed block changed or is missing; do not delete an ambiguous service")
    before=tomllib.loads(current.decode("utf-8-sig"))
    require(before.get("mcp_servers",{}).get(SERVER_NAME)==entry,"config_changed","Service changed after registration")
    result=current.replace(block,b"",1)
    after=tomllib.loads(result.decode("utf-8-sig"))
    require(SERVER_NAME not in after.get("mcp_servers",{}) and without_service(before)==without_service(after),
            "config_boundary_violation","Rollback would remove another setting")
    return result


def register(settings):
    core.require_acceptance(settings)
    require((ENV/"Scripts"/"python.exe").is_file(),"missing_runtime","Install only after native acceptance")
    smoke=decode((settings.work_root/"mcp_smoke.json").read_bytes())
    require(smoke.get("status")=="passed" and smoke.get("code_profile")==core.profile(settings) and
            smoke.get("app_version")==core.app_version(settings) and smoke.get("native_test_write")=="passed" and
            set(smoke.get("tools",[]))==core.OPERATIONS,"mcp_unverified","Real SDK handshake, discovery, read and copy-write smoke must pass before registration")
    with writer_lock(settings):
        config = safe_path(settings.codex_config,exists=True)
        raw=config.read_bytes();entry=service_entry(settings)
        receipt_path=settings.work_root/"mcp_registration.json"
        if receipt_path.exists():
            previous=decode(receipt_path.read_bytes())
            if previous.get("status")=="registered" and tomllib.loads(raw.decode("utf-8-sig")).get("mcp_servers",{}).get(SERVER_NAME)==entry:
                return {**previous,"idempotent_replay":True}
            require(False,"previous_registration","Inspect previous registration/rollback receipt; no automatic replacement")
        after,block=prepare_config(raw,entry)
        backup=settings.work_root/("codex_config_before_"+digest(raw)+".toml")
        if not backup.exists():write_new(backup,raw)
        else:require(backup.read_bytes()==raw,"backup_conflict","Config backup content mismatch")
        receipt={"status":"prepared","service":SERVER_NAME,"entry":entry,"backup":str(backup),"before_sha256":digest(raw),"after_sha256":digest(after),"block":block.decode("utf-8")}
        write_new(receipt_path,canonical(receipt))
        require(config.read_bytes()==raw,"config_changed","Config changed after preview; preserve backup and stop")
        atomic_write(config,after)
        require(config.read_bytes()==after,"config_race","Config changed during registration; no forced full restore")
        receipt.update(status="registered",registered_us=time.time_ns()//1000,other_config_unchanged=True)
        atomic_write(receipt_path,canonical(receipt))
        return {k:v for k,v in receipt.items() if k not in {"block","entry"}}


def rollback(settings):
    with writer_lock(settings):
        receipt_path=settings.work_root/"mcp_registration.json"
        receipt=decode(receipt_path.read_bytes())
        if receipt.get("status")=="rolled_back":return {"status":"rolled_back","idempotent_replay":True}
        require(receipt.get("status") in {"registered","prepared"},"invalid_receipt","No owned registered entry to remove")
        config = safe_path(settings.codex_config,exists=True)
        raw=config.read_bytes()
        output=remove_block(raw,receipt["block"].encode("utf-8"),receipt["entry"])
        backup=settings.work_root/("codex_config_before_rollback_"+digest(raw)+".toml")
        if not backup.exists():write_new(backup,raw)
        require(config.read_bytes()==raw,"config_changed","Config changed during rollback; stop")
        atomic_write(config,output)
        require(config.read_bytes()==output,"config_race","Config changed during rollback; do not force restore")
        receipt.update(status="rolled_back",rollback_backup=str(backup),rollback_us=time.time_ns()//1000)
        atomic_write(receipt_path,canonical(receipt))
        return {"status":"rolled_back","removed_service_only":SERVER_NAME,"drafts_deleted":False,"media_deleted":False,"venv_deleted":False}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("action",choices=["install","register","rollback"])
    parser.add_argument("--config", type=Path)
    args=parser.parse_args()
    result={"install":install,"register":register,"rollback":rollback}[args.action](load_settings(args.config))
    sys.stdout.buffer.write(canonical(result)+b"\n")
