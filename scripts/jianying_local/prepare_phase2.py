"""Six short new test drafts (five human checks + one synthetic relink source)."""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode=True
PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))
from scripts.jianying_local import core, codec, formats, setup_mcp
from scripts.jianying_local.runtime import (Settings, canonical, check_stamp, decode, file_stamp, load_settings, require, require_closed, safe_path, write_new)


def backup_docs(settings):
    from scripts.jianying_local.prepare_canaries import DOCS
    files=list(DOCS)
    root=settings.work_root/"documentation_backup_phase2"
    require(not root.exists(),"target_exists","Phase2 documentation backup already exists")
    root.mkdir()
    rows=[]
    for i,p in enumerate(files):
        stamp=file_stamp(p,hash_bytes=True)
        dest=root/(str(i)+"_"+p.name)
        write_new(dest,p.read_bytes())
        rows.append({"original":str(p),"backup":str(dest),"sha256":stamp["sha256"]})
    write_new(root/"manifest.json",canonical({"files":rows}))
    return {"backup":str(root),"files":len(rows)}


def prepare(settings, run_label=None):
    require(settings.canary,"invalid_input","Only explicit native tests")
    require_closed()
    if run_label is not None:
        core.leaf(run_label)
    marker = "_"+run_label if run_label else ""
    manifest_path=settings.work_root/("canaries_phase2"+marker+".json")
    if manifest_path.exists():
        return {"manifest":str(manifest_path),"idempotent_replay":True}
    started_path=settings.work_root/("phase2_started"+marker+".json")
    require(not started_path.exists(),"incomplete_test_run","Review partial phase2 receipts; do not create duplicate tests")
    old=decode((settings.work_root/"canaries.json").read_bytes())
    a=core.read_source(settings,old["drafts"][0]["name"])
    b=core.read_source(settings,old["drafts"][1]["name"])
    before_catalog=core.load_catalog(settings)[2]
    protected=[]
    for source in (a,b):
        for p in source["folder"].rglob("*"):
            safe_path(p)
            if p.is_file() and p.stat().st_size <= 32*1024*1024:
                protected.append(file_stamp(p,hash_bytes=True))
    config_stamp=file_stamp(settings.codex_config,hash_bytes=True)
    code_profile=core.profile(settings)
    profile=codec.identity(settings)
    assets=safe_path(settings.work_root/("test_assets_phase2"+marker))
    require(not assets.exists(),"target_exists","Retain existing test assets; no overwrite")
    assets.mkdir()
    write_new(started_path,canonical({"profile":code_profile,"started_us":time.time_ns()//1000}))
    write_new(assets/"catalog_before.json",canonical(before_catalog))
    suffix=time.strftime("%Y%m%d_%H%M%S")
    drafts=[]
    tone=str(settings.work_root/"test_assets"/"仅验收用_非发布BGM.wav")
    material_map=core.resource_map(a["doc"])
    tracks=a["doc"]["tracks"]
    track=next(t for t in tracks if t["type"]=="video")
    original_clips=track["segments"]
    media=[material_map[s["material_id"]][1]["path"] for s in original_clips]
    def publish(request, feature, helper=False):
        write_new(assets/(feature+"_request.json"),canonical(request))
        p=core.plan_draft(settings,request)
        r=core.apply_plan(settings,p["plan_id"],p["plan_sha256"])
        v=core.verify_draft(settings,request["name"],p["plan_id"])
        drafts.append({"feature":feature,"name":request["name"],"path":r["draft_path"],"plan_id":p["plan_id"],
                       "plan_sha256":p["plan_sha256"],"duration_us":r["stats"]["duration_us"],"helper_only":helper,"file_verification":v})
        return core.read_source(settings,request["name"])
    first=publish({"request_id":str(uuid.uuid4()),"mode":"enrich","source_draft":old["drafts"][0]["name"],
                   "name":"剪映二期_01格式与最新保存_"+suffix,
                   "bgm":[{"path":tone,"start_us":4_000_000,"duration_us":1_000_000,"volume":.2}]},"format")
    companion={t["id"]:"follow" for t in first["doc"]["tracks"] if t["type"]!="video"}
    main=next(t for t in first["doc"]["tracks"] if t["type"]=="video")
    ids=[s["id"] for s in main["segments"]]
    timeline=assets/"最终时间轴_仅验收字幕.srt"
    raw=assets/"完整原片_仅验收字幕.srt"
    write_new(timeline,"1\n00:00:00,500 --> 00:00:01,500\n底部字幕：时间轴导入测试\n".encode("utf-8"))
    write_new(raw,("1\n00:00:02,500 --> 00:00:03,500\n原片字幕：重复使用映射\n\n"
                   "2\n00:00:00,500 --> 00:00:01,500\n跨边界整句：文字保留，显示裁时\n").encode("utf-8"))
    ops=[{"type":"trim_clip","track_id":main["id"],"segment_id":ids[0],"in_us":500000,"out_us":4500000,"companion_tracks":companion},
         {"type":"insert_clip","track_id":main["id"],"anchor_segment_id":ids[0],"position":"after","clip":{"path":media[1],"in_us":2000000,"out_us":4000000},"companion_tracks":companion},
         {"type":"move_clip","track_id":main["id"],"segment_id":ids[2],"anchor_segment_id":ids[1],"position":"before","companion_tracks":companion},
         {"type":"trim_clip","track_id":main["id"],"segment_id":ids[1],"in_us":1000000,"out_us":3500000,"companion_tracks":companion}]
    local=publish({"request_id":str(uuid.uuid4()),"mode":"patch","source_draft":first["doc"]["name"],
                   "name":"剪映二期_02局部修改与两类字幕_"+suffix,"operations":ops,
                   "subtitles":[{"path":str(timeline),"basis":"timeline"},{"path":str(raw),"basis":"source","media_path":media[1]}]},"local_and_captions")
    local_main=next(t for t in local["doc"]["tracks"] if t["type"]=="video")
    left,right=local_main["segments"][:2]
    # Left move is first candidate but absent locally; do not download it. Short
    # flash is already cached. Only human playback promotes it to production.
    flash="6724845376098013708"
    effect=publish({"request_id":str(uuid.uuid4()),"mode":"patch","source_draft":local["doc"]["name"],
                    "name":"剪映二期_03循环配乐与闪白_"+suffix,
                    "bgm":[{"path":tone,"start_us":0,"duration_us":8500000,"volume":.25,"loop":True,"loop_crossfade":True,
                            "fade_in_us":400000,"ducking":[{"start_us":7500000,"end_us":8000000,"volume":.05,"attack_us":200000,"release_us":200000}]},
                           {"path":tone,"start_us":8250000,"duration_us":3250000,"volume":.25,"crossfade_previous_us":250000,"fade_out_us":400000}],
                    "transitions":[{"after_segment_id":left["id"],"before_segment_id":right["id"],"resource_id":flash,"duration_us":300000}],
                    "audio_analysis":[{"path":tone,"in_us":0,"duration_us":8000000}]},"music_and_transition")
    old_media=safe_path(assets/"before_move"/"synthetic_reference.mp4")
    old_media.parent.mkdir()
    new_media=safe_path(assets/"after_move"/"synthetic_reference.mp4")
    new_media.parent.mkdir()
    run=subprocess.run([shutil.which("ffmpeg"),"-nostdin","-v","error","-f","lavfi","-i","testsrc2=s=256x144:r=30",
                        "-t","6","-c:v","libx264","-preset","ultrafast","-crf","30","-pix_fmt","yuv420p",str(old_media)],
                        capture_output=True,timeout=30,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    require(run.returncode==0,"fixture_failed","Synthetic relink fixture generation failed")
    seed=publish({"request_id":str(uuid.uuid4()),"mode":"create","name":"剪映二期_04搬迁前_仅测试源_"+suffix,
                  "canvas":{"width":1920,"height":1080,"fps":30},"clips":[{"path":str(old_media),"in_us":1000000,"out_us":4000000}]},"relink_seed",True)
    # Move only the exact newly generated fixture between validated owned dirs.
    require(old_media.is_relative_to(assets) and new_media.is_relative_to(assets) and not new_media.exists(),"invalid_path","Unsafe synthetic move")
    old_media.rename(new_media)
    relinked=publish({"request_id":str(uuid.uuid4()),"mode":"patch","source_draft":seed["doc"]["name"],
                     "name":"剪映二期_04素材搬迁后重链接_"+suffix,
                     "relinks":{"files":[{"old_path":str(old_media),"new_path":str(new_media)}]}},"relink")
    portrait=publish({"request_id":str(uuid.uuid4()),"mode":"create","name":"剪映二期_05竖屏底部字幕_"+suffix,
                      "canvas":{"width":1080,"height":1920,"fps":30},"clips":[{"path":media[0],"in_us":1000000,"out_us":4000000}],
                      "subtitles":[{"path":str(timeline),"basis":"timeline"}]},"portrait")
    for stamp in protected:check_stamp(stamp)
    check_stamp(config_stamp)
    current=core.load_catalog(settings)[2]
    require(current["all_draft_store"][len(drafts):]==before_catalog["all_draft_store"],"catalog_boundary_violation","Original catalog entries changed")
    # Native volume UI accepts dB with one decimal. Preserve its float32 gain,
    # not an impossible exact decimal 70% assertion.
    import struct
    gain=struct.unpack("f",struct.pack("f",10**(-3.1/20)))[0]
    manual={"draft":first["doc"]["name"],"segment_id":main["segments"][0]["id"],"volume":gain,"volume_db":-3.1,
            "instruction":"Only in test 01 set first video's audio volume to -3.1dB, save/exit/reopen. Other clips stay untouched."}
    manifest={"schema":"jianying-local-canaries/2","code_profile":code_profile,"format_identity":profile,"app_version":core.app_version(settings),
              "drafts":drafts,"protected_files":protected,"protected_media":list({v["stamp"]["path"]:v for s in (a,b) for v in s["media"].values()}.values()),
              "config_stamp":config_stamp,"catalog_before_path":str(assets/"catalog_before.json"),"manual_change":manual,
              "transition_resource_id":flash,"caption_presets":["landscape","portrait"],"status":"manual_acceptance_pending",
              "media_copies":0,"synthetic_fixture_moved_only":str(new_media),"generated_test_video_bytes":new_media.stat().st_size,
              "test_voice_identity":"technical text/windows only, not transcribed human speech","created_us":time.time_ns()//1000}
    write_new(manifest_path,canonical(manifest))
    from scripts.jianying_local.acceptance_phase2 import CHECKS
    write_new(settings.work_root/("human_report_phase2_template"+marker+".json"),canonical({
        "user_confirmation":"请替换为用户实际验收回复，不能由工具编造通过",
        "manifest_path":str(manifest_path),
        "checks":{k:False for k in sorted(CHECKS)}}))
    lines=["# 剪映二期人工验收（尚未通过）","", "请在剪映手动操作；无需把鼠标交给工具。只打开下面5个主测试，不修改任何原工程。",
           "每个主测试都播放、保存、退出后重开确认。完成后彻底退出剪映；有问题请指出编号，不要笼统勾选通过。", ""]
    for d in drafts:
        if not d["helper_only"]:lines += ["- `"+d["name"]+"`（"+str(d["duration_us"]/1000000)+"秒）"]
    lines += ["", "## 检查内容", "",
        "1. 01：三个完整原片引用，正常播放；只将第一个视频片段原声音量设为−3.1dB，保存、重开保留。后台读回实际线性值0.6998419761657715，不冒充精确70%，不读旧明文。",
        "2. 02：四段顺序为原第1段→新增重复第2原片→原第3段→原第2段，总11.5秒；前后延长确认完整原片可用后撤销。旧提示音跟随到10–11秒。",
        "3. 02字幕：底部中文。成片句0.5–1.5秒；重复原片句4.5–5.5及10.5–11.5秒；跨界整句仅显示9–9.5秒，文字不能被删。检查可继续编辑。",
        "4. 03：保留02所有内容。帧对齐后循环接缝7.766666秒、避让7.5–8秒、多段配乐交叉淡化8.266666–8.5秒、末尾淡出。4秒切点闪白请求300ms、有效266666µs，播放、保存重开可见。预览有效时间为准；这里只验收技术，不是发布BGM／真实口播。",
        "5. 04重链接：3秒合成图案正常播放，无素材丢失。辅助源“04搬迁前_仅测试源”故意保留旧失效地址，不需要打开；仅该新合成文件被搬迁，用户原片未移动。",
        "6. 05：3秒竖屏，0.5–1.5秒中文底部字幕，大小／描边／安全区合适，可编辑、保存重开。原横屏素材竖屏适配仅为字幕试验，不是正式剪辑。",
        "", "请回复01–05分别是否通过，并确认01音量已设−3.1dB、全部已保存重开且已退出。明确委托时agent可检查独立测试稿，试听与界面观察分开。未通过项保持关闭；全部新验收完成前不安装SDK或注册MCP。", ""]
    write_new(settings.work_root/("acceptance_checklist_phase2"+marker+".md"),"\n".join(lines).encode("utf-8"))
    return {"manifest":str(manifest_path),"drafts":[{k:v for k,v in d.items() if k!="file_verification"} for d in drafts],
            "originals_unchanged":True,"old_catalog_rows_preserved":len(before_catalog["all_draft_store"]),"manual_change":manual,"status":manifest["status"]}

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--backup-docs",action="store_true");parser.add_argument("--run-label")
    parser.add_argument("--config", type=Path)
    args=parser.parse_args();settings=load_settings(args.config, canary=True)
    result=backup_docs(settings) if args.backup_docs else prepare(settings,args.run_label)
    sys.stdout.buffer.write(canonical(result)+b"\n")
