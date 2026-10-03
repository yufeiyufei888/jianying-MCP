"""Reference-only planning and checked copy-on-write, never an editor controller.

The native index has no compare-and-swap API. Repeated checks reduce races but
cannot make restarting Jianying during publication safe. Keep it closed.
"""
from __future__ import annotations

import copy
import math
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from . import VERSION, backend, codec, dependencies as deps_io, formats, music, patching, subtitles
from .runtime import (Settings, ToolError, app_pids, atomic_write, canonical,
                      check_stamp, decode, digest, file_stamp, integer, leaf,
                      probe, profile, require, require_closed, safe_path,
                      write_new, writer_lock)

SCHEMA = 360000
MAX_JSON = 32 * 1024 * 1024
CONTENT_NAMES = {"draft_info.json", "draft_content.json"}
AUX_NAMES = {"draft_meta_info.json", "draft_settings", "key_value.json"}
IDENTITY_KEYS = {"id", "name", "create_time", "update_time"}
REQUIRED_FEATURES = {"latest_saved_format", "local_main_edits", "bgm_loops_and_joins", "srt_timeline_and_original", "safe_relink"}


def obj(value, required=(), optional=(), label="object"):
    require(isinstance(value, dict), "invalid_input", f"{label} must be an object")
    require(set(required) <= value.keys() and not value.keys() - set(required) - set(optional),
            "invalid_input", f"Unexpected or missing fields in {label}")
    return value


def array(value, label, minimum=0, maximum=10000):
    require(isinstance(value, list) and minimum <= len(value) <= maximum,
            "invalid_input", f"{label} must be a list of {minimum}..{maximum} items")
    return value


def number(value, label, low=0, high=4):
    require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
            "invalid_input", f"{label} must be finite and between {low} and {high}")
    return value


def small(path):
    require(path.stat().st_size <= MAX_JSON, "unsupported_structure", "Draft metadata exceeds the size limit")
    return path.read_bytes()


def draft_path(settings, name):
    return safe_path(settings.drafts_root / leaf(name), exists=True)


def files_of(folder):
    entries = sorted(folder.iterdir(), key=lambda p: p.name)
    require(all(safe_path(p).is_file() for p in entries), "unsupported_structure",
            "Nested draft assets/cache/timeline layout is unsupported; no copying or guessing")
    names = {p.name for p in entries}
    content = names & CONTENT_NAMES
    require(len(content) == 1 and names - CONTENT_NAMES <= AUX_NAMES and
            "draft_meta_info.json" in names, "unsupported_structure",
            "Only a flat, single-content-file native draft is supported")
    return entries, next(iter(content))


def walk_paths(value, trail=()):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and child and (
                    "path" in key.lower() or (len(child) > 2 and child[1:3] in (":\\", ":/"))):
                yield trail + (key,), child
            else:
                yield from walk_paths(child, trail + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_paths(child, trail + (index,))


def resource_map(doc):
    materials = doc.get("materials")
    require(isinstance(materials, dict), "unsupported_structure", "Missing materials map")
    resources = {}
    for kind, values in materials.items():
        require(isinstance(values, list), "unsupported_structure", f"Unknown materials shape: {kind}")
        for row in values:
            require(isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"],
                    "unsupported_structure", f"Invalid resource identity in {kind}")
            if row["id"] in resources:
                # Pinned native saves repeat an identical local audio material
                # once per use. Preserve every raw row; only the lookup shares
                # its identity. Conflicting rows/categories remain an error.
                previous = resources[row["id"]]
                require(doc.get("last_modified_platform", {}).get("app_version") == "11.5.0" and
                        kind == "audios" and row.get("type") == "extract_music" and
                        previous[0] == kind and canonical(previous[1]) == canonical(row),
                        "duplicate_id", "Conflicting or unverified duplicate material identity")
                continue
            resources[row["id"]] = (kind, row)
    return resources


def segments(doc):
    for track in doc["tracks"]:
        for segment in track["segments"]:
            yield track, segment


def time_range(value, label):
    require(isinstance(value, dict) and "duration" in value,
            "unsupported_structure", f"Missing range: {label}")
    return integer(value.get("start", 0), label + ".start"), integer(value["duration"], label + ".duration", 1)


def validate_doc(doc, media=None):
    require(isinstance(doc, dict) and doc.get("version") == SCHEMA,
            "unsupported_structure", "Only explicitly tested plain draft schema 360000 is supported")
    integer(doc.get("duration"), "draft.duration", 1)
    canvas = doc.get("canvas_config", {})
    integer(canvas.get("width"), "canvas.width", 1)
    integer(canvas.get("height"), "canvas.height", 1)
    number(doc.get("fps", 30), "fps", 1, 240)
    require(isinstance(doc.get("id"), str) and isinstance(doc.get("name"), str),
            "unsupported_structure", "Missing draft identity")
    resources = resource_map(doc)
    tracks = array(doc.get("tracks"), "tracks", 1)
    identities = {doc["id"], *resources}
    maximum_end = 0
    segment_count = 0
    for track in tracks:
        require(isinstance(track, dict) and track.get("type") in {"video", "audio", "text", "sticker", "effect", "filter", "adjust"},
                "unsupported_structure", "Unknown track type")
        require(isinstance(track.get("id"), str) and track["id"] not in identities,
                "duplicate_id", "Missing or duplicate track identity")
        identities.add(track["id"])
        for seg in array(track.get("segments"), "segments"):
            require(isinstance(seg, dict) and isinstance(seg.get("id"), str) and seg["id"] not in identities,
                    "duplicate_id", "Missing or duplicate segment identity")
            identities.add(seg["id"])
            require(seg.get("material_id") in resources, "broken_reference", "Unknown segment material")
            for key in array(seg.get("extra_material_refs", []), "extra_material_refs"):
                require(key in resources, "broken_reference", "Unknown segment auxiliary material")
            start, duration = time_range(seg.get("target_timerange"), "target")
            require(start + duration <= doc["duration"], "out_of_range", "Segment exceeds timeline")
            for group in array(seg.get("common_keyframes", []), "common_keyframes"):
                require(isinstance(group, dict), "unsupported_structure", "Unknown keyframe group")
                for point in array(group.get("keyframe_list", []), "keyframe_list"):
                    require(isinstance(point, dict), "unsupported_structure", "Unknown keyframe point")
                    offset = integer(point.get("time_offset", 0), "keyframe.time_offset")
                    require(offset <= duration, "out_of_range", "Keyframe exceeds segment duration")
                    for value in array(point.get("values", []), "keyframe.values"):
                        require(type(value) in (int, float) and math.isfinite(value), "invalid_input", "Invalid keyframe value")
            maximum_end = max(maximum_end, start + duration)
            kind, mat = resources[seg["material_id"]]
            if kind in {"videos", "audios"}:
                number(seg.get("speed", 1), "speed", 0.01, 100)
                s_start, s_duration = time_range(seg.get("source_timerange"), "source")
                integer(mat.get("duration"), "material.duration", 1)
                require(s_start + s_duration <= mat["duration"], "out_of_range", "Source range exceeds material")
                if media is not None:
                    measured = media[str(safe_path(mat["path"]))]["duration_us"]
                    require(s_start + s_duration <= measured, "out_of_range", "Source range exceeds measured full media")
            segment_count += 1
    require(maximum_end == doc["duration"], "unsupported_structure", "Timeline duration does not match its segments")
    return {"schema": SCHEMA, "tracks": len(tracks), "segments": segment_count,
            "duration_us": doc["duration"], "resources": len(resources)}


def dependency_inventory(doc, meta, folder):
    media, seen, others = {}, set(), []
    for kind in ("videos", "audios"):
        for mat in doc["materials"].get(kind, []):
            path = safe_path(mat.get("path", ""), exists=True)
            require(not path.is_relative_to(folder) and not path.is_relative_to(folder.parent),
                    "internal_dependency", "Media inside the native draft root cannot be retained without guessing/copying")
            info = probe(path)
            require(info["video"] if kind == "videos" else info["audio"], "invalid_media", "Material stream is missing")
            # Preserve original metadata, but reject disagreement large enough to conceal a trimmed proxy.
            require(abs(mat.get("duration", 0) - info["duration_us"]) <= 100_000,
                    "unsupported_structure", "Material does not represent the measured complete original")
            media[str(path)] = info
            seen.add(str(path))
    for data, is_meta in ((doc, False), (meta, True)):
        for trail, value in walk_paths(data):
            if is_meta and trail in {("draft_fold_path",), ("draft_root_path",)}:
                continue
            path = safe_path(value, exists=True)
            require(path.is_file() and not path.is_relative_to(folder.parent), "internal_dependency",
                    "Unknown internal or directory dependency cannot be copied safely")
            if str(path) not in seen:
                # A cover can remain an external small reference. Other unknown file dependencies are not assumed safe.
                require(trail[-1] in {"static_cover_image_path", "draft_cover", "cover_path"},
                        "unknown_dependency", f"Unvalidated dependency field: {trail}")
                others.append(file_stamp(path, hash_bytes=True))
                seen.add(str(path))
    return media, others


def read_source(settings, name, allow_missing=False):
    if (draft_path(settings, name) / "Timelines" / "project.json").exists() or allow_missing:
        source = formats.read_layout(settings, name)
        doc, meta, folder = source["doc"], source["meta"], source["folder"]
        validate_doc(doc)
        require(meta.get("draft_id") == doc["id"] and meta.get("draft_name") == name and
                doc.get("name") in {"", name} and meta.get("tm_duration") == doc["duration"],
                "unsupported_structure", "Draft saved identity/duration conflicts")
        require(safe_path(meta.get("draft_fold_path", "")) == folder and safe_path(meta.get("draft_root_path", "")) == settings.drafts_root,
                "unsupported_structure", "Draft metadata points to another folder/root")
        media, deps, missing = deps_io.inventory(doc, meta, folder, settings, allow_missing=allow_missing, probe_fn=probe)
        if not missing:
            validate_doc(doc, media)
        source.update(media=media, dependencies=deps, missing=missing)
        source["fingerprint"] = formats.fingerprint(source)
        return source
    folder = draft_path(settings, name)
    paths, content_name = files_of(folder)
    raw = {p.name: small(p) for p in paths}
    doc = decode(raw[content_name])
    meta = decode(raw["draft_meta_info.json"])
    validate_doc(doc)
    require(meta.get("draft_id") == doc["id"] and meta.get("draft_name") == doc["name"] == name and
            meta.get("tm_duration") == doc["duration"], "unsupported_structure", "Draft identity or duration metadata conflicts")
    require(safe_path(meta.get("draft_fold_path", "")) == folder and
            safe_path(meta.get("draft_root_path", "")) == settings.drafts_root,
            "unsupported_structure", "Draft metadata points to a different folder/root")
    if "key_value.json" in raw:
        require(not list(walk_paths(decode(raw["key_value.json"]))), "unknown_dependency", "Auxiliary JSON has path dependencies")
    if "draft_settings" in raw:
        text = raw["draft_settings"].decode("utf-8-sig")
        require(text.startswith("[General]") and ":\\" not in text and ":/" not in text,
                "unsupported_structure", "Unknown draft_settings format/dependency")
    media, dependencies = dependency_inventory(doc, meta, folder)
    validate_doc(doc, media)
    result = {"folder": folder, "content_name": content_name, "raw": raw, "doc": doc, "meta": meta,
              "files": [file_stamp(p, hash_bytes=True) for p in paths], "media": media, "dependencies": dependencies,
              "format": "flat-360000", "project": None, "timeline_id": None, "encoded": {}, "copy_resources": {},
              "resource_copy_bytes": 0, "ignored": [], "format_identity": None, "missing": [],
              "source_tree": formats.tree(folder)}
    result["fingerprint"] = formats.fingerprint(result)
    return result


def load_catalog(settings):
    path = safe_path(settings.drafts_root / "root_meta_info.json", exists=True)
    data = small(path)
    doc = decode(data)
    require(isinstance(doc, dict) and isinstance(doc.get("all_draft_store"), list) and
            type(doc.get("draft_ids")) is int and doc["draft_ids"] >= 0,
            "unsupported_catalog", "Unsupported native draft directory index")
    require(all(isinstance(row, dict) for row in doc["all_draft_store"]), "unsupported_catalog", "Invalid catalog row")
    return path, data, doc


def no_collision(settings, name, draft_id=None):
    require(not (settings.drafts_root / name).exists(), "target_exists", "Target already exists; overwrite is forbidden")
    _, _, catalog = load_catalog(settings)
    for row in catalog["all_draft_store"]:
        require(row.get("draft_name", "").casefold() != name.casefold() and
                row.get("draft_id") != draft_id and
                (not row.get("draft_fold_path") or safe_path(row["draft_fold_path"]) != settings.drafts_root / name),
                "catalog_conflict", "Target identity already occurs in the catalog")


def normalize(request, settings):
    obj(request, ("request_id", "mode", "name"), ("canvas", "clips", "source_draft", "bgm", "transitions", "operations", "relinks", "subtitles", "audio_analysis"), "request")
    try:
        rid = str(uuid.UUID(request["request_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError("invalid_input", "request_id must be a UUID") from exc
    value = copy.deepcopy(request)
    value.update(request_id=rid, name=leaf(request["name"]))
    require(value["mode"] in {"create", "enrich", "patch"}, "invalid_input", "mode must be create, enrich or patch")
    if value["mode"] == "create":
        require("source_draft" not in value and "clips" in value and "canvas" in value,
                "invalid_input", "New draft requires explicit canvas and reviewed clips, no source_draft")
        canvas = obj(value["canvas"], ("width", "height", "fps"), label="canvas")
        integer(canvas["width"], "width", 1); integer(canvas["height"], "height", 1)
        require(canvas["width"] <= 7680 and canvas["height"] <= 7680, "invalid_input", "Canvas exceeds supported range")
        integer(canvas["fps"], "fps", 1)
        require(canvas["fps"] <= 120, "invalid_input", "FPS exceeds supported range")
        for clip in array(value["clips"], "clips", 1):
            obj(clip, ("path", "in_us", "out_us"), ("crop", "volume", "transform"), "clip")
            clip["path"] = str(safe_path(clip["path"], exists=True))
            require(Path(clip["path"]).suffix.lower() in {".mp4", ".mov"}, "invalid_media", "Only full MP4/MOV originals are supported")
            require(not Path(clip["path"]).is_relative_to(settings.drafts_root), "internal_dependency", "Use originals outside the draft root")
            info = probe(clip["path"])
            integer(clip["in_us"], "in_us"); integer(clip["out_us"], "out_us", 1)
            require(info["video"] and clip["in_us"] < clip["out_us"] <= info["duration_us"], "out_of_range", "Clip exceeds measured complete original")
            number(clip.setdefault("volume", 1), "volume")
            crop = clip.setdefault("crop", {})
            allowed = {f"{corner}_{axis}" for corner in ("upper_left", "upper_right", "lower_left", "lower_right") for axis in ("x", "y")}
            obj(crop, (), allowed, "crop")
            for v in crop.values(): number(v, "crop", 0, 1)
            defaults = {f"{corner}_{axis}": int((axis == "x" and "right" in corner) or (axis == "y" and "lower" in corner)) for corner in ("upper_left", "upper_right", "lower_left", "lower_right") for axis in ("x", "y")}
            defaults.update(crop)
            require(defaults["upper_left_y"] == defaults["upper_right_y"] and defaults["lower_left_y"] == defaults["lower_right_y"] and
                    defaults["upper_left_x"] == defaults["lower_left_x"] and defaults["upper_right_x"] == defaults["lower_right_x"] and
                    defaults["upper_left_x"] < defaults["upper_right_x"] and defaults["upper_left_y"] < defaults["lower_left_y"],
                    "invalid_input", "Only a nonempty rectangular normalized crop is supported")
            transform = clip.setdefault("transform", {})
            allowed = {"alpha", "flip_horizontal", "flip_vertical", "rotation", "scale_x", "scale_y", "transform_x", "transform_y"}
            obj(transform, (), allowed, "transform")
            for k, v in transform.items():
                if k.startswith("flip_"): require(type(v) is bool, "invalid_input", "Flip must be boolean")
                elif k == "alpha": number(v, k, 0, 1)
                elif k.startswith("scale"): number(v, k, 0.01, 10)
                else: number(v, k, -360 if k == "rotation" else -5, 360 if k == "rotation" else 5)
        require(not value.get("transitions"), "invalid_input", "New draft transitions require saved segment IDs; use a later enrich request")
    else:
        require("source_draft" in value and "canvas" not in value and "clips" not in value,
                "invalid_input", "enrich forbids rebuilding, reordering, retiming or inserting main-track clips")
        value["source_draft"] = leaf(value["source_draft"])
        require(value["source_draft"].casefold() != value["name"].casefold(), "invalid_input", "Copy name must differ from source")
    if value["mode"] != "patch":
        require(not any(k in value for k in ("operations", "relinks")) and (value["mode"] == "create" or "subtitles" not in value),
                "invalid_input", "Existing local edits/relink/captions require explicit patch mode")
    value.setdefault("operations", [])
    value.setdefault("relinks", {})
    value.setdefault("subtitles", [])
    value.setdefault("audio_analysis", [])
    value.setdefault("bgm", [])
    value.setdefault("transitions", [])
    require(value["mode"] == "create" or any(value[k] for k in ("bgm", "transitions", "operations", "relinks", "subtitles")), "invalid_input", "No change requested")
    array(value["bgm"], "bgm", maximum=100)
    array(value["transitions"], "transitions", maximum=1000)
    array(value["operations"], "operations", maximum=1000)
    array(value["subtitles"], "subtitles", maximum=1000)
    array(value["audio_analysis"], "audio_analysis", maximum=100)
    return value


def normalize_music(items, duration, settings):
    for item in items:
        obj(item, ("path", "start_us", "duration_us", "volume"), ("in_us", "fade_in_us", "fade_out_us", "ducking", "loop", "loop_out_us", "loop_crossfade", "loop_crossfade_us", "crossfade_previous_us"), "bgm")
        item["path"] = str(safe_path(item["path"], exists=True))
        require(not Path(item["path"]).is_relative_to(settings.drafts_root), "internal_dependency", "BGM must be an external original file")
        info = probe(item["path"])
        integer(item["start_us"], "start_us"); integer(item["duration_us"], "duration_us", 1)
        integer(item.setdefault("in_us", 0), "in_us")
        require(type(item.get("loop", False)) is bool and type(item.get("loop_crossfade", False)) is bool, "invalid_input", "Loop/join flags must be boolean")
        require(info["audio"] and item["start_us"] + item["duration_us"] <= duration and
                (item.get("loop") or item["in_us"] + item["duration_us"] <= info["duration_us"]), "out_of_range", "BGM exceeds audio/timeline; looping requires explicit flag")
        if item.get("loop"):
            integer(item.setdefault("loop_out_us", info["duration_us"]), "loop_out_us", 1)
            require(item["in_us"] < item["loop_out_us"] <= info["duration_us"], "out_of_range", "Invalid loop source range")
            integer(item.setdefault("loop_crossfade_us", 250000 if item.get("loop_crossfade") else 0), "loop_crossfade_us")
            require(item["loop_crossfade_us"] * 2 < item["loop_out_us"]-item["in_us"], "out_of_range", "Loop crossfade exceeds cycle")
        else:
            require(not item.get("loop_crossfade") and not item.get("loop_crossfade_us") and "loop_out_us" not in item, "invalid_input", "Loop options require loop=true")
        integer(item.get("crossfade_previous_us", 0), "crossfade_previous_us")
        number(item["volume"], "volume")
        for key in ("fade_in_us", "fade_out_us"): integer(item.setdefault(key, 0), key)
        require(item["fade_in_us"] + item["fade_out_us"] <= item["duration_us"], "out_of_range", "Fades overlap")
        windows = array(item.setdefault("ducking", []), "ducking", maximum=10000)
        previous_end = -1
        for window in windows:
            obj(window, ("start_us", "end_us", "volume", "attack_us", "release_us"), label="ducking")
            for key in ("start_us", "end_us"): integer(window[key], key)
            for key in ("attack_us", "release_us"): integer(window[key], key, 1)
            number(window["volume"], "ducking.volume", 0, item["volume"])
            attack_start = window["start_us"] - window["attack_us"]
            release_end = window["end_us"] + window["release_us"]
            require(item["start_us"] <= attack_start <= window["start_us"] < window["end_us"] <= release_end <= item["start_us"] + item["duration_us"] and
                    attack_start > previous_end, "out_of_range", "Ducking windows including attack/release must be ordered, disjoint and within BGM")
            previous_end = release_end
    if any(i.get("loop") or i.get("crossfade_previous_us") for i in items):
        music.schedule(items)


def accepted(settings):
    path = settings.work_root / "native_acceptance.json"
    if not path.exists(): return None
    value = decode(small(safe_path(path, exists=True)))
    if value.get("status") != "user_accepted" or value.get("code_profile") != profile(settings) or value.get("schema") != SCHEMA or value.get("plan_version") != 2:
        return None
    if any(value.get("features", {}).get(k) is not True for k in REQUIRED_FEATURES):
        return None
    if value.get("format_identity") != codec.identity(settings):
        return None
    return value


def require_acceptance(settings):
    if not settings.canary:
        evidence = accepted(settings)
        require(evidence is not None, "native_acceptance_required", "All five phase2 native checks must pass before production writes/MCP registration")
        require(evidence.get("app_version") == app_version(settings), "native_acceptance_required", "Jianying version changed; native acceptance must be repeated")


def app_version(settings=None):
    root = (settings or Settings()).app_root
    return root.name if (root / "JianyingPro.exe").is_file() else "unknown"


def add_transitions(doc, items, settings, source):
    mapping = {seg["id"]: (track, index, seg) for track in doc["tracks"] for index, seg in enumerate(track["segments"])}
    resources = resource_map(doc)
    changed = []
    used = set()
    for item in items:
        obj(item, ("after_segment_id", "before_segment_id", "resource_id", "duration_us"), label="transition")
        integer(item["duration_us"], "transition.duration_us", 1)
        require(all(isinstance(item[k], str) for k in ("after_segment_id", "before_segment_id", "resource_id")), "invalid_input", "Transition identities must be strings")
        require(item["after_segment_id"] in mapping and item["before_segment_id"] in mapping,
                "missing_segment", "Transition requires explicit existing before/after segment IDs")
        track, index, left = mapping[item["after_segment_id"]]
        other, next_index, right = mapping[item["before_segment_id"]]
        require(track is other and track["type"] == "video" and next_index == index + 1,
                "invalid_cut", "Transition segments must be adjacent on the same video track")
        start, length = time_range(left["target_timerange"], "left")
        rstart, rlength = time_range(right["target_timerange"], "right")
        require(start + length == rstart and item["duration_us"] <= min(length, rlength),
                "invalid_cut", "Transition does not fit an adjacent cut")
        require(left["id"] not in used and not any(resources[r][0] == "transitions" for r in left.get("extra_material_refs", [])),
                "transition_conflict", "This cut already has a transition")
        used.add(left["id"])
        require(left.get("speed", 1) == right.get("speed", 1) == 1 and not left.get("reverse") and not right.get("reverse"),
                "invalid_cut", "Transition handles with reverse/retiming are unsupported")
        lmat = resources[left["material_id"]][1]
        half = (item["duration_us"] + 1) // 2
        lin, llen = time_range(left["source_timerange"], "left.source")
        rin, _ = time_range(right["source_timerange"], "right.source")
        require(lmat["duration"] - lin - llen >= half and rin >= half,
                "insufficient_handles", "Need unused original frames after left clip and before right clip; no automatic retiming")
        if not settings.canary:
            evidence = accepted(settings)
            require(evidence and item["resource_id"] in evidence.get("transition_resources", {}),
                    "unverified_transition", "This transition resource has not passed native acceptance")
        requested_duration = item["duration_us"]
        # Native centred transitions use an even number of timeline frames.
        # Preview the effective value instead of tolerating a later edit.
        fps = doc.get("fps", 30)
        frames = math.floor(requested_duration*fps/1_000_000 + .5)
        effective_duration = math.floor((frames//2)*2*1_000_000/fps)
        require(effective_duration > 0, "invalid_cut", "Transition is shorter than two native frames")
        item["duration_us"] = effective_duration
        payload = backend.transition_payload(item, settings, source)
        doc["materials"].setdefault("transitions", []).append(payload)
        left.setdefault("extra_material_refs", []).append(payload["id"])
        changed.append({"segment_id": left["id"], "resource": payload,
                        "requested_duration_us": requested_duration, "effective_duration_us": effective_duration})
    return changed


def prove_boundary(base, output, delta):
    if delta.get("version") == 2:
        return prove_patch_boundary(base, output, delta)
    restored = copy.deepcopy(output)
    for key in IDENTITY_KEYS:
        if key in base: restored[key] = copy.deepcopy(base[key])
        else: restored.pop(key, None)
    new_tracks = set(delta["new_track_ids"])
    require([t["id"] for t in restored["tracks"] if t["id"] not in new_tracks] == [t["id"] for t in base["tracks"]],
            "boundary_violation", "Original track order/identities changed")
    restored["tracks"] = [t for t in restored["tracks"] if t["id"] not in new_tracks]
    additions = delta["materials"]
    for kind, rows in additions.items():
        current = restored["materials"].get(kind, [])
        original = base["materials"].get(kind, [])
        require(current == original + rows, "boundary_violation", "Material edit is not an exact append")
        if kind in base["materials"]: restored["materials"][kind] = copy.deepcopy(original)
        else: restored["materials"].pop(kind, None)
    mapping = {s["id"]: s for _, s in segments(restored)}
    for item in delta["transitions"]:
        refs = mapping[item["segment_id"]]["extra_material_refs"]
        require(refs[-1] == item["resource"]["id"], "boundary_violation", "Transition reference is not an append")
        refs.pop()
    require(restored == base, "boundary_violation", "Unrequested change in original tracks, fields or resources")
    return True


def prove_patch_boundary(base, output, delta):
    restored = copy.deepcopy(output)
    for key in IDENTITY_KEYS | {"duration", "path"}:
        if key in base: restored[key] = copy.deepcopy(base[key])
        else: restored.pop(key, None)
    new_tracks = set(delta["new_track_ids"])
    require([t["id"] for t in restored["tracks"] if t["id"] not in new_tracks] == [t["id"] for t in base["tracks"]],
            "boundary_violation", "Original track identities/order changed")
    restored["tracks"] = [t for t in restored["tracks"] if t["id"] not in new_tracks]
    allowed_ranges = delta["segment_ranges"]
    new_segments = set(delta["inserted_segment_ids"])
    transitions = {x["segment_id"]: x["resource"]["id"] for x in delta["transitions"]}
    for original_track, track in zip(base["tracks"], restored["tracks"]):
        require({k:v for k,v in track.items() if k != "segments"} == {k:v for k,v in original_track.items() if k != "segments"},
                "boundary_violation", "Original track configuration changed")
        current = {s["id"]: s for s in track["segments"] if s["id"] not in new_segments}
        require(set(current) == {s["id"] for s in original_track["segments"]}, "boundary_violation", "Original segments removed or extra segments added")
        for old in original_track["segments"]:
            s = current[old["id"]]
            allowed = allowed_ranges.get(old["id"])
            if allowed:
                require(s.get("source_timerange") == allowed["source"] and s["target_timerange"] == allowed["target"],
                        "boundary_violation", "Local clip range differs from preview")
                for k in ("source_timerange", "target_timerange"):
                    if k in old: s[k] = copy.deepcopy(old[k])
                    else: s.pop(k, None)
            if old["id"] in transitions:
                refs = s.get("extra_material_refs", [])
                require(refs and refs[-1] == transitions[old["id"]], "boundary_violation", "Transition is not the explicit appended reference")
                refs.pop()
            require(s == old, "boundary_violation", "Unrequested original clip/effect/keyframe/unknown-field change")
        track["segments"] = [current[s["id"]] for s in original_track["segments"]]
    for kind in restored["materials"]:
        current = restored["materials"][kind]
        additions = delta["materials"].get(kind, [])
        require(not additions or current[-len(additions):] == additions, "boundary_violation", "New resource differs from preview")
        if additions: current = current[:-len(additions)]
        original = base["materials"].get(kind, [])
        for update in delta["relinks"]:
            for m in current:
                if m["id"] in update["material_ids"]:
                    require(m["path"] == update["new_path"], "boundary_violation", "Relink differs from explicit mapping")
                    m["path"] = next(x["path"] for x in original if x["id"] == m["id"])
        require(current == original, "boundary_violation", "Original resource configuration changed")
        restored["materials"][kind] = current
    for kind in list(restored["materials"]):
        if kind not in base["materials"]: restored["materials"].pop(kind)
    require(restored == base, "boundary_violation", "Unrequested JSON fields changed")
    return True


def new_meta(settings, doc, folder, source_meta=None):
    meta = copy.deepcopy(source_meta) if source_meta else decode(small(settings.vendor / "assets" / "draft_meta_info.json"))
    meta.update(draft_name=doc["name"], draft_id=doc["id"], draft_fold_path=str(folder),
                draft_root_path=str(settings.drafts_root), tm_duration=doc["duration"],
                tm_draft_create=time.time_ns() // 1000, tm_draft_modified=time.time_ns() // 1000)
    return meta


def uuid_id(value):
    try: return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc: raise ToolError("invalid_input", "Invalid plan UUID") from exc


def job_path(settings, plan_id):
    return safe_path(settings.work_root / "jobs" / uuid_id(plan_id))


def public_plan(plan, receipt=None):
    result = {"plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"], "request_sha256": plan["request_sha256"],
              "preview": plan["preview"], "editor_acceptance": "pending"}
    if receipt: result["receipt"] = receipt
    return result


def plan_draft(settings, request):
    settings.validate()
    require_closed()
    normalized = normalize(request, settings)
    job = job_path(settings, normalized["request_id"])
    with writer_lock(settings):
        require_closed()
        if (job / "plan.json").exists():
            existing = load_plan(settings, normalized["request_id"])
            # Normalized audio defaults are added later. Compare with the same normalized original request instead.
            require(existing["request_initial_sha256"] == digest(canonical(normalized)), "request_id_conflict", "Same request_id was used with a different request")
            receipt = decode(small(job / "receipt.json")) if (job / "receipt.json").exists() else None
            return public_plan(existing, receipt)
        require(not job.exists(), "incomplete_job", "An incomplete preview already owns this request_id; inspect it, do not overwrite")
        initial_hash = digest(canonical(normalized))
        no_collision(settings, normalized["name"])
        catalog_path, catalog_bytes, _ = load_catalog(settings)
        source = read_source(settings, normalized["source_draft"], allow_missing=bool(normalized["relinks"])) if normalized["mode"] != "create" else None
        base = source["doc"] if source else None
        doc = copy.deepcopy(base) if base else backend.build(normalized, settings)
        source_meta = copy.deepcopy(source["meta"]) if source else None
        relinks = deps_io.relink(doc, source_meta, normalized["relinks"], settings) if normalized["relinks"] else []
        local_changes = patching.apply(doc, normalized["operations"], settings)
        caption_report = subtitles.map_cues(doc, normalized["subtitles"])
        original_materials = copy.deepcopy(base["materials"]) if base else copy.deepcopy(doc["materials"])
        new_tracks = subtitles.add(doc, caption_report["mapped"], settings)
        normalize_music(normalized["bgm"], doc["duration"], settings)
        music_tracks = backend.add_music(doc, normalized["bgm"], settings)
        new_tracks += music_tracks
        changes = add_transitions(doc, normalized["transitions"], settings, base)
        identity = str(uuid.uuid4()).upper()
        doc.update(id=identity, name=normalized["name"], create_time=time.time_ns() // 1_000_000, update_time=time.time_ns() // 1_000_000)
        delta = {"version": 2, "new_track_ids": new_tracks, "transitions": changes, "relinks": relinks,
                 "inserted_segment_ids": [s["id"] for change in local_changes for s in change["segments"] if s["before"] is None],
                 "segment_ranges": {s["id"]: {"source": copy.deepcopy(s.get("source_timerange")), "target": copy.deepcopy(s["target_timerange"])}
                                    for _,s in segments(doc) if base and s["id"] in {x["id"] for _,x in segments(base)}},
                 "materials": {k: v[len(original_materials.get(k, [])):] for k, v in doc["materials"].items() if len(v) > len(original_materials.get(k, []))}}
        if base: prove_boundary(base, doc, delta)
        target = settings.drafts_root / normalized["name"]
        meta = new_meta(settings, doc, target, source_meta)
        # Internal covers are budgeted/stamped by the layout adapter. During
        # preview they still live in the source, not the uncreated target.
        dependency_meta = copy.deepcopy(meta)
        if source and dependency_meta.get("draft_cover") in {"draft_cover.jpg", "draft_cover.png"}:
            dependency_meta["draft_cover"] = str(source["folder"] / dependency_meta["draft_cover"])
        media, deps, missing = deps_io.inventory(doc, dependency_meta, source["folder"] if source else target,
                                               settings, probe_fn=probe)
        stats = validate_doc(doc, media)
        if source:
            payload, content_name = formats.payload(source, doc, meta, target, settings)
        else:
            content_name = "draft_info.json"
            payload = {content_name: canonical(doc), "draft_meta_info.json": canonical(meta)}
            payload["draft_settings"] = small(settings.vendor / "assets" / "draft_settings_template")
            payload["key_value.json"] = small(settings.vendor / "assets" / "key_value_template.json")
        measurements = music.analyze(settings, normalized["audio_analysis"]) if normalized["audio_analysis"] else []
        plan = {"schema": "jianying-local-plan/2", "plan_id": normalized["request_id"], "request": normalized,
                "request_initial_sha256": initial_hash, "request_sha256": digest(canonical(normalized)),
                "canary": settings.canary, "code_profile": profile(settings), "app_version": app_version(settings),
                "drafts_root": str(settings.drafts_root), "work_root": str(settings.work_root), "target": str(target),
                "content_name": content_name, "draft_id": identity, "created_us": time.time_ns() // 1000,
                "source_files": source["files"] if source else [], "source_directory_names": source["source_tree"] if source else [],
                "source_folder": str(source["folder"]) if source else None,
                "source_media": list(source["media"].values()) if source else [],
                "missing_source_paths": sorted({m["path"] for m in source["missing"]}) if source else [],
                "format": source["format"] if source else "flat-360000", "format_identity": (source["format_identity"] if source and source["format_identity"] else codec.status(settings).get("identity")),
                "source_fingerprint": source["fingerprint"] if source else None,
                "input_files": caption_report["input_files"], "expected_doc_sha256": digest(canonical(doc)),
                "expected_meta_sha256": digest(canonical(meta)),
                "catalog_stamp": file_stamp(catalog_path, hash_bytes=True), "media": list(media.values()), "dependencies": deps,
                "output_sha256": {k: digest(v) for k, v in payload.items()}, "delta": delta,
                "preview": {"mode": normalized["mode"], "name": normalized["name"], "target": str(target), **stats,
                            "source_draft": normalized.get("source_draft"), "added_bgm_tracks": len(music_tracks),
                            "added_transitions": len(changes), "media_copies": 0,
                            "format": source["format"] if source else "flat-360000", "latest_content_source": source["content_name"] if source else None,
                            "local_operations": local_changes, "relinks": [{k:v for k,v in r.items() if k not in {"new_media", "old_media"}} for r in relinks],
                            "subtitles": {k:v for k,v in caption_report.items() if k != "input_files"}, "audio_metrics": measurements,
                            "loop_schedule": music.schedule(normalized["bgm"], fps=doc.get("fps", 30)),
                            "transition_frame_alignment": [{"segment_id": c["segment_id"], "requested_us": c["requested_duration_us"], "effective_us": c["effective_duration_us"]} for c in changes],
                            "resource_copy_bytes": source["resource_copy_bytes"] if source else 0,
                            "omitted_regenerable_files": source["ignored"] if source else [],
                            "speech_detection": "not_performed", "explicit_ducking_intervals": sum(len(i["ducking"]) for i in normalized["bgm"]),
                            "file_bytes": sum(len(v) for v in payload.values()), "native_acceptance_required": not settings.canary and accepted(settings) is None,
                            "segments": [{"id": s["id"], "track_id": t["id"], "material_id": s["material_id"],
                                          "target": s["target_timerange"], "source": s.get("source_timerange")} for t, s in segments(doc)]}}
        plan["plan_sha256"] = digest(canonical(plan))
        require_closed()
        check_stamp(plan["catalog_stamp"])
        for stamp in plan["source_files"]: check_stamp(stamp)
        for info in plan["media"]: check_stamp(info["stamp"])
        for stamp in plan["input_files"]: check_stamp(stamp)
        job.mkdir(parents=True)
        (job / "payload").mkdir()
        if source:
            (job / "source_snapshot").mkdir()
            for k, v in {**source["raw"], **source["copy_resources"]}.items():
                dest = job / "source_snapshot" / k
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_new(dest, v)
            write_new(job / "source_doc.json", canonical(base))
            write_new(job / "source_meta.json", canonical(source["meta"]))
        write_new(job / "expected_doc.json", canonical(doc))
        write_new(job / "expected_meta.json", canonical(meta))
        write_new(job / "catalog_preview.json", catalog_bytes)
        for k, v in payload.items():
            dest = job / "payload" / k
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_new(dest, v)
        write_new(job / "plan.json", canonical(plan))
        return public_plan(plan)


def load_plan(settings, plan_id):
    job = job_path(settings, plan_id)
    plan = decode(small(safe_path(job / "plan.json", exists=True)))
    saved = plan.pop("plan_sha256", None)
    require(saved == digest(canonical(plan)), "invalid_plan", "Plan integrity hash mismatch")
    plan["plan_sha256"] = saved
    require(plan.get("schema") in {"jianying-local-plan/1", "jianying-local-plan/2"} and plan["plan_id"] == uuid_id(plan_id) and
            plan["work_root"] == str(settings.work_root) and plan["drafts_root"] == str(settings.drafts_root) and
            plan["target"] == str(settings.drafts_root / leaf(plan["request"]["name"])),
            "invalid_plan", "Plan does not belong to these fixed roots")
    if plan["schema"] == "jianying-local-plan/1":
        require(set(plan["output_sha256"]) <= CONTENT_NAMES | AUX_NAMES and len(set(plan["output_sha256"]) & CONTENT_NAMES) == 1,
                "invalid_plan", "Unexpected historical payload files")
    else:
        for key in plan["output_sha256"]:
            valid_payload_name(key)
        require(plan["content_name"] in plan["output_sha256"] and "draft_meta_info.json" in plan["output_sha256"], "invalid_plan", "Content/metadata payload is missing")
    return plan


def valid_payload_name(value):
    p = Path(value)
    require(isinstance(value, str) and not p.is_absolute() and not any(x in value for x in ("\\", ":")) and
            ".." not in p.parts and str(p.as_posix()) == value, "invalid_plan", "Unsafe payload path")
    if len(p.parts) == 1:
        allowed = CONTENT_NAMES | AUX_NAMES | formats.ROOT_JSON | {"timeline_layout.json", "draft_cover.jpg"}
        require(value in allowed, "invalid_plan", "Unexpected root payload file")
    elif value == "Timelines/project.json":
        return
    elif p.parts[0] == "common_attachment" and len(p.parts) == 2:
        require(p.name in formats.ATTACHMENTS, "invalid_plan", "Unknown attachment payload")
    else:
        require(len(p.parts) in {3,4} and p.parts[0] == "Timelines", "invalid_plan", "Unknown nested payload")
        uuid_id(p.parts[1])
        require((len(p.parts) == 3 and p.name in formats.TIMELINE_JSON | CONTENT_NAMES | {"draft_cover.jpg"}) or
                (len(p.parts) == 4 and p.parts[2] == "common_attachment" and p.name in formats.ATTACHMENTS),
                "invalid_plan", "Unknown timeline payload")


def check_inputs(plan, settings):
    require(plan["schema"] == "jianying-local-plan/2", "stale_plan", "Historical v1 plans are read-only evidence; create a new preview")
    require(plan["code_profile"] == profile(settings) and plan["app_version"] == app_version(settings), "stale_plan", "Code/backend/editor version changed; preview again with a new request_id")
    check_stamp(plan["catalog_stamp"])
    for stamp in plan["source_files"]: check_stamp(stamp)
    if plan["source_files"]:
        names = formats.tree(Path(plan["source_folder"]))
        require(names == plan["source_directory_names"], "stale_input", "Source draft folder layout changed")
    for info in plan["media"]:
        check_stamp(info["stamp"])
        require(probe(info["stamp"]["path"]) == info, "stale_input", "Media probe result changed")
    for stamp in plan["dependencies"]: deps_io.check_dependency(stamp)
    for stamp in plan.get("input_files", []): check_stamp(stamp)
    if plan.get("format_identity"):
        require(codec.identity(settings) == plan["format_identity"], "stale_plan", "Codec/DLL/editor profile changed")
    for info in plan.get("source_media", []):
        check_stamp(info["stamp"])
    for path in plan.get("missing_source_paths", []):
        require(not Path(path).exists(), "stale_input", "Previously missing original reappeared; refresh preview")


def validate_payload(plan, settings, folder):
    for name, expected in plan["output_sha256"].items():
        require(digest(small(safe_path(folder / name, exists=True))) == expected, "payload_changed", "Planned output bytes changed")
    require(sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == sorted(plan["output_sha256"]), "payload_changed", "Unexpected payload dependency")
    if plan.get("format") == "nested-single-11.5":
        from dataclasses import replace
        layout = formats.read_layout(replace(settings, drafts_root=folder.parent), folder.name)
        doc, meta = layout["doc"], layout["meta"]
    else:
        doc = decode(small(folder / plan["content_name"]))
        meta = decode(small(folder / "draft_meta_info.json"))
    require(digest(canonical(doc)) == plan["expected_doc_sha256"] and digest(canonical(meta)) == plan["expected_meta_sha256"],
            "payload_changed", "Decoded output differs from immutable planned content")
    require(doc["id"] == meta["draft_id"] == plan["draft_id"] and
            doc["name"] == meta["draft_name"] == plan["request"]["name"] and
            meta["tm_duration"] == doc["duration"] and safe_path(meta["draft_fold_path"]) == Path(plan["target"]) and
            safe_path(meta["draft_root_path"]) == settings.drafts_root, "invalid_payload", "Output identity/path metadata conflicts")
    media = {i["stamp"]["path"]: i for i in plan["media"]}
    stats = validate_doc(doc, media)
    if plan["source_files"]:
        snapshot = job_path(settings, plan["plan_id"]) / "source_snapshot"
        for stamp in plan["source_files"]:
            relative = Path(stamp["path"]).relative_to(Path(plan["source_folder"]))
            require(digest(small(snapshot / relative)) == stamp["sha256"], "invalid_plan", "Source snapshot was modified")
        base = decode(small(job_path(settings, plan["plan_id"]) / "source_doc.json"))
        prove_boundary(base, doc, plan["delta"])
        # Metadata and auxiliary files also have an exact, limited edit boundary.
        original_meta = decode(small(job_path(settings, plan["plan_id"]) / "source_meta.json"))
        restored_meta = copy.deepcopy(meta)
        for key in ("draft_name", "draft_id", "draft_fold_path", "draft_root_path", "tm_duration", "tm_draft_create", "tm_draft_modified"):
            if key in original_meta: restored_meta[key] = original_meta[key]
            else: restored_meta.pop(key, None)
        for update in plan["delta"].get("relinks", []):
            for group, old_group in zip(restored_meta.get("draft_materials", []), original_meta.get("draft_materials", [])):
                for row, old_row in zip(group.get("value", []), old_group.get("value", [])):
                    if row.get("file_Path") and safe_path(row["file_Path"]) == Path(update["new_path"]):
                        row["file_Path"] = old_row.get("file_Path")
        require(restored_meta == original_meta, "boundary_violation", "Unrequested metadata edit")
        if plan["format"] == "flat-360000":
            for name in plan["output_sha256"]:
                if name not in {plan["content_name"], "draft_meta_info.json"}:
                    require(small(snapshot / name) == small(folder / name), "boundary_violation", "Auxiliary configuration changed")
    return stats, doc, meta


def catalog_entry(plan, meta):
    folder = Path(plan["target"])
    return {"cloud_draft_cover": False, "cloud_draft_sync": False, "draft_cloud_last_action_download": False,
            "draft_cloud_purchase_info": "", "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
            "draft_cover": meta.get("draft_cover", ""), "draft_fold_path": folder.as_posix(), "draft_id": plan["draft_id"],
            "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False, "draft_is_infinite_canvas_draft": False,
            "draft_is_invisible": False, "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
            "draft_json_file": (folder / plan["content_name"]).as_posix(), "draft_name": plan["request"]["name"],
            "draft_new_version": meta.get("draft_new_version", ""), "draft_root_path": Path(plan["drafts_root"]).as_posix(),
            "draft_timeline_materials_size": 0, "draft_type": "", "draft_web_article_video_enter_from": "",
            "pippit_avatar_url": "", "pippit_extra_info": "", "pippit_id": "", "pippit_user_name": "", "streaming_edit_draft_ready": True,
            "tm_draft_cloud_completed": "", "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
            "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
            "tm_draft_create": meta["tm_draft_create"], "tm_draft_modified": meta["tm_draft_modified"],
            "tm_draft_removed": 0, "tm_duration": meta["tm_duration"]}


def index_delta(before, after, entry):
    require(after.get("all_draft_store") == [entry] + before["all_draft_store"] and
            after.get("draft_ids") == before["draft_ids"] + 1 and
            {k: v for k, v in after.items() if k not in {"all_draft_store", "draft_ids"}} ==
            {k: v for k, v in before.items() if k not in {"all_draft_store", "draft_ids"}},
            "catalog_boundary_violation", "Existing native catalog entries/settings changed")


def apply_plan(settings, plan_id, expected_plan_sha256):
    settings.validate()
    with writer_lock(settings):
        plan = load_plan(settings, plan_id)
        require(plan["plan_sha256"] == expected_plan_sha256, "stale_plan", "Explicit plan hash does not match preview")
        require(not plan["canary"] or settings.canary, "native_acceptance_required", "Canary plan cannot be applied through production/MCP")
        job = job_path(settings, plan_id)
        receipt_path = job / "receipt.json"
        # A completed replay is read-only. It never rebuilds an edited result.
        if receipt_path.exists():
            receipt = decode(small(receipt_path))
            if receipt.get("status") == "registered":
                return {**receipt, "idempotent_replay": True, "current_state_checked": False,
                        "replay_notice": "Historical publication receipt only; use verify_draft for current/editor-edited state"}
            if receipt.get("status") == "publication_prepared":
                return recover_publication(settings, plan, receipt, job)
            raise ToolError("previous_failure", f"Previous attempt {receipt.get('status')}; examine receipt/backups, do not overwrite")
        require_closed()
        require_acceptance(settings)
        check_inputs(plan, settings)
        no_collision(settings, plan["request"]["name"], plan["draft_id"])
        stats, doc, meta = validate_payload(plan, settings, job / "payload")
        path, before_bytes, before = load_catalog(settings)
        entry = catalog_entry(plan, meta)
        after = copy.deepcopy(before)
        after["all_draft_store"] = [entry] + before["all_draft_store"]
        after["draft_ids"] += 1
        index_delta(before, after, entry)
        after_bytes = canonical(after)
        write_new(job / "catalog_before.json", before_bytes)
        receipt = {"schema": "jianying-local-receipt/2", "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
                   "draft_path": plan["target"], "draft_name": plan["request"]["name"], "draft_id": plan["draft_id"],
                   "file_validation": "passed", "boundary_validation": "passed" if plan["source_files"] else "new_draft",
                   "registration": "not_started", "editor_acceptance": "pending", "status": "publication_prepared",
                   "catalog_before_sha256": digest(before_bytes), "catalog_after_sha256": digest(after_bytes),
                   "catalog_backup": str(job / "catalog_before.json"), "output_sha256": plan["output_sha256"],
                   "source_unchanged": True, "media_copies": 0, "stats": stats,
                   "resource_copy_bytes": plan["preview"]["resource_copy_bytes"],
                   "subtitle_pending_review": plan["preview"]["subtitles"]["warnings"],
                   "race_boundary": "No atomic CAS with the native app; keep Jianying fully closed"}
        write_new(job / "catalog_after.json", after_bytes)
        write_new(receipt_path, canonical(receipt))
        try:
            require_closed(); check_inputs(plan, settings)
            target = safe_path(plan["target"])
            target.mkdir()  # Fails rather than overwriting any existing folder.
            for name in plan["output_sha256"]:
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                write_new(target / name, small(job / "payload" / name))
            validate_payload(plan, settings, target)
            require_closed(); check_inputs(plan, settings)
            atomic_write(path, after_bytes)
            require(small(path) == after_bytes, "catalog_race", "Catalog changed during registration; retain backups, no forced rollback")
            for stamp in plan["source_files"]: check_stamp(stamp)
            receipt.update(status="registered", registration="registered", registered_us=time.time_ns() // 1000)
            atomic_write(receipt_path, canonical(receipt))
            return receipt
        except Exception as exc:
            receipt.update(status="failed", registration="uncertain" if digest(small(path)) != digest(before_bytes) else "not_registered",
                           error={"code": getattr(exc, "code", "write_failed"), "message": str(exc)},
                           recovery="Keep generated folder and exact catalog backup; no automatic overwrite/delete/rollback")
            atomic_write(receipt_path, canonical(receipt))
            raise


def recover_publication(settings, plan, receipt, job):
    # After an interrupted process, only acknowledge an already completed exact publication.
    # Never resume partial native writes or revert a user-edited catalog automatically.
    require_closed()
    path, data, _ = load_catalog(settings)
    require(digest(data) == receipt["catalog_after_sha256"], "incomplete_publication", "Interrupted publication is not fully registered; preserve evidence and inspect manually")
    validate_payload(plan, settings, safe_path(plan["target"], exists=True))
    for stamp in plan["source_files"]: check_stamp(stamp)
    receipt.update(status="registered", registration="registered", recovered_exact_publication=True)
    atomic_write(job / "receipt.json", canonical(receipt))
    return receipt


def doctor(settings):
    settings.validate()
    pids = app_pids()
    return {"version": VERSION, "python": sys.version.split()[0], "python_executable": sys.executable,
            "ffprobe": shutil.which("ffprobe"), "ffmpeg": shutil.which("ffmpeg"), "editor_pids": pids,
            "editor_closed": not pids, "editor_version_candidate": app_version(settings), "supported_schema": SCHEMA,
            "backend": str(settings.vendor), "backend_present": (settings.vendor / "script_file.py").is_file(),
            "drafts_root": str(settings.drafts_root), "work_root": str(settings.work_root),
            "native_acceptance": "accepted" if accepted(settings) else "pending", "canary_mode": settings.canary,
            "plan_schema": "jianying-local-plan/2", "codec": codec.status(settings),
            "formats": ["flat-360000", "nested-single-11.5"],
            "production_features": accepted(settings).get("features", {}) if accepted(settings) else {},
            "startup_scan": False, "network": False, "automatic_export": False}


def list_drafts(settings, limit=100):
    require(1 <= integer(limit, "limit", 1) <= 1000, "invalid_input", "limit is 1..1000")
    _, data, catalog = load_catalog(settings)
    rows = catalog["all_draft_store"]
    return {"catalog_sha256": digest(data), "total": len(rows), "drafts": [
        {k: row.get(k) for k in ("draft_name", "draft_id", "draft_fold_path", "tm_duration")}
        for row in rows[:limit]], "truncated": len(rows) > limit}


def inspect_draft(settings, name, limit=100, cursor=None, audio_analysis=None):
    integer(limit, "limit", 1)
    require(limit <= 1000, "invalid_input", "Segment page limit is 1..1000")
    try:
        source = read_source(settings, name, allow_missing=True)
    except ToolError as exc:
        if exc.code in {"unsupported_structure", "unknown_dependency", "codec_unverified", "codec_failed", "save_state_conflict", "unsupported_editor"}:
            return {"name": name, "readability": "unsupported", "writable": False,
                    "diagnostic": {"code": exc.code, "reason": exc.reason}, "editor_acceptance": "not_inferred"}
        raise
    doc = source["doc"]
    offset = 0
    if cursor is not None:
        obj(cursor, ("fingerprint", "offset"), label="cursor")
        require(cursor["fingerprint"] == source["fingerprint"], "stale_cursor", "Page belongs to another saved version")
        offset = integer(cursor["offset"], "offset")
    rows = [{"id": s["id"], "track_id": t["id"], "material_id": s["material_id"], "target": s["target_timerange"],
             "source": s.get("source_timerange"), "volume": s.get("volume", 1), "speed": s.get("speed", 1),
             "common_keyframes": s.get("common_keyframes", []), "extra_material_refs": s.get("extra_material_refs", [])} for t,s in segments(doc)]
    require(offset <= len(rows), "invalid_input", "Cursor offset exceeds segment count")
    return {"name": name, "id": doc["id"], "path": str(source["folder"]), "readability": "supported_local_format",
            "format": source["format"], "latest_content_source": source["content_name"], "source_fingerprint": source["fingerprint"],
            "format_identity": source["format_identity"], "missing_media": source["missing"],
            "capabilities": {"diagnose": True, "copy_plan": not source["missing"], "relink_preview": True, "production_apply": accepted(settings) is not None},
            "stats": validate_doc(doc, source["media"] if not source["missing"] else None), "fingerprints": source["files"], "media": list(source["media"].values()),
            "tracks": [{"id": t["id"], "type": t["type"], "name": t.get("name"), "segment_count": len(t["segments"]),
                        "segments": [s for s in rows[offset:offset+limit] if s["track_id"] == t["id"]]} for t in doc["tracks"]],
            "next_cursor": {"fingerprint": source["fingerprint"], "offset": offset+limit} if offset+limit < len(rows) else None,
            "audio_metrics": music.analyze(settings, audio_analysis) if audio_analysis else [], "editor_acceptance": "not_inferred"}


def verify_draft(settings, name, plan_id=None, validation="exact"):
    source = read_source(settings, name)
    result = {"name": name, "file_validation": "passed", "stats": validate_doc(source["doc"], source["media"]),
              "media_references": len(source["media"]), "editor_acceptance": "not_inferred", "boundary_validation": "not_requested"}
    if plan_id:
        plan = load_plan(settings, plan_id)
        require(plan["target"] == str(source["folder"]), "invalid_input", "Plan belongs to a different draft")
        require(validation in {"exact", "after_save"}, "invalid_input", "Validation must be exact or after_save")
        if validation == "after_save":
            from .verification import compare_saved
            expected = decode(small(job_path(settings, plan_id) / "expected_doc.json"))
            require(digest(canonical(expected)) == plan["expected_doc_sha256"], "invalid_plan", "Expected semantic snapshot changed")
            compare_saved(expected, source["doc"], settings=settings)
        else:
            validate_payload(plan, settings, source["folder"])
        for stamp in plan["source_files"]: check_stamp(stamp)
        result.update(boundary_validation="passed", source_unchanged=True, exact_planned_bytes=validation == "exact",
                      validation=validation, semantic_validation="passed" if validation == "after_save" else "not_requested")
    return result


OPERATIONS = {"doctor", "list_drafts", "inspect_draft", "plan_draft", "apply_plan", "verify_draft"}


def dispatch(settings, operation, arguments):
    require(operation in OPERATIONS, "unknown_operation", "Only the six documented operations are exposed")
    if operation == "doctor": obj(arguments); return doctor(settings)
    if operation == "list_drafts": obj(arguments, (), ("limit",)); return list_drafts(settings, **arguments)
    if operation == "inspect_draft": obj(arguments, ("name",), ("limit", "cursor", "audio_analysis")); return inspect_draft(settings, **arguments)
    if operation == "plan_draft": return plan_draft(settings, arguments)
    if operation == "apply_plan": obj(arguments, ("plan_id", "expected_plan_sha256")); return apply_plan(settings, **arguments)
    obj(arguments, ("name",), ("plan_id", "validation")); return verify_draft(settings, **arguments)
