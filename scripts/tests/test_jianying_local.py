"""Isolated metadata fixtures: never writes the actual native catalog."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.jianying_local import core
from scripts.jianying_local.runtime import Settings, ToolError, canonical, digest, file_stamp


def fixture_doc(path):
    return {"version": 360000, "id": "draft_old", "name": "source", "duration": 8_000_000,
            "create_time": 1, "update_time": 2, "fps": 30,
            "canvas_config": {"width": 1920, "height": 1080, "unknown": [1, "保留"]},
            "unknown_top": {"nested": [False, None, {"token": "unchanged"}]},
            "materials": {"videos": [{"id": "video", "path": str(path), "duration": 20_000_000, "type": "video", "unknown": 99}],
                          "audios": [], "speeds": [{"id": "speed", "speed": 1}], "audio_fades": [], "transitions": [],
                          "unrecognized_materials": [{"id": "unknown_resource", "unknown": "untouched"}]},
            "tracks": [{"id": "track_old", "type": "video", "name": "手工精剪", "unknown": {"preserve": 1},
                        "segments": [{"id": "left", "material_id": "video", "target_timerange": {"start": 0, "duration": 4_000_000},
                                      "source_timerange": {"start": 2_000_000, "duration": 4_000_000}, "speed": 1, "volume": 1,
                                      "extra_material_refs": ["speed"], "unknown": [1, 2, 3]},
                                     {"id": "right", "material_id": "video", "target_timerange": {"start": 4_000_000, "duration": 4_000_000},
                                      "source_timerange": {"start": 10_000_000, "duration": 4_000_000}, "speed": 1, "volume": 0.9,
                                      "extra_material_refs": ["speed"], "clip": {"manual_crop": "not_rebuilt"}}]}]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jianying-local-tests-")
        self.root = Path(self.tmp.name)
        self.native = self.root / "native"
        self.native.mkdir()
        self.work = self.root / "jobs_root"
        self.vendor = self.root / "synthetic-backend"
        self.settings = Settings(self.native, self.work, self.vendor, True)
        # Mocked payload builders must not accidentally depend on a real backend.
        # These tiny self-authored templates are temporary test data, not vendor code.
        assets = self.settings.vendor / "assets"
        assets.mkdir(parents=True)
        (assets / "draft_meta_info.json").write_bytes(b"{}")
        (assets / "draft_settings_template").write_bytes(b"[General]\n")
        (assets / "key_value_template.json").write_bytes(b"{}")
        self.media = self.root / "original.mp4"
        self.media.write_bytes(b"fixture-video-not-decoded")
        self.music = self.root / "music.wav"
        self.music.write_bytes(b"fixture-audio-not-decoded")
        self.doc = fixture_doc(self.media)
        source = self.native / "source"
        source.mkdir()
        self.meta = {"draft_name": "source", "draft_id": self.doc["id"], "draft_fold_path": str(source),
                     "draft_root_path": str(self.native), "tm_duration": self.doc["duration"], "tm_draft_create": 1,
                     "tm_draft_modified": 2, "unknown": {"preserve": True}}
        self.original = {"draft_info.json": canonical(self.doc), "draft_meta_info.json": canonical(self.meta),
                         "key_value.json": b"{\"untouched\":42}", "draft_settings": b"[General]\nreal_edit_keys=17\n"}
        for k, v in self.original.items(): (source / k).write_bytes(v)
        self.catalog = {"all_draft_store": [{"draft_name": "source", "draft_id": "draft_old", "draft_fold_path": str(source), "unknown": [1,2]}],
                        "draft_ids": 7, "root_path": str(self.native), "unknown": {"native": "retained"}}
        (self.native / "root_meta_info.json").write_bytes(canonical(self.catalog))
        self.patches = [patch.object(core, "app_pids", return_value=[]),
                        patch("scripts.jianying_local.runtime.app_pids", return_value=[]),
                        patch.object(core, "profile", return_value="fixture-code-profile"),
                        patch.object(core, "app_version", return_value="11.5.0.fixture"),
                        patch.object(core, "probe", side_effect=self.fake_probe),
                        patch.object(core.backend, "build", side_effect=self.fake_build),
                        patch.object(core.backend, "add_music", side_effect=self.fake_music),
                        patch.object(core.backend, "transition_payload", side_effect=self.fake_transition)]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.tmp.cleanup)

    def fake_probe(self, path):
        p = Path(path)
        return {"stamp": file_stamp(p), "duration_us": 20_000_000, "video": {"width":1920,"height":1080} if p.suffix == ".mp4" else None,
                "audio": {"codec_name": "pcm"}}

    def fake_build(self, request, settings):
        doc = fixture_doc(self.media)
        doc["name"] = request["name"]
        cursor = 0
        doc["tracks"][0]["segments"] = []
        for i, clip in enumerate(request["clips"]):
            length = clip["out_us"] - clip["in_us"]
            doc["tracks"][0]["segments"].append({"id": f"clip_{i}", "material_id": "video", "target_timerange": {"start": cursor, "duration": length},
                                               "source_timerange": {"start": clip["in_us"], "duration": length}, "speed": 1, "volume": clip["volume"], "extra_material_refs": ["speed"]})
            cursor += length
        doc["duration"] = cursor
        return doc

    def fake_music(self, doc, items, settings):
        ids = []
        for i, item in enumerate(items):
            aid, sid, tid = (f"new_audio_{i}", f"new_speed_{i}", f"new_track_{i}")
            doc["materials"]["audios"].append({"id": aid, "path": item["path"], "duration": 20_000_000})
            doc["materials"]["speeds"].append({"id": sid, "speed": 1})
            refs = [sid]
            if item["fade_in_us"] or item["fade_out_us"]:
                fid = f"new_fade_{i}"
                doc["materials"]["audio_fades"].append({"id": fid, "fade_in_duration": item["fade_in_us"], "fade_out_duration": item["fade_out_us"]})
                refs.append(fid)
            doc["tracks"].append({"id": tid, "type": "audio", "segments": [{"id": f"new_seg_{i}", "material_id": aid,
                                "target_timerange": {"start": item["start_us"], "duration": item["duration_us"]},
                                "source_timerange": {"start": item["in_us"], "duration": item["duration_us"]}, "speed": 1,
                                "volume": item["volume"], "extra_material_refs": refs}]})
            ids.append(tid)
        return ids

    def fake_transition(self, item, settings, source):
        if item["resource_id"] != "6724845717472416269":
            raise ToolError("unverified_transition", "not a canary")
        return {"id": "new_transition", "resource_id": item["resource_id"], "duration": item["duration_us"]}

    def request(self, mode="enrich"):
        value = {"request_id": str(uuid.uuid4()), "mode": mode, "name": "test-copy"}
        if mode == "create": value.update(canvas={"width":1920,"height":1080,"fps":30}, clips=[{"path":str(self.media),"in_us":2_000_000,"out_us":6_000_000}])
        else: value.update(source_draft="source", bgm=[{"path":str(self.music),"start_us":0,"duration_us":8_000_000,"volume":0.3,"fade_in_us":500000,"fade_out_us":500000,
                     "ducking":[{"start_us":2_000_000,"end_us":3_000_000,"volume":0.08,"attack_us":200000,"release_us":300000}]}],
                     transitions=[{"after_segment_id":"left","before_segment_id":"right","resource_id":"6724845717472416269","duration_us":500000}])
        return value

    def apply(self, req=None):
        preview = core.plan_draft(self.settings, req or self.request())
        return preview, core.apply_plan(self.settings, preview["plan_id"], preview["plan_sha256"])

    def error(self, code, call):
        with self.assertRaises(ToolError) as ctx: call()
        self.assertEqual(code, ctx.exception.code)

    def test_create_full_reference_and_no_overwrite(self):
        preview, receipt = self.apply(self.request("create"))
        output = json.loads((Path(receipt["draft_path"])/"draft_info.json").read_bytes())
        self.assertEqual(output["materials"]["videos"][0]["path"],str(self.media))
        self.assertEqual(output["materials"]["videos"][0]["duration"],20_000_000)
        self.assertEqual(0,receipt["media_copies"])
        self.assertEqual({"draft_info.json","draft_meta_info.json","draft_settings","key_value.json"},{p.name for p in Path(receipt["draft_path"]).iterdir()})
        self.error("target_exists", lambda: core.plan_draft(self.settings, self.request("create")))

    def test_enrich_unknown_fields_and_original_tracks_preserved(self):
        preview, receipt = self.apply()
        plan = core.load_plan(self.settings, preview["plan_id"])
        out = json.loads((Path(receipt["draft_path"])/"draft_info.json").read_bytes())
        self.assertTrue(core.prove_boundary(self.doc,out,plan["delta"]))
        for k,v in self.original.items(): self.assertEqual(v,(self.native/"source"/k).read_bytes())
        self.assertEqual(self.catalog["all_draft_store"],json.loads((self.native/"root_meta_info.json").read_bytes())["all_draft_store"][1:])
        self.assertEqual("pending",receipt["editor_acceptance"])
        self.assertEqual("passed",core.verify_draft(self.settings,"test-copy",preview["plan_id"])["boundary_validation"])

    def make_mirror(self):
        source = self.native / "source"
        (source / "draft_content.json").write_bytes((source / "draft_info.json").read_bytes())

    def test_mirrored_read_inspect_and_all_fingerprints(self):
        self.make_mirror()
        source = core.read_source(self.settings, "source")
        self.assertEqual("flat-mirrored-360000", source["format"])
        self.assertEqual("draft_content.json", source["content_name"])
        self.assertEqual(self.doc, source["doc"])
        self.assertTrue({"draft_info.json", "draft_content.json"} <= {Path(s["path"]).name for s in source["files"]})
        self.assertEqual("flat-mirrored-360000", core.inspect_draft(self.settings, "source")["format"])

    def test_mirrored_copy_sync_original_track_and_aux_preserved(self):
        self.make_mirror()
        before = {p.name: p.read_bytes() for p in (self.native / "source").iterdir()}
        preview, receipt = self.apply()
        target = Path(receipt["draft_path"])
        self.assertEqual((target / "draft_info.json").read_bytes(), (target / "draft_content.json").read_bytes())
        plan = core.load_plan(self.settings, preview["plan_id"])
        self.assertEqual("flat-mirrored-360000", plan["format"])
        output = json.loads((target / "draft_content.json").read_bytes())
        restored = copy.deepcopy(output["tracks"][0])
        restored["segments"][0]["extra_material_refs"].remove(plan["delta"]["transitions"][0]["resource"]["id"])
        self.assertEqual(self.doc["tracks"][0], restored)
        self.assertEqual(self.doc["unknown_top"], output["unknown_top"])
        for name, value in before.items():
            self.assertEqual(value, (self.native / "source" / name).read_bytes())
        self.assertEqual("passed", core.verify_draft(self.settings, "test-copy", preview["plan_id"])["boundary_validation"])

    def test_mirrored_semantically_equal_but_different_bytes_rejected(self):
        self.make_mirror()
        (self.native / "source" / "draft_info.json").write_bytes(json.dumps(self.doc, indent=2).encode())
        self.error("save_state_conflict", lambda: core.read_source(self.settings, "source"))

    def test_mirrored_differing_contents_rejected_no_mtime_guess(self):
        self.make_mirror()
        changed = copy.deepcopy(self.doc)
        changed["tracks"][0]["segments"][0]["volume"] = .2
        (self.native / "source" / "draft_content.json").write_bytes(canonical(changed))
        self.error("save_state_conflict", lambda: core.plan_draft(self.settings, self.request()))

    def test_mirrored_opaque_or_nonobject_rejected_without_codec(self):
        self.make_mirror()
        for value, code in ((b"encoded:opaque", "unreadable_json"), (b"[]", "unsupported_structure")):
            for name in core.CONTENT_NAMES:
                (self.native / "source" / name).write_bytes(value)
            with patch.object(core.codec, "unpack", side_effect=AssertionError("No flat decryption fallback")):
                self.error(code, lambda: core.read_source(self.settings, "source"))

    def test_mirrored_identity_and_dependency_checks_still_required(self):
        changed = copy.deepcopy(self.doc)
        changed["id"] = "other"
        for name in core.CONTENT_NAMES:
            (self.native / "source" / name).write_bytes(canonical(changed))
        self.error("unsupported_structure", lambda: core.read_source(self.settings, "source"))
        changed = copy.deepcopy(self.doc)
        changed["unrecognized"] = {"cache_path": str(self.music)}
        for name in core.CONTENT_NAMES:
            (self.native / "source" / name).write_bytes(canonical(changed))
        self.error("unknown_dependency", lambda: core.read_source(self.settings, "source"))

    def test_mirrored_allow_missing_diagnostic_uses_same_rules(self):
        self.make_mirror()
        self.media.unlink()
        result = core.read_source(self.settings, "source", allow_missing=True)
        self.assertEqual("flat-mirrored-360000", result["format"])
        self.assertEqual(1, len(result["missing"]))
        (self.native / "source" / "draft_info.json").write_bytes(b"different")
        self.error("save_state_conflict", lambda: core.read_source(self.settings, "source", allow_missing=True))

    def test_mirrored_either_source_file_change_invalidates_plan(self):
        self.make_mirror()
        for name in sorted(core.CONTENT_NAMES):
            request = self.request()
            request["name"] = "test-" + uuid.uuid4().hex
            preview = core.plan_draft(self.settings, request)
            path = self.native / "source" / name
            before = path.read_bytes()
            path.write_bytes(before + b" ")
            self.error("stale_input", lambda: core.apply_plan(self.settings, preview["plan_id"], preview["plan_sha256"]))
            path.write_bytes(before)
            self.assertFalse((self.native / request["name"]).exists())

    def test_mirrored_secondary_output_tampering_rejected(self):
        self.make_mirror()
        preview = core.plan_draft(self.settings, self.request())
        output = core.job_path(self.settings, preview["plan_id"]) / "payload" / "draft_info.json"
        output.write_bytes(output.read_bytes() + b" ")
        self.error("payload_changed", lambda: core.apply_plan(self.settings, preview["plan_id"], preview["plan_sha256"]))

    def test_mirrored_duplicate_submission_creates_one_copy(self):
        self.make_mirror()
        request = self.request()
        preview, receipt = self.apply(request)
        repeated = core.apply_plan(self.settings, preview["plan_id"], preview["plan_sha256"])
        self.assertTrue(repeated["idempotent_replay"])
        self.assertEqual(receipt["draft_path"], repeated["draft_path"])
        self.assertEqual(2, len(json.loads((self.native / "root_meta_info.json").read_bytes())["all_draft_store"]))

    def test_mirrored_production_needs_additional_native_evidence(self):
        prod = Settings(self.native, self.work, self.vendor, False)
        evidence = {"app_version": "11.5.0.fixture"}
        with patch.object(core, "accepted", return_value=evidence):
            self.error("native_acceptance_required", lambda: core.require_acceptance(prod, "flat-mirrored-360000"))
            evidence["mirrored_plaintext"] = {"native_validation": "passed"}
            core.require_acceptance(prod, "flat-mirrored-360000")

    def test_original_retime_rejected_by_boundary(self):
        preview,_ = self.apply()
        plan = core.load_plan(self.settings,preview["plan_id"])
        doc = json.loads((self.native/"test-copy"/"draft_info.json").read_bytes())
        doc["tracks"][0]["segments"][1]["volume"] = .1
        self.error("boundary_violation",lambda:core.prove_boundary(self.doc,doc,plan["delta"]))

    def test_duplicate_request_and_replay_do_not_overwrite_user_edits(self):
        req=self.request()
        preview,receipt=self.apply(req)
        self.assertEqual(preview["plan_sha256"],core.plan_draft(self.settings,req)["plan_sha256"])
        edited=self.native/"test-copy"/"draft_info.json"
        edited.write_bytes(edited.read_bytes()+b"\n")
        replay=core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertTrue(edited.read_bytes().endswith(b"\n"))
        changed=copy.deepcopy(req); changed["name"]="different"
        self.error("request_id_conflict",lambda:core.plan_draft(self.settings,changed))

    def test_editor_running_rejected(self):
        with patch("scripts.jianying_local.runtime.app_pids",return_value=[123]):
            self.error("editor_running",lambda:core.plan_draft(self.settings,self.request()))

    def test_application_restarts_before_catalog_write(self):
        preview=core.plan_draft(self.settings,self.request())
        with patch("scripts.jianying_local.runtime.app_pids",side_effect=[[],[],[77]]):
            self.error("editor_running",lambda:core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"]))
        self.assertEqual(self.catalog,json.loads((self.native/"root_meta_info.json").read_bytes()))
        receipt=json.loads((core.job_path(self.settings,preview["plan_id"])/"receipt.json").read_bytes())
        self.assertEqual("failed",receipt["status"])
        self.assertTrue(Path(receipt["catalog_backup"]).exists())

    def test_stale_source_media_or_catalog_rejected(self):
        for changed in (self.native/"source"/"draft_info.json",self.media,self.native/"root_meta_info.json"):
            req=self.request(); req["name"]="test-"+uuid.uuid4().hex
            preview=core.plan_draft(self.settings,req)
            old=changed.read_bytes(); changed.write_bytes(old+b" ")
            self.error("stale_input",lambda:core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"]))
            changed.write_bytes(old)
            self.assertFalse((self.native/req["name"]).exists())

    def test_stale_hash_code_profile_and_tampered_payload(self):
        preview=core.plan_draft(self.settings,self.request())
        self.error("stale_plan",lambda:core.apply_plan(self.settings,preview["plan_id"],"0"*64))
        with patch.object(core,"profile",return_value="upgrade"):
            self.error("stale_plan",lambda:core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"]))
        f=core.job_path(self.settings,preview["plan_id"])/"payload"/"draft_info.json"
        f.write_bytes(f.read_bytes()+b" ")
        self.error("payload_changed",lambda:core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"]))

    def test_sampled_media_change_with_restored_size_and_time_rejected(self):
        preview=core.plan_draft(self.settings,self.request())
        before=self.media.stat()
        self.media.write_bytes(b"X"*before.st_size)
        os.utime(self.media,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.error("stale_input",lambda:core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"]))

    def test_overlapping_roots_rejected(self):
        settings=Settings(self.native,self.native/"jobs",self.vendor,True)
        self.error("invalid_path",lambda:core.plan_draft(settings,self.request()))

    def test_reparse_media_rejected_when_host_allows_link_creation(self):
        link=self.root/"linked.mp4"
        try:link.symlink_to(self.media)
        except OSError:self.skipTest("Windows symlink creation unavailable")
        req=self.request("create");req["clips"][0]["path"]=str(link)
        self.error("reparse_path",lambda:core.plan_draft(self.settings,req))

    def test_unknown_structure_opaque_and_internal_dependency_rejected(self):
        extra=self.native/"source"/"assets";extra.mkdir()
        self.error("unsupported_structure",lambda:core.read_source(self.settings,"source"));extra.rmdir()
        path=self.native/"source"/"draft_info.json";path.write_bytes(b"opaque")
        self.error("unreadable_json",lambda:core.read_source(self.settings,"source"));path.write_bytes(self.original[path.name])
        d=copy.deepcopy(self.doc);d["unrecognized"]= {"cache_path":str(self.music)}
        path.write_bytes(canonical(d))
        self.error("unknown_dependency",lambda:core.read_source(self.settings,"source"))

    def test_input_out_of_bounds_and_unknown_fields(self):
        req=self.request("create");req["clips"][0]["out_us"]=21_000_000
        self.error("out_of_range",lambda:core.plan_draft(self.settings,req))
        req=self.request();req["bgm"][0]["duration_us"]=9_000_000
        self.error("out_of_range",lambda:core.plan_draft(self.settings,req))
        req=self.request();req["bgm"][0]["volume"]=float("nan")
        self.error("invalid_input",lambda:core.plan_draft(self.settings,req))
        req=self.request();req["insert"]=[]
        self.error("invalid_input",lambda:core.plan_draft(self.settings,req))
        req=self.request();req["canvas"]={}
        self.error("invalid_input",lambda:core.plan_draft(self.settings,req))

    def test_ducking_overlap_and_missing_intervals(self):
        req=self.request();req["bgm"][0]["ducking"]*=2
        self.error("out_of_range",lambda:core.plan_draft(self.settings,req))
        req=self.request();req["bgm"][0].pop("ducking")
        preview=core.plan_draft(self.settings,req)
        self.assertEqual(0,preview["preview"]["explicit_ducking_intervals"])
        self.assertEqual("not_performed",preview["preview"]["speech_detection"])

    def test_transition_conflict_resource_and_handles(self):
        req=self.request();req["transitions"]*=2
        self.error("transition_conflict",lambda:core.plan_draft(self.settings,req))
        req=self.request();req["transitions"][0]["resource_id"]="unknown"
        self.error("unverified_transition",lambda:core.plan_draft(self.settings,req))
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][1]["source_timerange"]["start"]=0
        (self.native/"source"/"draft_info.json").write_bytes(canonical(d))
        self.error("insufficient_handles",lambda:core.plan_draft(self.settings,self.request()))

    def test_broken_link_and_duplicate_identity(self):
        d=copy.deepcopy(self.doc);d["tracks"][0]["segments"][0]["extra_material_refs"].append("lost")
        self.error("broken_reference",lambda:core.validate_doc(d))
        d=copy.deepcopy(self.doc);d["materials"]["speeds"].append(d["materials"]["speeds"][0])
        self.error("duplicate_id",lambda:core.validate_doc(d))

    def test_catalog_conflict_preserves_entries(self):
        c=copy.deepcopy(self.catalog);c["all_draft_store"].append({"draft_name":"test-copy","draft_id":"stranger"})
        (self.native/"root_meta_info.json").write_bytes(canonical(c))
        self.error("catalog_conflict",lambda:core.plan_draft(self.settings,self.request()))
        self.assertEqual(c,json.loads((self.native/"root_meta_info.json").read_bytes()))

    def test_failed_catalog_replace_keeps_backup_and_original(self):
        preview=core.plan_draft(self.settings,self.request())
        original_atomic=core.atomic_write
        def fail_catalog(path,data):
            if path.name=="root_meta_info.json":raise OSError("simulated catalog failure")
            return original_atomic(path,data)
        with patch.object(core,"atomic_write",side_effect=fail_catalog):
            with self.assertRaises(OSError):core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"])
        self.assertEqual(self.catalog,json.loads((self.native/"root_meta_info.json").read_bytes()))
        self.error("previous_failure",lambda:core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"]))
        self.assertTrue((core.job_path(self.settings,preview["plan_id"])/"catalog_before.json").exists())

    def test_exact_completed_publication_recovers_interrupted_receipt(self):
        preview,receipt=self.apply()
        receipt["status"]="publication_prepared"
        (core.job_path(self.settings,preview["plan_id"])/"receipt.json").write_bytes(canonical(receipt))
        recovered=core.apply_plan(self.settings,preview["plan_id"],preview["plan_sha256"])
        self.assertTrue(recovered["recovered_exact_publication"])
        self.assertEqual(2,len(json.loads((self.native/"root_meta_info.json").read_bytes())["all_draft_store"]))

    def test_production_gate_no_native_write(self):
        preview=core.plan_draft(self.settings,self.request("create"))
        prod=Settings(self.native,self.work,self.vendor,False)
        self.error("native_acceptance_required",lambda:core.apply_plan(prod,preview["plan_id"],preview["plan_sha256"]))
        self.assertFalse((self.native/"test-copy").exists())

    def test_path_escape_and_duplicate_json_key(self):
        from scripts.jianying_local.runtime import decode
        for name in ("../other","CON","ends.","", "with:colon"):
            req=self.request();req["name"]=name
            self.error("invalid_name",lambda:core.plan_draft(self.settings,req))
        self.error("invalid_json",lambda:decode(b'{"a":1,"a":2}'))


if __name__ == "__main__":unittest.main()
