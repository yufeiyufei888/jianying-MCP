"""Record explicit human phase2 checks, then independently read native saved state.

Admin only; no MCP tool can manufacture or submit human acceptance.
"""
from __future__ import annotations
import argparse
import copy
import sys
import time
from pathlib import Path

sys.dont_write_bytecode=True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.jianying_local import core, codec, verification
from scripts.jianying_local.runtime import Settings, canonical, check_stamp, decode, digest, load_settings, require, require_closed, safe_path, write_new

CHECKS = {
    "format_open_save_reopen", "format_first_clip_volume_changed_in_native_ui",
    "local_order_ranges_extension", "companion_follow_sync",
    "timeline_and_original_srt_repeat_mapping", "partial_caption_text_reviewed",
    "bottom_chinese_editable_landscape_portrait", "music_loops_joins_fades_ducking_audible",
    "flash_transition_visible_saved_reopened", "relinked_synthetic_media_plays",
    "all_five_saved_closed_reopened", "original_drafts_and_media_not_edited",
}


def record(settings, report):
    core.obj(report, ("user_confirmation", "checks", "manifest_path"), ("delegated_ui_evidence", "listening_evidence", "mirrored_copy_evidence"), label="phase2 native report")
    require(isinstance(report["user_confirmation"], str) and len(report["user_confirmation"].strip()) >= 10,
            "human_report_required", "Use the actual user's explicit confirmation, never a generated acceptance")
    core.obj(report["checks"], CHECKS, label="phase2 manual checks")
    require(all(v is True for v in report["checks"].values()), "native_test_failed", "Unpassed capabilities remain disabled")
    require_closed()
    manifest_path=Path(report["manifest_path"])
    require(safe_path(manifest_path,exists=True).parent==settings.work_root and
            manifest_path.name.startswith("canaries_phase2") and manifest_path.suffix==".json",
            "invalid_manifest", "Only owned frozen phase2 manifests are accepted")
    manifest = decode(manifest_path.read_bytes())
    if "delegated_ui_evidence" in report:
        ui=report["delegated_ui_evidence"]
        require(isinstance(ui,list) and len(ui)==5 and all(isinstance(x,dict) and x.get("actor")=="delegated_agent" and
                x.get("opened_played_saved_reopened") is True and isinstance(x.get("observations"),str) and len(x["observations"])>=20 for x in ui),
                "native_test_failed", "Delegation does not substitute for recorded actual UI observations")
        require({x["name"] for x in ui}=={d["name"] for d in manifest["drafts"] if not d["helper_only"]},
                "invalid_manifest", "UI evidence must identify these exact five drafts")
        listen=report.get("listening_evidence",{})
        require(listen.get("actor")=="user" and listen.get("passed") is True and isinstance(listen.get("actual_quote"),str) and
                isinstance(listen.get("scope"),str),"human_report_required","Actual user listening confirmation is separate from screenshots")
    require(manifest["code_profile"] == core.profile(settings) and manifest["app_version"] == core.app_version(settings) and
            manifest["format_identity"] == codec.identity(settings), "stale_test", "Code/codec/editor changed; preview new tests")
    for stamp in manifest["protected_files"]:
        check_stamp(stamp)
    for info in manifest["protected_media"]:
        check_stamp(info["stamp"])
        require(core.probe(info["stamp"]["path"]) == info, "stale_input", "Original media probe changed")
    check_stamp(manifest["config_stamp"])
    old_catalog = decode(Path(manifest["catalog_before_path"]).read_bytes())
    current = core.load_catalog(settings)[2]["all_draft_store"]
    for row in old_catalog["all_draft_store"]:
        require(sum(r == row for r in current) == 1, "catalog_boundary_violation", "An original catalog entry changed or disappeared")
    saved, resources = [], {}
    manual = manifest["manual_change"]
    for item in manifest["drafts"]:
        if item["helper_only"]:
            continue  # Its sole owned synthetic fixture was deliberately relocated.
        source = core.read_source(settings, item["name"])
        require(source["format"] == "nested-single-11.5", "native_save_required", "Test must be saved/reopened in the pinned native editor")
        plan = core.load_plan(settings, item["plan_id"])
        expected = decode((core.job_path(settings, item["plan_id"]) / "expected_doc.json").read_bytes())
        require(digest(canonical(expected)) == plan["expected_doc_sha256"], "payload_changed", "Test expectation changed")
        if item["feature"] == "format":
            match = next(s for _, s in core.segments(expected) if s["id"] == manual["segment_id"])
            match["volume"] = manual["volume"]
            actual = next(s for _, s in core.segments(source["doc"]) if s["id"] == manual["segment_id"])
            require(abs(actual.get("volume", 1) - manual["volume"]) < 1e-8, "latest_state_not_confirmed", "Latest native dB change is absent")
            # Native UI records its last nonzero volume for future mute toggles;
            # allow only a value equal to the explicitly requested test change.
            if "last_nonzero_volume" in actual:
                require(actual["last_nonzero_volume"] == manual["volume"], "native_semantics_changed", "Unexpected saved volume memory")
                match["last_nonzero_volume"] = manual["volume"]
        compared = verification.compare_saved(expected, source["doc"], settings=settings)
        if item["feature"] == "music_and_transition":
            rid = manifest["transition_resource_id"]
            rows = [m for m in source["doc"]["materials"].get("transitions", []) if m.get("resource_id") == rid]
            require(len(rows) == 1 and any(rows[0]["id"] in s.get("extra_material_refs", []) for _, s in core.segments(source["doc"])),
                    "broken_reference", "Saved flash transition missing or unlinked")
            resources[rid] = copy.deepcopy(rows[0])
        saved.append({"name": item["name"], "feature": item["feature"], "plan_id": item["plan_id"],
                      "content_source": source["content_name"], "fingerprint": source["fingerprint"], "files": source["files"],
                      "semantic_verification": compared, "stats": core.validate_doc(source["doc"], source["media"])})
    # Retain the previously accepted dissolve resource only from its currently
    # readable native test, with an actual cache dependency (no enum-only proof).
    old_tests = decode((settings.work_root / "canaries.json").read_bytes())
    native_b = core.read_source(settings, old_tests["drafts"][1]["name"])
    for row in native_b["doc"]["materials"].get("transitions", []):
        if row.get("resource_id") == "6724845717472416269":
            resources[row["resource_id"]] = copy.deepcopy(row)
    require(len(saved) == 5, "invalid_manifest", "Five native tests are required")
    mirrored = None
    if "mirrored_copy_evidence" in report:
        ui = report["mirrored_copy_evidence"]
        core.obj(ui, ("name", "plan_id", "actor", "opened_played_saved_reopened", "observations"), label="dual-file native check")
        require(ui["actor"] in {"user", "delegated_agent"} and ui["opened_played_saved_reopened"] is True and
                isinstance(ui["observations"], str) and len(ui["observations"]) >= 20,
                "native_test_failed", "Record actual dual-file copy observations, not a generated pass")
        mirror_plan = core.load_plan(settings, ui["plan_id"])
        require(mirror_plan["code_profile"] == manifest["code_profile"] and mirror_plan["canary"] and
                mirror_plan["format"] == "flat-mirrored-360000" and mirror_plan["request"]["name"] == ui["name"],
                "stale_test", "Dual-file native test must use this exact tool version and copy plan")
        core.validate_payload(mirror_plan, settings, core.job_path(settings, ui["plan_id"]) / "payload")
        readback = core.verify_draft(settings, ui["name"], ui["plan_id"], validation="after_save")
        actual = core.read_source(settings, ui["name"])
        require(actual["format"] == "nested-single-11.5", "native_save_required", "Dual-file copy has not been natively saved")
        mirrored = {"native_validation": "passed", "ui": ui, "readback": readback,
                    "fingerprint": actual["fingerprint"], "files": actual["files"]}
    evidence = {"schema": core.SCHEMA, "plan_version": 2, "status": "user_accepted",
                "code_profile": manifest["code_profile"], "format_identity": manifest["format_identity"],
                "app_version": manifest["app_version"], "features": {k: True for k in core.REQUIRED_FEATURES},
                "transition_resources": resources, "caption_presets": manifest["caption_presets"],
                "human_report": report, "saved_drafts": saved, "originals_unchanged": True,
                "acceptance_method":"delegated_native_ui_and_explicit_user_listening" if "delegated_ui_evidence" in report else "explicit_user_native_checks",
                "test_manifest_path":str(manifest_path),
                "latest_manual_change_read_back": manual, "recorded_us": time.time_ns() // 1000,
                "scope": "local pinned single-timeline 11.5 plus legacy plain 360000; no arbitrary codec endpoint"}
    if mirrored is not None:
        evidence["mirrored_plaintext"] = mirrored
    path = settings.work_root / "native_acceptance.json"
    if path.exists():
        previous = decode(path.read_bytes())
        require(previous == evidence or core.accepted(settings) is not None, "acceptance_conflict", "Retain historical gate; no silent replacement")
        return {"status": "user_accepted", "path": str(path), "idempotent_replay": True}
    write_new(path, canonical(evidence))
    return {"status": "user_accepted", "path": str(path), "features": evidence["features"], "native_tests": len(saved)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_file", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    settings = load_settings(args.config)
    try:
        value = record(settings, decode(args.report_file.read_bytes()))
    except Exception as exc:
        path = settings.work_root / ("native_phase2_failure_" + str(time.time_ns()) + ".json")
        write_new(path, canonical({"status": "not_accepted", "error": str(exc), "code": getattr(exc, "code", "unexpected_error")}))
        raise
    sys.stdout.buffer.write(canonical(value) + b"\n")
