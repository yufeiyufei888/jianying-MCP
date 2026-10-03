"""Real low-level vendor + FFprobe smoke test with tiny generated fixtures.

Synthetic media is made only in TemporaryDirectory, not from user footage.
"""
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import patch

from scripts.jianying_local import backend, core, music, subtitles
from scripts.jianying_local.runtime import Settings, canonical


class RealBackendTests(unittest.TestCase):
    @unittest.skipUnless((Settings().vendor / "script_file.py").is_file(), "External backend unavailable")
    def test_bottom_caption_size_is_canvas_specific_and_other_style_preserved(self):
        if not (Path(os.environ.get("WINDIR", os.environ.get("SYSTEMROOT", ""))) / "Fonts" / "msyh.ttc").exists():
            self.skipTest("Pinned local Chinese font is unavailable")
        for width,height,expected_size in ((1920,1080,5),(1080,1920,9)):
            with self.subTest(canvas=(width,height)):
                doc={"canvas_config":{"width":width,"height":height},"fps":30,"tracks":[],"materials":{}}
                cue={"text":"底部中文字幕","start_us":500000,"end_us":1500000,"track_name":"口播"}
                subtitles.add(doc,[cue],Settings(canary=True))
                material=doc["materials"]["texts"][0]
                style=json.loads(material["content"])["styles"][0]
                self.assertEqual(expected_size,style["size"])
                self.assertEqual([1,1,1],style["fill"]["content"]["solid"]["color"])
                self.assertTrue(style["strokes"])
                self.assertEqual(.82,material["line_max_width"])
                self.assertEqual(-.8,doc["tracks"][0]["segments"][0]["clip"]["transform"]["y"])
                self.assertEqual([],doc["materials"].get("speeds",[]))
                self.assertEqual([],doc["tracks"][0]["segments"][0]["extra_material_refs"])

    @unittest.skipUnless((Settings().vendor / "script_file.py").is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe"), "External backend / FFmpeg missing")
    def test_real_loop_srt_loudness_and_no_media_copy(self):
        with tempfile.TemporaryDirectory(prefix="jy-real-v2-") as task_tmp:
            root=Path(task_tmp);native=root/"native";native.mkdir()
            settings=Settings(native,root/"work",Settings().skill_root,True)
            (native/"root_meta_info.json").write_bytes(canonical({"all_draft_store":[],"draft_ids":0}))
            video=root/"original.mp4"
            result=subprocess.run([shutil.which("ffmpeg"),"-v","error","-f","lavfi","-i","color=c=blue:s=256x144:r=30",
                                   "-t","8","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",str(video)],capture_output=True)
            self.assertEqual(0,result.returncode)
            audio=root/"tone.wav"
            with wave.open(str(audio),"wb") as wav:
                wav.setparams((1,2,16000,0,"NONE","not compressed"))
                wav.writeframes(b"".join(struct.pack("<h",round(5000*math.sin(2*math.pi*220*i/16000))) for i in range(16000*3)))
            timeline=root/"timeline.srt";timeline.write_text("1\n00:00:00,500 --> 00:00:01,500\n底部字幕测试\n",encoding="utf-8")
            raw=root/"raw.srt";raw.write_text("1\n00:00:02,000 --> 00:00:03,000\n原片重复使用字幕\n",encoding="utf-8")
            req={"request_id":str(uuid.uuid4()),"mode":"create","name":"base","canvas":{"width":1920,"height":1080,"fps":30},
                 "clips":[{"path":str(video),"in_us":1_000_000,"out_us":4_000_000} for _ in range(4)]}
            with patch("scripts.jianying_local.runtime.app_pids",return_value=[]),patch.object(core,"app_version",return_value="fixture"):
                p=core.plan_draft(settings,req);core.apply_plan(settings,p["plan_id"],p["plan_sha256"])
                patch_req={"request_id":str(uuid.uuid4()),"mode":"patch","name":"result","source_draft":"base",
                           "subtitles":[{"path":str(timeline),"basis":"timeline"},{"path":str(raw),"basis":"source","media_path":str(video)}],
                           "bgm":[{"path":str(audio),"start_us":0,"duration_us":12_000_000,"volume":.3,"loop":True,"loop_crossfade":True,
                                   "fade_in_us":200000,"fade_out_us":200000,"ducking":[{"start_us":2_700_000,"end_us":3_300_000,"volume":.05,"attack_us":100000,"release_us":100000}]}]}
                p=core.plan_draft(settings,patch_req);r=core.apply_plan(settings,p["plan_id"],p["plan_sha256"])
                out=core.read_source(settings,"result")
                self.assertEqual(1,len(out["doc"]["materials"]["audios"]))
                music_segments=[s for t,s in core.segments(out["doc"]) if t["type"]=="audio"]
                self.assertEqual(5,len(music_segments))
                self.assertEqual(12_000_000,max(s["target_timerange"]["start"]+s["target_timerange"]["duration"] for s in music_segments))
                texts=out["doc"]["materials"]["texts"]
                self.assertEqual(5,len(texts))
                for t,s in core.segments(out["doc"]):
                    if t["type"]=="text":self.assertEqual(-.8,s["clip"]["transform"]["y"])
                self.assertTrue(all(Path(m["path"]).is_file() for m in out["doc"]["materials"]["audios"]))
                self.assertFalse(any(f.suffix in {".mp4",".wav"} for f in Path(r["draft_path"]).rglob("*")))
                measured=music.analyze(settings,[{"path":str(audio),"in_us":0,"duration_us":3_000_000}])[0]
                cached=music.analyze(settings,[{"path":str(audio),"in_us":0,"duration_us":3_000_000}])[0]
                self.assertIsInstance(measured["integrated_lufs"],float)
                self.assertTrue(cached["cached"])
                self.assertEqual(0,measured["audio_files_created"])

    @unittest.skipUnless((Settings().vendor / "script_file.py").is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe"), "External backend / FFmpeg missing")
    def test_real_materials_fades_keyframes_and_transition_links(self):
        with tempfile.TemporaryDirectory(prefix="jy-real-backend-") as task_tmp:
            root=Path(task_tmp); native=root/"native"; native.mkdir()
            settings=Settings(native,root/"jobs",Settings().skill_root,True)
            (native/"root_meta_info.json").write_bytes(canonical({"all_draft_store":[],"draft_ids":0,"root_path":str(native)}))
            video=root/"original.mp4"
            result=subprocess.run([shutil.which("ffmpeg"),"-v","error","-f","lavfi","-i","testsrc2=size=256x144:rate=30",
                                   "-t","8","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",str(video)],capture_output=True)
            self.assertEqual(0,result.returncode,result.stderr.decode("utf-8",errors="replace"))
            music=root/"tone.wav"
            with wave.open(str(music),"wb") as wav:
                wav.setparams((1,2,16000,0,"NONE","not compressed"))
                wav.writeframes(b"".join(struct.pack("<h",round(7000*math.sin(2*math.pi*440*i/16000))) for i in range(16000*6)))
            req={"request_id":str(uuid.uuid4()),"mode":"create","name":"native-fixture",
                 "canvas":{"width":1920,"height":1080,"fps":30},
                 "clips":[{"path":str(video),"in_us":1_000_000,"out_us":3_000_000,"crop":{"upper_left_x":0.1,"lower_left_x":0.1},"transform":{"scale_x":1.05,"scale_y":1.05}},
                          {"path":str(video),"in_us":4_000_000,"out_us":6_000_000}]}
            with patch("scripts.jianying_local.runtime.app_pids",return_value=[]),patch.object(core,"app_version",return_value="fixture"):
                plan=core.plan_draft(settings,req)
                receipt=core.apply_plan(settings,plan["plan_id"],plan["plan_sha256"])
                source=core.read_source(settings,"native-fixture")
                ids=[s["id"] for s in source["doc"]["tracks"][0]["segments"]]
                enrich={"request_id":str(uuid.uuid4()),"mode":"enrich","name":"effects-fixture","source_draft":"native-fixture",
                        "bgm":[{"path":str(music),"start_us":0,"duration_us":4_000_000,"volume":0.25,"fade_in_us":300000,"fade_out_us":400000,
                                "ducking":[{"start_us":1_000_000,"end_us":2_000_000,"volume":0.06,"attack_us":200000,"release_us":300000}]}],
                        "transitions":[{"after_segment_id":ids[0],"before_segment_id":ids[1],"duration_us":400000,"resource_id":"6724845717472416269"}]}
                plan2=core.plan_draft(settings,enrich)
                receipt2=core.apply_plan(settings,plan2["plan_id"],plan2["plan_sha256"])
                out=core.read_source(settings,"effects-fixture")["doc"]
                audio=next(t for t in out["tracks"] if t["type"]=="audio")["segments"][0]
                self.assertEqual(2,len(audio["extra_material_refs"]))
                self.assertEqual(1,len(out["materials"]["audio_fades"]))
                self.assertEqual(1,len(out["materials"]["transitions"]))
                kfs=audio["common_keyframes"][0]["keyframe_list"]
                self.assertEqual([0,800000,1000000,2000000,2300000,4000000],[k["time_offset"] for k in kfs])
                self.assertEqual([0.25,0.25,0.06,0.06,0.25,0.25],[k["values"][0] for k in kfs])
                self.assertEqual(str(video),out["materials"]["videos"][0]["path"])
                self.assertEqual(8_000_000,out["materials"]["videos"][0]["duration"])
                self.assertEqual(str(music),out["materials"]["audios"][0]["path"])
                self.assertEqual("passed",core.verify_draft(settings,"effects-fixture",plan2["plan_id"])["boundary_validation"])
                self.assertFalse({"pywinauto","uiautomation","pyautogui"}&set(sys.modules))


if __name__=="__main__":unittest.main()
