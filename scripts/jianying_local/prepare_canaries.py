"""Explicit one-time native tests; no production batch and no editor control."""
from __future__ import annotations

import argparse
import math
import struct
import sys
import time
import uuid
import wave
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.jianying_local import core
from scripts.jianying_local.runtime import (Settings, canonical, check_stamp, decode,
                                           digest, file_stamp, require, require_closed,
                                           safe_path, write_new, load_settings)

DOCS = [Path(__file__).resolve().parents[2] / "skills" / "jianying-local" / "SKILL.md",
        Path(__file__).resolve().parents[2] / "docs" / "native-acceptance.md"]


def backup_docs(settings):
    folder = settings.work_root / "documentation_backup"
    if (folder / "manifest.json").exists():
        return decode((folder / "manifest.json").read_bytes())
    folder.mkdir(parents=True, exist_ok=False)
    rows=[]
    for index,path in enumerate(DOCS):
        source = file_stamp(path,hash_bytes=True)
        name=f"{index}_{path.name}"
        write_new(folder/name,path.read_bytes())
        rows.append({"original":str(path),"backup":str(folder/name),"sha256":source["sha256"]})
    result={"files":rows,"created_us":time.time_ns()//1000}
    write_new(folder/"manifest.json",canonical(result))
    return result


def prepare(settings, source_name):
    require(settings.canary, "invalid_input", "Use the explicit canary settings")
    require_closed()
    manifest_path=settings.work_root/"canaries.json"
    if manifest_path.exists():
        return {**decode(manifest_path.read_bytes()),"idempotent_replay":True}
    before_catalog=core.load_catalog(settings)[2]
    source=core.read_source(settings,source_name)
    protected=[]
    # Protect small current draft metadata, not whole media/disk. All old root rows remain unchanged.
    for row in before_catalog["all_draft_store"]:
        value=row.get("draft_fold_path")
        if not value:continue
        folder=safe_path(value)
        if folder.parent!=settings.drafts_root or not folder.is_dir():continue
        for path in folder.iterdir():
            if path.is_file() and path.name in core.CONTENT_NAMES|core.AUX_NAMES:
                protected.append(file_stamp(path,hash_bytes=True))
    config=settings.codex_config
    config_stamp=file_stamp(config,hash_bytes=True)
    settings.work_root.mkdir(parents=True,exist_ok=True)
    assets=settings.work_root/"test_assets"
    assets.mkdir(exist_ok=True)
    tone=assets/"仅验收用_非发布BGM.wav"
    require(not tone.exists(),"target_exists","Test tone exists without a completed manifest; retain evidence instead of overwriting")
    # 8 seconds, mono 16 kHz PCM: 256 KiB, no external package or music license.
    with tone.open("xb") as file:
        with wave.open(file,"wb") as wav:
            wav.setparams((1,2,16000,0,"NONE","not compressed"))
            wav.writeframes(b"".join(struct.pack("<h",round(12000*(math.sin(2*math.pi*440*i/16000)+0.3*math.sin(2*math.pi*660*i/16000))/1.3)) for i in range(16000*8)))
    video_track=next(t for t in source["doc"]["tracks"] if t["type"]=="video")
    materials=core.resource_map(source["doc"])
    clips=[]
    for seg in video_track["segments"]:
        mat=materials[seg["material_id"]][1]
        if mat["duration"]>=5_000_000:
            clips.append({"path":mat["path"],"in_us":1_000_000,"out_us":4_000_000})
        if len(clips)==3:break
    require(len(clips)==3,"insufficient_media","Need three complete originals for the short new-draft test")
    suffix=time.strftime("%Y%m%d_%H%M%S")
    create={"request_id":str(uuid.uuid4()),"mode":"create","name":f"剪映通用_A原片引用试验_{suffix}",
            "canvas":{"width":1920,"height":1080,"fps":30},"clips":clips}
    transitions=[];cut_us=None
    for left,right in zip(video_track["segments"],video_track["segments"][1:]):
        lin,llen=core.time_range(left["source_timerange"],"left")
        rin,_=core.time_range(right["source_timerange"],"right")
        start,length=core.time_range(left["target_timerange"],"left")
        rstart,rlength=core.time_range(right["target_timerange"],"right")
        mat=materials[left["material_id"]][1]
        if (start+length==rstart and mat["duration"]-lin-llen>=200000 and rin>=200000 and
                min(length,rlength)>=400000 and 3_000_000<=rstart<=source["doc"]["duration"]-5_000_000):
            transitions=[{"after_segment_id":left["id"],"before_segment_id":right["id"],"resource_id":"6724845717472416269","duration_us":400000}]
            cut_us=rstart
            break
    require(transitions,"insufficient_handles","No test cut with safe original handles; no automatic retiming")
    bgm_start=cut_us-3_000_000
    enrich={"request_id":str(uuid.uuid4()),"mode":"enrich","source_draft":source_name,"name":f"剪映通用_B增补副本试验_{suffix}",
            "bgm":[{"path":str(tone),"start_us":bgm_start,"duration_us":8_000_000,"volume":0.35,
                    "fade_in_us":600000,"fade_out_us":800000,
                    "ducking":[{"start_us":cut_us-1_000_000,"end_us":cut_us+1_000_000,"volume":0.08,"attack_us":300000,"release_us":500000}]}],
            "transitions":transitions}
    (settings.work_root/"test_requests").mkdir(exist_ok=True)
    write_new(settings.work_root/"test_requests"/"A_create.json",canonical(create))
    write_new(settings.work_root/"test_requests"/"B_enrich.json",canonical(enrich))
    output=[]
    for req in (create,enrich):
        preview=core.plan_draft(settings,req)
        receipt=core.apply_plan(settings,preview["plan_id"],preview["plan_sha256"])
        verified=core.verify_draft(settings,req["name"],preview["plan_id"])
        output.append({"plan_id":preview["plan_id"],"plan_sha256":preview["plan_sha256"],"name":req["name"],
                       "path":receipt["draft_path"],"duration_us":receipt["stats"]["duration_us"],"verification":verified})
    for stamp in protected:check_stamp(stamp)
    check_stamp(config_stamp)
    after_catalog=core.load_catalog(settings)[2]
    require(after_catalog["all_draft_store"][2:]==before_catalog["all_draft_store"],"catalog_boundary_violation","Old rows changed during tests")
    result={"schema":"jianying-local-canaries/1","code_profile":core.profile(settings),"app_version":core.app_version(settings),
            "source_draft":source_name,"drafts":output,"protected_files":protected,"config_stamp":config_stamp,
            "manual_acceptance":"pending","music_purpose":"technical tone only, not artistic/publishable BGM",
            "test_cut_us":cut_us,"test_bgm_range_us":[bgm_start,bgm_start+8_000_000],
            "test_duck_range_us":[cut_us-1_000_000,cut_us+1_000_000],"transition_resource_id":"6724845717472416269",
            "originals_unchanged":True,"old_catalog_entries_unchanged":True,"config_unchanged":True,
            "finite_sampling_not_full_media_hash":True}
    write_new(manifest_path,canonical(result))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-docs",action="store_true")
    parser.add_argument("--source-draft")
    parser.add_argument("--config", type=Path)
    args=parser.parse_args()
    settings=load_settings(args.config, canary=True)
    if args.backup_docs:result=backup_docs(settings)
    else:
        require(args.source_draft is not None,"invalid_input","Explicit source draft required")
        result=prepare(settings,args.source_draft)
    summary=result if args.backup_docs else {k:v for k,v in result.items() if k not in {"protected_files","config_stamp"}}
    sys.stdout.buffer.write(canonical(summary)+b"\n")


if __name__=="__main__":main()
