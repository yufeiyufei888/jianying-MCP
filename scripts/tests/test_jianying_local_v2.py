"""Second-stage boundaries, exclusively temp metadata/media fixtures."""
import copy
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.jianying_local import codec, core, dependencies, formats, music, patching, subtitles, verification
from scripts.jianying_local.runtime import Settings, ToolError, canonical, decode, digest, file_stamp
from scripts.tests.test_jianying_local import PipelineTests, fixture_doc


class V2Tests(unittest.TestCase):
    setUp = PipelineTests.setUp
    fake_probe = PipelineTests.fake_probe
    fake_music = PipelineTests.fake_music
    fake_transition = PipelineTests.fake_transition
    request = PipelineTests.request
    apply = PipelineTests.apply
    error = PipelineTests.error

    def fake_build(self, request, settings):
        doc = PipelineTests.fake_build(self, request, settings)
        if request["name"] == "insert-helper":
            mapping = {m["id"]: "insert_"+m["id"] for values in doc["materials"].values() for m in values}
            for values in doc["materials"].values():
                for m in values:
                    m["id"] = mapping[m["id"]]
            for t,s in core.segments(doc):
                s["id"] = "insert_"+uuid.uuid4().hex
                s["material_id"] = mapping[s["material_id"]]
                s["extra_material_refs"] = [mapping[x] for x in s["extra_material_refs"]]
        return doc

    def patch_request(self, ops=None, **extra):
        return {"request_id": str(uuid.uuid4()), "mode": "patch", "name": "test-copy", "source_draft": "source",
                "operations": ops or [], **extra}

    def operation(self, kind, **extra):
        return {"type": kind, "track_id": "track_old", "companion_tracks": {}, **extra}

    def result_doc(self, preview):
        return decode((core.job_path(self.settings, preview["plan_id"])/"expected_doc.json").read_bytes())

    def add_companion(self, start=5_000_000, length=1_000_000):
        self.doc["tracks"].append({"id":"companion", "type":"audio", "name":"保留", "segments":[
            {"id":"sound", "material_id":"video", "target_timerange":{"start":start,"duration":length},
             "source_timerange":{"start":2_000_000,"duration":length}, "speed":1, "volume":.5, "extra_material_refs":[], "unknown":99}]})
        (self.native/"source"/"draft_info.json").write_bytes(canonical(self.doc))

    def test_v2_plan_and_historical_v1_rejected(self):
        p = core.plan_draft(self.settings, self.request())
        plan = core.load_plan(self.settings,p["plan_id"])
        self.assertEqual("jianying-local-plan/2",plan["schema"])
        plan["schema"] = "jianying-local-plan/1"
        self.error("stale_plan",lambda:core.check_inputs(plan,self.settings))

    def test_trim_extend_preserves_unknowns_and_ids(self):
        p,r = self.apply(self.patch_request([self.operation("trim_clip",segment_id="left",in_us=1_000_000,out_us=7_000_000)]))
        out = self.result_doc(p)
        self.assertEqual(10_000_000,out["duration"])
        self.assertEqual(["left","right"],[s["id"] for s in out["tracks"][0]["segments"]])
        self.assertEqual(self.doc["tracks"][0]["segments"][0]["unknown"],out["tracks"][0]["segments"][0]["unknown"])
        self.assertEqual(6_000_000,out["tracks"][0]["segments"][1]["target_timerange"]["start"])
        self.assertEqual("passed",core.verify_draft(self.settings,"test-copy",p["plan_id"])["file_validation"])

    def test_move_preserves_original_ids_and_parameters(self):
        p = core.plan_draft(self.settings,self.patch_request([self.operation("move_clip",segment_id="right",anchor_segment_id="left",position="before")]))
        out = self.result_doc(p)
        self.assertEqual(["right","left"],[s["id"] for s in out["tracks"][0]["segments"]])
        self.assertEqual(.9,out["tracks"][0]["segments"][0]["volume"])

    def test_insert_only_missing_clip(self):
        p = core.plan_draft(self.settings,self.patch_request([self.operation("insert_clip",anchor_segment_id="left",position="after",
                               clip={"path":str(self.media),"in_us":1_000_000,"out_us":3_000_000})]))
        out = self.result_doc(p)
        self.assertEqual(3,len(out["tracks"][0]["segments"]))
        self.assertEqual("left",out["tracks"][0]["segments"][0]["id"])
        self.assertEqual("right",out["tracks"][0]["segments"][2]["id"])
        self.assertEqual(20_000_000,out["materials"]["videos"][-1]["duration"])

    def test_companion_policy_required(self):
        self.add_companion()
        self.error("companion_policy_required",lambda:core.plan_draft(self.settings,self.patch_request([self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)])))

    def test_follow_companion_shift(self):
        self.add_companion()
        op = self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)
        op["companion_tracks"] = {"companion":"follow"}
        p = core.plan_draft(self.settings,self.patch_request([op]))
        out = self.result_doc(p)
        self.assertEqual(6_000_000,out["tracks"][1]["segments"][0]["target_timerange"]["start"])
        self.assertEqual(99,out["tracks"][1]["segments"][0]["unknown"])

    def test_follow_cross_discontinuous_edit_rejected(self):
        self.add_companion(3_000_000,2_000_000)
        op = self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)
        op["companion_tracks"]={"companion":"follow"}
        self.error("companion_crosses_edit",lambda:core.plan_draft(self.settings,self.patch_request([op])))

    def test_fixed_companion_outside_new_duration_rejected(self):
        self.add_companion(7_000_000,1_000_000)
        op = self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=3_000_000)
        op["companion_tracks"]={"companion":"fixed"}
        self.error("fixed_track_out_of_range",lambda:core.plan_draft(self.settings,self.patch_request([op])))

    def test_sequential_operations_use_latest_local_coordinates(self):
        self.add_companion()
        a=self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)
        b=self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=8_000_000)
        a["companion_tracks"]=b["companion_tracks"]={"companion":"follow"}
        p=core.plan_draft(self.settings,self.patch_request([a,b]))
        self.assertEqual(7_000_000,self.result_doc(p)["tracks"][1]["segments"][0]["target_timerange"]["start"])

    def test_speed_curve_and_keyframe_trim_rejected(self):
        d=copy.deepcopy(self.doc)
        d["materials"]["speeds"][0]["curve_speed"]={"points":[1,2]}
        self.error("unsupported_clip",lambda:patching.apply(d,[self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)],self.settings))
        d=copy.deepcopy(self.doc)
        d["tracks"][0]["segments"][0]["common_keyframes"]=[{"keyframe_list":[{"time_offset":4_000_000,"values":[1]}]}]
        self.error("keyframe_conflict",lambda:patching.apply(d,[self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=3_000_000)],self.settings))

    def test_transition_cut_change_rejected(self):
        d=copy.deepcopy(self.doc)
        d["materials"]["transitions"].append({"id":"trans","duration":500000,"resource_id":"fixture"})
        d["tracks"][0]["segments"][0]["extra_material_refs"].append("trans")
        self.error("transition_conflict",lambda:patching.apply(d,[self.operation("move_clip",segment_id="right",anchor_segment_id="left",position="before")],self.settings))

    def test_unknown_field_edit_not_hidden_by_patch_boundary(self):
        p=core.plan_draft(self.settings,self.patch_request([self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)]))
        plan=core.load_plan(self.settings,p["plan_id"])
        out=self.result_doc(p);out["tracks"][0]["segments"][1]["clip"]={"bad":"modified"}
        self.error("boundary_violation",lambda:core.prove_boundary(self.doc,out,plan["delta"]))

    def test_inspect_missing_media_and_paging_binding(self):
        page=core.inspect_draft(self.settings,"source",limit=1)
        self.assertIsNotNone(page["next_cursor"])
        self.media.unlink()
        result=core.inspect_draft(self.settings,"source")
        self.assertEqual(1,len(result["missing_media"]))
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][0]["volume"]=.4
        (self.native/"source"/"draft_info.json").write_bytes(canonical(d))
        self.error("stale_cursor",lambda:core.inspect_draft(self.settings,"source",cursor=page["next_cursor"]))

    def test_relink_same_filename_different_content_rejected(self):
        other=self.root/"moved";other.mkdir();new=other/self.media.name
        new.write_bytes(b"other-contents-same-name")
        with patch.object(dependencies,"probe",side_effect=self.fake_probe):
            self.error("media_mismatch",lambda:dependencies.relink(copy.deepcopy(self.doc),copy.deepcopy(self.meta),
                        {"files":[{"old_path":str(self.media),"new_path":str(new)}]},self.settings))

    def test_relink_missing_without_evidence_needs_confirmation(self):
        new=self.root/"moved.mp4";new.write_bytes(self.media.read_bytes());self.media.unlink()
        request={"files":[{"old_path":str(self.media),"new_path":str(new)}]}
        with patch.object(dependencies,"probe",side_effect=self.fake_probe):
            self.error("insufficient_evidence",lambda:dependencies.relink(copy.deepcopy(self.doc),copy.deepcopy(self.meta),request,self.settings))
            request["files"][0]["confirm_unverified"]=True
            result=dependencies.relink(copy.deepcopy(self.doc),copy.deepcopy(self.meta),request,self.settings)
            self.assertIn("confirmation",result[0]["evidence"])

    def test_relink_keeps_segments_and_original_paths(self):
        new=self.root/"moved.mp4";new.write_bytes(self.media.read_bytes())
        with patch.object(dependencies,"probe",side_effect=self.fake_probe):
            p,r=self.apply(self.patch_request(relinks={"files":[{"old_path":str(self.media),"new_path":str(new)}]}))
        out=self.result_doc(p)
        self.assertEqual(self.doc["tracks"],out["tracks"])
        self.assertEqual(str(new),out["materials"]["videos"][0]["path"])
        self.assertEqual(str(self.media),decode((self.native/"source"/"draft_info.json").read_bytes())["materials"]["videos"][0]["path"])

    def test_ambiguous_directory_and_file_mapping_rejected(self):
        new=self.root/"moved";new.mkdir();(new/self.media.name).write_bytes(self.media.read_bytes())
        request={"files":[{"old_path":str(self.media),"new_path":str(new/self.media.name)}],
                 "directories":[{"old_dir":str(self.root),"new_dir":str(new)}]}
        with patch.object(dependencies,"probe",side_effect=self.fake_probe):
            self.error("ambiguous_relink",lambda:dependencies.relink(copy.deepcopy(self.doc),copy.deepcopy(self.meta),request,self.settings))

    def test_directory_relink_same_original_in_multiple_materials(self):
        new=self.root/"moved";new.mkdir();(new/self.media.name).write_bytes(self.media.read_bytes())
        d=copy.deepcopy(self.doc);extra=copy.deepcopy(d["materials"]["videos"][0]);extra["id"]="repeat-material";d["materials"]["videos"].append(extra)
        with patch.object(dependencies,"probe",side_effect=self.fake_probe):
            updates=dependencies.relink(d,copy.deepcopy(self.meta),{"directories":[{"old_dir":str(self.root),"new_dir":str(new)}]},self.settings)
        self.assertEqual(1,len(updates));self.assertEqual(2,len(updates[0]["material_ids"]))

    def test_missing_original_historical_fingerprint_relink(self):
        with patch.object(dependencies,"probe",side_effect=self.fake_probe):
            dependencies.inventory(self.doc,self.meta,self.native/"source",self.settings)
            new=self.root/"moved.mp4";new.write_bytes(self.media.read_bytes());self.media.unlink()
            updates=dependencies.relink(copy.deepcopy(self.doc),copy.deepcopy(self.meta),{"files":[{"old_path":str(self.media),"new_path":str(new)}]},self.settings)
        self.assertEqual("bounded_sample_and_probe",updates[0]["evidence"])

    def test_keyframe_bounds_and_values_checked(self):
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][0]["common_keyframes"]=[{"keyframe_list":[{"time_offset":4000001,"values":[1]}]}]
        self.error("out_of_range",lambda:core.validate_doc(d))
        d["tracks"][0]["segments"][0]["common_keyframes"][0]["keyframe_list"][0].update(time_offset=0,values=[float("nan")])
        self.error("invalid_input",lambda:core.validate_doc(d))

    def test_material_frame_alignment_not_clip_tolerance(self):
        d=copy.deepcopy(self.doc);d["materials"]["videos"][0]["duration"]+=20000
        self.assertEqual(1,len(verification.compare_saved(self.doc,d)["material_duration_frame_alignments"]))
        d["tracks"][0]["segments"][0]["source_timerange"]["start"]+=1
        self.error("native_semantics_changed",lambda:verification.compare_saved(self.doc,d))

    def test_default_allowlist_does_not_discard_unknown_zero_false(self):
        d=copy.deepcopy(self.doc);d["unknown_zero"]=0;d["unknown_false"]=False
        self.error("native_semantics_changed",lambda:verification.compare_saved(self.doc,d))

    def audio_rounding_docs(self):
        expected = copy.deepcopy(self.doc)
        self.fake_music(expected, [{"path": str(self.music), "start_us": 0, "in_us": 0,
            "duration_us": 2_000_000, "volume": .15, "fade_in_us": 0, "fade_out_us": 0}], self.settings)
        expected["materials"]["audios"][0].update(type="extract_music", duration=140_173_063)
        actual = copy.deepcopy(expected)
        actual["last_modified_platform"] = {"app_version": "11.5.0"}
        actual["materials"]["audios"][0]["duration"] = 140_200_000
        return expected, actual

    def test_native_audio_full_material_up_rounding(self):
        expected, actual = self.audio_rounding_docs()
        report = verification.compare_saved(expected, actual)
        self.assertEqual([{"id": "new_audio_0", "before_us": 140_173_063, "after_us": 140_200_000}],
                         report["audio_material_duration_frame_alignments"])

    def test_audio_rounding_requires_exact_native_version(self):
        expected, actual = self.audio_rounding_docs()
        actual["last_modified_platform"]["app_version"] = "12.0.0"
        self.error("native_semantics_changed", lambda: verification.compare_saved(expected, actual))

    def test_audio_rounding_not_generic_frame_tolerance(self):
        for value in (140_173_064, 140_166_666, 140_233_333):
            expected, actual = self.audio_rounding_docs()
            actual["materials"]["audios"][0]["duration"] = value
            self.error("native_semantics_changed", lambda: verification.compare_saved(expected, actual))

    def test_audio_rounding_preserves_material_identity_path_and_type(self):
        for field, value in (("id", "wrong-id"), ("path", str(self.root / "other.wav")), ("type", "music")):
            expected, actual = self.audio_rounding_docs()
            actual["materials"]["audios"][0][field] = value
            self.error("broken_reference" if field == "id" else "native_semantics_changed",
                       lambda: verification.compare_saved(expected, actual))

    def test_audio_rounding_does_not_relax_clip_ranges(self):
        for field in ("source_timerange", "target_timerange"):
            expected, actual = self.audio_rounding_docs()
            actual["tracks"][-1]["segments"][0][field]["start"] += 1
            self.error("native_semantics_changed", lambda: verification.compare_saved(expected, actual))

    def test_audio_rounding_does_not_relax_volume_or_unknown_fields(self):
        for field, value in (("volume", .14), ("unknown_native_zero", 0)):
            expected, actual = self.audio_rounding_docs()
            actual["tracks"][-1]["segments"][0][field] = value
            self.error("native_semantics_changed", lambda: verification.compare_saved(expected, actual))

    def test_changed_format_pin_invalidates_plan(self):
        p=core.plan_draft(self.settings,self.request());plan=core.load_plan(self.settings,p["plan_id"]);plan["format_identity"]={"test_pin":"old"}
        with patch.object(codec,"identity",return_value={"test_pin":"changed"}):
            self.error("stale_plan",lambda:core.check_inputs(plan,self.settings))

    def test_loop_exact_last_clip_and_crossfade_default(self):
        items=[{"path":str(self.music),"start_us":0,"duration_us":45_000_000,"volume":.3,"loop":True,"loop_crossfade":True}]
        core.normalize_music(items,45_000_000,self.settings)
        rows=music.schedule(items)
        self.assertEqual(3,len(rows))
        self.assertEqual(250000,rows[1]["fade_in_us"])
        self.assertEqual(45_000_000,rows[-1]["start_us"]+rows[-1]["duration_us"])

    def test_ducking_across_loop_join(self):
        item={"path":str(self.music),"start_us":0,"duration_us":40_000_000,"volume":.3,"loop":True,"loop_crossfade":True,
              "ducking":[{"start_us":19_600_000,"end_us":20_200_000,"volume":.05,"attack_us":100000,"release_us":100000}]}
        core.normalize_music([item],40_000_000,self.settings)
        rows=music.schedule([item]);join=19_900_000
        gains=[music.interpolate(music.envelope(item,r),join-r["start_us"]) for r in rows if r["start_us"]<=join<r["start_us"]+r["duration_us"]]
        self.assertAlmostEqual(.05,sum(gains),places=6)

    def test_two_bgm_crossfade_explicit_overlap(self):
        items=[{"path":str(self.music),"start_us":0,"duration_us":8_000_000,"volume":.3},
               {"path":str(self.music),"start_us":7_750_000,"duration_us":8_000_000,"volume":.3,"crossfade_previous_us":250000}]
        core.normalize_music(items,15_750_000,self.settings)
        rows=music.schedule(items)
        self.assertEqual(250000,rows[0]["fade_out_us"])
        self.assertEqual(250000,rows[1]["fade_in_us"])

    def srt(self, text):
        path=self.root/(uuid.uuid4().hex+".srt");path.write_text(text,encoding="utf-8")
        return path

    def test_timeline_srt_chinese_bom(self):
        path=self.srt("\ufeff1\n00:00:01,000 --> 00:00:02,000\n今天出发\n")
        result=subtitles.map_cues(self.doc,[{"path":str(path),"basis":"timeline"}])
        self.assertEqual("今天出发",result["mapped"][0]["text"])
        self.assertEqual(1_000_000,result["mapped"][0]["start_us"])

    def test_source_srt_per_occurrence_not_original_order(self):
        path=self.srt("1\n00:00:03,000 --> 00:00:04,000\n重复素材\n")
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][1]["source_timerange"]["start"]=2_000_000
        result=subtitles.map_cues(d,[{"path":str(path),"basis":"source","material_id":"video"}])
        self.assertEqual([1_000_000,5_000_000],[c["start_us"] for c in result["mapped"]])

    def test_partial_cue_preserves_text_and_marks_review(self):
        path=self.srt("1\n00:00:01,000 --> 00:00:03,000\n整句话不擅自改字\n")
        result=subtitles.map_cues(self.doc,[{"path":str(path),"basis":"source","material_id":"video"}])
        self.assertTrue(result["mapped"][0]["partial"])
        self.assertEqual("整句话不擅自改字",result["mapped"][0]["text"])
        self.assertEqual("partial_sentence_pending_review",result["warnings"][0]["kind"])

    def test_overlap_long_and_unmapped_cues_traceable(self):
        path=self.srt("1\n00:00:01,000 --> 00:00:03,000\n"+"长"*50+"\n\n2\n00:00:02,000 --> 00:00:04,000\n重叠\n\n3\n00:00:30,000 --> 00:00:31,000\n不在保留范围\n")
        result=subtitles.map_cues(self.doc,[{"path":str(path),"basis":"timeline"}])
        self.assertEqual(1,len(result["unmapped"]))
        self.assertIn("overlapping_cues_pending_review",{w["kind"] for w in result["warnings"]})

    def test_source_caption_speed_curve_and_reverse_rejected(self):
        path=self.srt("1\n00:00:03,000 --> 00:00:04,000\n测试\n")
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][0]["reverse"]=True
        self.error("unsupported_mapping",lambda:subtitles.map_cues(d,[{"path":str(path),"basis":"source","material_id":"video"}]))

    def test_constant_speed_mapping(self):
        path=self.srt("1\n00:00:03,000 --> 00:00:05,000\n两倍速\n")
        d=copy.deepcopy(self.doc);s=d["tracks"][0]["segments"][0]
        s["speed"]=2;s["source_timerange"]["duration"]=8_000_000
        result=subtitles.map_cues(d,[{"path":str(path),"basis":"source","material_id":"video"}])
        self.assertEqual([500000,1500000],[result["mapped"][0]["start_us"],result["mapped"][0]["end_us"]])

    def test_srt_encoding_and_malformed_rejected(self):
        self.error("invalid_srt",lambda:subtitles.parse(b"1\n00:00:01,000 --> 00:00:00,000\nbad"))
        self.error("invalid_encoding",lambda:subtitles.parse("中文".encode("gb18030")))
        cue="1\n00:00:01,000 --> 00:00:02,000\n中文"
        self.assertEqual("中文",subtitles.parse(cue.encode("gb18030"),"gb18030")[0]["text"])

    def test_unknown_fields_are_not_ignored_after_save(self):
        d=copy.deepcopy(self.doc);d["unknown_top"]["nested"][0]=True
        self.error("native_semantics_changed",lambda:verification.compare_saved(self.doc,d))

    def test_semantic_defaults_and_actual_volume_changes(self):
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][0].pop("speed");d["tracks"][0]["segments"][0].pop("volume")
        self.assertEqual("passed",verification.compare_saved(self.doc,d)["semantic_validation"])
        d["tracks"][0]["segments"][0]["volume"]=.5
        self.error("native_semantics_changed",lambda:verification.compare_saved(self.doc,d))


class NestedTests(V2Tests):
    # Use an explicit loader, not inherited test methods, to avoid duplicate counts.
    def nested(self):
        folder=self.native/"source";tid=str(uuid.uuid4()).upper()
        d=copy.deepcopy(self.doc);d["id"]=tid;d["last_modified_platform"]={"app_version":"11.5.0"}
        m=copy.deepcopy(self.meta);m["draft_id"]=tid
        sub=folder/"Timelines"/tid;sub.mkdir(parents=True)
        (folder/"Timelines"/"project.json").write_bytes(canonical({"id":tid,"main_timeline_id":tid,"timelines":[{"id":tid}],"version":0}))
        (folder/"draft_content.json").write_bytes(b"encoded:"+canonical(d))
        (sub/"draft_content.json").write_bytes(b"encoded:"+canonical(d))
        (folder/"draft_meta_info.json").write_bytes(b"encoded:"+canonical(m))
        (folder/"timeline_layout.json").write_bytes(canonical({"activeTimeline":tid,"unknown":44}))
        patches=[patch.object(codec,"identity",return_value={"fixture":"pin"}),
                 patch.object(codec,"unpack",side_effect=lambda b,s:(decode(b[8:]),True) if b.startswith(b"encoded:") else (decode(b),False)),
                 patch.object(codec,"pack",side_effect=lambda d,s,e:b"encoded:"+canonical(d) if e else canonical(d))]
        for p in patches:p.start();self.addCleanup(p.stop)
        return folder,sub,d,m

    def test_latest_nested_not_stale_plain(self):
        folder,sub,d,m=self.nested()
        stale=copy.deepcopy(self.doc);stale["duration"]=1
        (folder/"draft_info.json").write_bytes(canonical(stale))
        source=core.read_source(self.settings,"source")
        self.assertEqual(8_000_000,source["doc"]["duration"])
        self.assertIn("Timelines/",source["content_name"])

    def test_root_active_conflict_rejected(self):
        folder,sub,d,m=self.nested();d["duration"]=9_000_000
        (folder/"draft_content.json").write_bytes(b"encoded:"+canonical(d))
        self.error("save_state_conflict",lambda:core.read_source(self.settings,"source"))

    def test_identical_flat_pair_cannot_override_active_timeline(self):
        folder, sub, d, m = self.nested()
        flat = canonical(d)
        (folder / "draft_info.json").write_bytes(flat)
        (folder / "draft_content.json").write_bytes(flat)
        active = copy.deepcopy(d)
        active["tracks"][0]["segments"][0]["volume"] = .2
        (sub / "draft_content.json").write_bytes(b"encoded:" + canonical(active))
        self.error("save_state_conflict", lambda: core.read_source(self.settings, "source"))

    def test_malformed_active_project_cannot_fall_back_to_plain_pair(self):
        folder, sub, d, m = self.nested()
        for name in core.CONTENT_NAMES:
            (folder / name).write_bytes(canonical(d))
        (folder / "Timelines" / "project.json").write_bytes(b"{}")
        self.error("unsupported_structure", lambda: core.read_source(self.settings, "source"))

    def test_multiple_timelines_rejected(self):
        folder,sub,d,m=self.nested()
        path=folder/"Timelines"/"project.json";project=decode(path.read_bytes());project["timelines"].append({"id":"second"});path.write_bytes(canonical(project))
        self.error("unsupported_structure",lambda:core.read_source(self.settings,"source"))

    def test_nested_copy_preserves_aux_unknown_and_updates_id(self):
        folder,sub,d,m=self.nested()
        p,r=self.apply(self.patch_request([self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)]))
        out=self.result_doc(p);target=Path(r["draft_path"])
        layout=decode((target/"timeline_layout.json").read_bytes())
        self.assertEqual(44,layout["unknown"]);self.assertEqual(out["id"],layout["activeTimeline"])
        self.assertTrue((target/"Timelines"/out["id"]/"draft_content.json").is_file())
        self.assertFalse((target/"draft_info.json").exists())
        self.assertEqual(d,codec.unpack((sub/"draft_content.json").read_bytes(),self.settings)[0])

    def test_resource_budget_and_unknown_asset_stop(self):
        folder,sub,d,m=self.nested()
        (folder/"draft_cover.jpg").write_bytes(b"x"*(20*1024*1024+1))
        self.error("resource_budget",lambda:core.read_source(self.settings,"source"))
        (folder/"draft_cover.jpg").unlink();(folder/"unknown-resource.bin").write_bytes(b"opaque")
        self.error("unknown_dependency",lambda:core.read_source(self.settings,"source"))

    def test_auxiliary_hidden_path_dependency_rejected(self):
        folder,sub,d,m=self.nested();(folder/"attachment_pc_common.json").write_bytes(canonical({"unknown_path":str(self.media)}))
        self.error("unknown_dependency",lambda:core.read_source(self.settings,"source"))

    def saved_default_attachments(self):
        folder,sub,d,m=self.nested()
        editing=(Path(__file__).parent/"fixtures"/"jianying_11_5_empty_editing_attachment.json").read_bytes()
        plugin=b'{"plugin_draft":{"plugin_segments":[],"version":"1.0.0"}}\r\n'
        for root in (folder,sub):
            (root/"attachment_editing.json").write_bytes(editing)
            (root/"common_attachment").mkdir()
            (root/"common_attachment"/"attachment_plugin_draft.json").write_bytes(plugin)
        return folder,sub,editing,plugin

    def test_native_default_sidecars_latest_read_and_copy_unchanged(self):
        folder,sub,editing,plugin=self.saved_default_attachments()
        source=core.read_source(self.settings,"source")
        self.assertEqual("nested-single-11.5",source["format"])
        p,r=self.apply(self.patch_request([self.operation("trim_clip",segment_id="left",in_us=2_000_000,out_us=7_000_000)]))
        out=self.result_doc(p);target=Path(r["draft_path"])
        for root in (target,target/"Timelines"/out["id"]):
            self.assertEqual(editing,(root/"attachment_editing.json").read_bytes())
            self.assertEqual(plugin,(root/"common_attachment"/"attachment_plugin_draft.json").read_bytes())
        self.assertEqual(editing,(folder/"attachment_editing.json").read_bytes())
        self.assertEqual(plugin,(sub/"common_attachment"/"attachment_plugin_draft.json").read_bytes())

    def test_nonempty_or_unknown_plugin_sidecar_rejected(self):
        folder,sub,editing,plugin=self.saved_default_attachments()
        path=sub/"common_attachment"/"attachment_plugin_draft.json"
        for changed in ({"plugin_draft":{"plugin_segments":[{"id":"hidden"}],"version":"1.0.0"}},
                        {"plugin_draft":{"plugin_segments":[],"version":"1.0.1"}},
                        {"plugin_draft":{"plugin_segments":[],"version":"1.0.0","unknown":False}}):
            with self.subTest(changed=changed):
                path.write_bytes(canonical(changed))
                self.error("unknown_dependency",lambda:core.read_source(self.settings,"source"))

    def test_editing_sidecar_hidden_resources_and_unknown_flags_rejected(self):
        folder,sub,editing,plugin=self.saved_default_attachments()
        path=folder/"attachment_editing.json"
        for field,value in (("slot_image_path",str(self.media)),("unknown_false",False),("version","1.0.1")):
            changed=decode(editing)
            if field=="slot_image_path":changed["editing_draft"]["cover_extra_info"][field]=value
            else:changed["editing_draft"][field]=value
            with self.subTest(field=field):
                path.write_bytes(canonical(changed))
                self.error("unknown_dependency",lambda:core.read_source(self.settings,"source"))

    def test_known_attachment_in_unknown_subdirectory_rejected(self):
        folder,sub,editing,plugin=self.saved_default_attachments()
        deeper=sub/"common_attachment"/"unvalidated";deeper.mkdir()
        (deeper/"attachment_plugin_draft.json").write_bytes(plugin)
        self.error("unknown_dependency",lambda:core.read_source(self.settings,"source"))

    def test_saved_editor_profile_rejected(self):
        folder,sub,d,m=self.nested();d["last_modified_platform"]["app_version"]="12.0.0"
        for path in (folder/"draft_content.json",sub/"draft_content.json"):path.write_bytes(b"encoded:"+canonical(d))
        self.error("unsupported_editor",lambda:core.read_source(self.settings,"source"))


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="jy-codec-tests-");self.root=Path(self.tmp.name)
        self.settings=Settings(self.root/"native",self.root/"work",self.root/"skill",True)
        self.pin=patch.object(codec,"identity",return_value={"pinned":"fixture"});self.pin.start()
        self.addCleanup(self.pin.stop);self.addCleanup(self.tmp.cleanup)

    def error(self,code,call):
        with self.assertRaises(ToolError) as c:call()
        self.assertEqual(code,c.exception.code)

    def child(self,args,**kwargs):
        source=Path(args[-2]).read_bytes()
        Path(args[-1]).write_bytes(b"encoded:"+source if args[1]=="-e" else source[8:])
        return 0,b"",b"",{"peak_working_set":123}

    def test_codec_roundtrip_and_cache_no_second_process(self):
        with patch.object(codec,"run_monitored",side_effect=self.child) as worker:
            data=canonical({"unknown":[False,0,"中文"]});encoded=codec.convert(data,self.settings,encrypt=True)
            self.assertEqual(data,codec.convert(encoded,self.settings));self.assertEqual(2,worker.call_count)

    def test_codec_cached_output_tamper_rejected(self):
        with patch.object(codec,"run_monitored",side_effect=self.child):
            data=b'encoded:{"known":1}';codec.convert(data,self.settings)
            receipt=decode(next((self.settings.work_root/"codec_snapshots").glob("*.json")).read_bytes())
            (self.settings.work_root/"codec_snapshots"/receipt["directory"]/"output.bin").write_bytes(b"{}")
            self.error("codec_cache_changed",lambda:codec.convert(data,self.settings))

    def test_codec_child_crash_preserves_snapshot_and_diagnostic(self):
        with patch.object(codec,"run_monitored",return_value=(9,b"",b"fixture-crash",{})):
            self.error("codec_failed",lambda:codec.convert(b"encoded:{}",self.settings))
        self.assertEqual(1,len(list(self.settings.work_root.rglob("failure.json"))))
        self.assertEqual(1,len(list(self.settings.work_root.rglob("input.bin"))))

    def test_codec_child_timeout_preserves_snapshot(self):
        with patch.object(codec,"run_monitored",side_effect=ToolError("worker_timeout","fixture timeout")):
            self.error("worker_timeout",lambda:codec.convert(b"encoded:{}",self.settings))
        self.assertEqual(1,len(list(self.settings.work_root.rglob("failure.json"))))

    def test_real_timeout_kills_only_own_child(self):
        import sys
        self.error("worker_timeout",lambda:codec.run_monitored([sys.executable,"-I","-B","-c","import time;time.sleep(2)"],cwd=self.root,timeout=.05))


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(V2Tests))
    suite.addTests(loader.loadTestsFromTestCase(CodecTests))
    for name in NestedTests.__dict__:
        if name.startswith("test_"):
            suite.addTest(NestedTests(name))
    return suite

if __name__ == "__main__": unittest.main()
