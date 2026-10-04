"""Post-save semantic comparison with an explicit native-default allowlist.

Unknown fields are retained, including unknown false/zero values. A mismatch is
diagnostic, never an excuse to overwrite a user's saved state.
"""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
from .runtime import require

NATIVE_VERSION = "11.5.0"

DEFAULTS = {
    "speed": 1, "volume": 1, "fps": 30, "reverse": False,
    "start": 0, "time_offset": 0, "alpha": 1, "rotation": 0,
    "curveType": "Line", "graphID": "", "material_id": "",
    "flag": 0, "attribute": 0, "is_default_name": True,
    "track_render_index": 0, "render_index": 0,
    "enable_adjust": True, "enable_hsl": False, "enable_lut": True,
    "enable_adjust_mask": True, "enable_color_curves": True,
    "enable_color_match_adjust": False,
    "cartoon": False, "has_audio": True, "is_ai_generate_content": False,
    "is_copyright": False, "is_unified_beauty": False, "intensifies_audio": False,
    "is_overlap": False, "is_tone_modify": False,
    "typesetting": 0, "alignment": 0, "global_alpha": 1,
    "letter_spacing": 0, "line_feed": 1, "force_apply_line_max_width": False,
    "keyframes": {}, "common_keyframes": [], "extra_material_refs": [],
    "source": "segmentsourcenormal", "uniform_scale": {}, "responsive_layout": {}, "render_timerange": {},
    "audio_source": "", "aigc_type": "none", "algorithm_type": "", "audio_adjust_params": [],
    "path": "", "media_path": "", "source_platform": 0, "app_id": 0, "fade_type": 0,
    "mode": 0, "resource_id": "", "crop_ratio": "free", "crop_scale": 1,
    "keyframe_refs": [], "visible": True, "last_nonzero_volume": 1,
    "enable_color_correct_adjust": False, "enable_smart_color_adjust": False,
    "enable_color_wheels": True, "track_attribute": 0, "is_default_name": False,
    "meta_type": "none", "copyright_limit_type": "none", "benefit_type": "none",
    "formula_id": "", "effect_id": "", "platform": "all", "ratio": "original",
    "free_render_index_mode_on": False,
    "type": "", "open_close_key_complement_frame_flag": False,
}
VOLATILE_TOP = {"create_time", "update_time", "platform", "last_modified_platform", "new_version"}
MATERIAL_ENGINE = {"check_flag", "local_material_id", "category_name", "category_id", "music_id", "material_id", "material_name", "name"}
AUTO_RESOURCES = {"canvases", "material_colors", "placeholder_infos", "sound_channel_mappings", "vocal_separations"}
EMPTY_FIELDS = {"matting", "stable", "video_algorithm", "video_mask_shadow", "video_mask_stroke", "beauty_face_auto_preset",
                "face_info", "picture_from", "request_id", "team_id", "category_id", "local_id", "origin_material_id",
                "ai_template_id", "ai_template_type", "ai_template_params", "similiar_music_info", "tts_benefit_info",
                "type_option", "fixed_width", "fixed_height", "enable_video_mask", "disable_video_mask", "style_id",
                "source_from", "crop_ratio", "crop_scale", "extra_type_option", "background_style", "font_id",
                "subtitle_keywords", "language", "recognize_task_id", "recognize_type", "recognize_text", "text_to_audio_ids",
                "subtitle_template_id", "words", "source_timerange"}
EMPTY_FIELDS |= {"algorithms", "gameplay_configs", "complement_frame_config", "deflicker", "quality_enhance",
                 "motion_blur_config", "noise_reduction", "path", "story_video_modify_video_config", "time_range",
                 "wave_points", "keyframe_graph_list", "relationships", "function_assistant_info", "fps",
                 "uneven_animation_template_info", "smart_ads_info", "static_cover_image_path", "config", "curve_speed", "curve_speed_config", "audio_fade"}

CONFIG_DEFAULTS = {"adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1, "export_range": None,
    "extract_audio_last_index": 1, "lyrics_recognition_id": "", "lyrics_sync": True, "lyrics_taskinfo": [],
    "maintrack_adsorb": True, "material_save_mode": 0, "multi_language_current": "none", "multi_language_list": [],
    "multi_language_main": "none", "multi_language_mode": "none", "original_sound_last_index": 1,
    "record_audio_last_index": 1, "sticker_max_index": 1, "subtitle_keywords_config": None, "subtitle_recognition_id": "",
    "subtitle_sync": True, "subtitle_taskinfo": [], "system_font_list": [], "video_mute": False, "zoom_info_params": None}
NULL_TOP = {"business_info", "cover", "extra_info", "group_container", "mutable_config", "retouch_cover", "time_marks"}
KEYFRAME_KINDS = {"adjusts", "audios", "effects", "filters", "handwrites", "stickers", "texts", "videos"}


def text_wrapper(row, native=False, settings=None):
    """Only observed redundant subtitle wrappers; styled content stays exact."""
    value = copy.deepcopy(row)
    if native:
        for key, empty in {"caption_template_info": {"path":"", "resource_id":""},
                           "lyrics_template": {"path":"", "resource_id":""},
                           "combo_info": {}, "current_words": {}, "shadow_point": {"x":0.0,"y":0.0}}.items():
            if value.get(key) == empty:
                value.pop(key)
        content = json.loads(value.get("content", "{}"))
        if value.get("translate_original_text") == content.get("text"):
            value.pop("translate_original_text")
        import os
        from .runtime import Settings
        fallback = (settings or Settings()).app_root / "Resources" / "Font" / "SystemFont" / "zh-hans.ttf"
        if value.get("font_path") and Path(value["font_path"]) == fallback and content.get("styles") and all(
                s.get("font", {}).get("path") for s in content["styles"]):
            value.pop("font_path")
    return value


def equivalent_effect_paths(left, right, effect_id):
    """Bounded proof for two locally cached copies of one rendering resource."""
    import os
    from .runtime import digest, safe_path
    if left == right:
        return True
    if not all(isinstance(p, str) and p for p in (left, right)):
        return False  # No equivalence proof for an absent renderer dependency.
    if not isinstance(effect_id, str) or not effect_id.isdigit():
        return False
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "User Data" / "Cache" / "effect" / effect_id
    paths = [safe_path(p, exists=True) for p in (left, right)]
    if not all(p.is_dir() and p.parent == root and p.name.isalnum() for p in paths):
        return False
    def signature(path):
        rows, total = [], 0
        for item in sorted(path.rglob("*")):
            safe_path(item)
            if not item.is_file():
                continue
            relative = item.relative_to(path).as_posix()
            # Observed CDN child-selector marker, not a renderer asset.
            if relative == "effect_platform_children.tag":
                require(item.stat().st_size <= 4096, "native_semantics_changed", "Unexpected cache selector size")
                continue
            total += item.stat().st_size
            require(total <= 20*1024*1024 and len(rows) < 1000,
                    "native_semantics_changed", "Cache equivalence exceeds bounded verification")
            rows.append((relative, item.stat().st_size, digest(item.read_bytes())))
        return rows
    a, b = map(signature, paths)
    return bool(a) and a == b


def normalized(value, trail=()):
    if isinstance(value, list):
        return [normalized(v, trail+(i,)) for i,v in enumerate(value)]
    if not isinstance(value, dict):
        return value
    result = {}
    for k,v in value.items():
        if not trail and k in NULL_TOP and v is None:
            continue
        if not trail and k == "keyframes" and isinstance(v, dict) and set(v) <= KEYFRAME_KINDS and all(x == [] for x in v.values()):
            continue
        if trail == ("config",) and k in CONFIG_DEFAULTS and v == CONFIG_DEFAULTS[k]:
            continue
        if not trail and k == "source" and v == "default":
            continue
        if k == "uniform_scale" and v == {"on": True, "value": 1}:
            continue
        if trail[-1:] == ("hdr_settings",) and ((k == "intensity" and v == 1) or (k == "nits" and v == 1000)):
            continue
        if k == "path" and isinstance(v,str) and Path(v).is_absolute():
            from .runtime import safe_path
            v = str(safe_path(v)).casefold()
        if k == "content" and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        if k in DEFAULTS and v == DEFAULTS[k]:
            continue
        if k in EMPTY_FIELDS and v in (None, "", False, 0, {}, []):
            continue
        if trail[-1:] in {("scale",), ("transform",)} and k in {"x", "y"} and v == (1 if trail[-1] == "scale" else 0):
            continue
        if trail[-1:] == ("flip",) and v is False:
            continue
        if k == "crop" and (v == {} or v == {"upper_left_x": 0, "upper_left_y": 0, "upper_right_x": 1, "upper_right_y": 0,
                                            "lower_left_x": 0, "lower_left_y": 1, "lower_right_x": 1, "lower_right_y": 1}):
            continue
        child = normalized(v, trail+(k,))
        if k in {"flip", "scale", "transform", "clip"} and not child:
            continue
        if k in EMPTY_FIELDS and child in (None, "", False, 0, {}, []):
            continue
        result[k] = child
    return result


def compare_saved(expected, actual, settings=None):
    from .core import resource_map, validate_doc
    validate_doc(actual)
    er, ar = resource_map(expected), resource_map(actual)
    pinned = actual.get("last_modified_platform", {}).get("app_version") == NATIVE_VERSION
    missing = set(er)-set(ar)
    removed_text_speeds = set()
    for key in missing:
        kind, row = er[key]
        uses = [(t,s) for t in expected["tracks"] for s in t["segments"] if key in s.get("extra_material_refs", [])]
        require(pinned and kind == "speeds" and row.get("speed") == 1 and
                not normalized({k:v for k,v in row.items() if k not in {"id", "type", "speed"}}) and
                uses and all(t["type"] == "text" for t,s in uses),
                "native_semantics_changed", "A planned nondefault/nontext material disappeared after save")
        removed_text_speeds.add(key)
    additional = set(ar)-set(er)
    for key in additional:
        kind, row = ar[key]
        require(row.get("type", "") in {"", "canvas_color", "none", "placeholder", "placeholder_info", "sound_channel_mapping", "vocal_separation"},
                "native_semantics_changed", "Unexpected automatic resource type")
        simple = {k:v for k,v in row.items() if k not in {"id", "type"}}
        require(kind in AUTO_RESOURCES and not normalized(simple), "native_semantics_changed", "Unplanned nondefault material appeared after save")
    # All media identities, paths, durations and nondefault effects must agree.
    frame_alignments = []
    audio_frame_alignments = []
    for key, (kind, e) in er.items():
        if key in removed_text_speeds:
            continue
        akind, a = ar[key]
        require(kind == akind, "native_semantics_changed", "Material category changed")
        e = {k:v for k,v in e.items() if k not in MATERIAL_ENGINE}
        a = {k:v for k,v in a.items() if k not in MATERIAL_ENGINE}
        if kind == "texts" and pinned:
            # An enrich/patch expectation may itself already be native-saved.
            # Canonicalize the same observed wrappers on both pinned sides;
            # otherwise unchanged native text falsely differs on its next save.
            e, a = text_wrapper(e, native=expected.get("last_modified_platform", {}).get("app_version") == NATIVE_VERSION, settings=settings), text_wrapper(a, native=True, settings=settings)
            for field, default in (("alignment", 1), ("line_max_width", .82)):
                if field not in a and e.get(field) == default:
                    e.pop(field)
        if kind == "transitions" and pinned and e.get("path") != a.get("path"):
            require(e.get("resource_id") == a.get("resource_id") and e.get("effect_id") == a.get("effect_id") and
                    equivalent_effect_paths(e.get("path"), a.get("path"), e.get("effect_id")),
                    "native_semantics_changed", "Transition rendering resource changed")
            e["path"] = a["path"]
        if kind == "videos" and e.get("duration") != a.get("duration"):
            # Native frame rounding of the COMPLETE original's material length,
            # never tolerance for clip in/out or timeline positions.
            quantum = math.ceil(1_000_000 / expected.get("fps", 30))
            require(type(e.get("duration")) is int and type(a.get("duration")) is int and
                    abs(e["duration"]-a["duration"]) <= quantum, "native_semantics_changed", "Full material duration changed beyond one frame")
            frame_alignments.append({"id": key, "before_us": e["duration"], "after_us": a["duration"]})
            e["duration"] = a["duration"]
        if kind == "audios" and e.get("duration") != a.get("duration"):
            # Observed 11.5 local MP3 import: COMPLETE material length rounds UP
            # to the project frame grid. Never relax any segment range or gain.
            fps = expected.get("fps", 30)
            before, after = e.get("duration"), a.get("duration")
            require(pinned and e.get("type") == a.get("type") == "extract_music" and
                    bool(e.get("path")) and normalized({"path": e["path"]}) == normalized({"path": a.get("path")}) and
                    type(before) is int and type(after) is int and before > 0 and
                    after == int(math.ceil(before * fps / 1_000_000) * 1_000_000 / fps) and
                    0 <= after - before <= math.ceil(1_000_000 / fps),
                    "native_semantics_changed", "Full audio length is not the pinned native frame up-rounding")
            audio_frame_alignments.append({"id": key, "before_us": before, "after_us": after})
            e["duration"] = after
        require(normalized(e) == normalized(a), "native_semantics_changed", f"Saved material differs: {kind}/{key}")
    e = copy.deepcopy(expected)
    a = copy.deepcopy(actual)
    # One microsecond can represent the same integral source-frame count when
    # target durations are computed by subtracting two truncated timestamps.
    # This is not a one-frame tolerance and never applies to video in/out.
    audio_representations = []
    expected_segments = {s["id"]: (t,s) for t in e["tracks"] for s in t["segments"]}
    if pinned:
        for track in a["tracks"]:
            if track["type"] != "audio":
                continue
            for saved in track["segments"]:
                if saved["id"] not in expected_segments:
                    continue
                old_track, planned = expected_segments[saved["id"]]
                old, new = planned.get("source_timerange",{}), saved.get("source_timerange",{})
                if old != new and old_track["type"] == "audio" and planned.get("speed",1) == saved.get("speed",1) == 1 and \
                        planned["target_timerange"] == saved["target_timerange"] and old.get("start",0) == new.get("start",0) and \
                        type(old.get("duration")) is int and type(new.get("duration")) is int and abs(old["duration"]-new["duration"]) <= 1:
                    fps = expected.get("fps",30)
                    frames = round(old["duration"]*fps/1_000_000)
                    if all(abs(x*fps/1_000_000-frames) <= fps/1_000_000+.0000001 for x in (old["duration"],new["duration"])):
                        audio_representations.append({"segment_id":saved["id"],"before_us":old["duration"],"after_us":new["duration"]})
                        planned["source_timerange"] = copy.deepcopy(new)
    e.pop("materials", None); a.pop("materials", None)
    for key in VOLATILE_TOP:
        e.pop(key, None); a.pop(key, None)
    for doc in (e,a):
        for index, track in enumerate(doc["tracks"]):
            for seg in track["segments"]:
                refs = [r for r in seg.get("extra_material_refs", []) if r not in additional | removed_text_speeds]
                kinds = [er[r][0] for r in refs]
                reorderable = AUTO_RESOURCES | {"speeds", "transitions"}
                if pinned and set(kinds) <= reorderable and len(kinds) == len(set(kinds)):
                    refs.sort()
                seg["extra_material_refs"] = refs
                # Render indexes derived solely from unchanged track order, and
                # image adjustment switches on audio-only segments, have no A/V
                # editing meaning. Do not ignore arbitrary index/value changes.
                if seg.get("track_render_index", 0) in {0, index}:
                    seg.pop("track_render_index", None)
                if track["type"] == "audio" or pinned and track["type"] == "text":
                    for k in ("enable_adjust", "enable_lut", "enable_adjust_mask"):
                        if isinstance(seg.get(k), bool):
                            seg.pop(k)
                    if seg.get("hdr_settings", "absent") is None:
                        seg.pop("hdr_settings")
    require(normalized(e) == normalized(a), "native_semantics_changed", "Saved tracks/clip ranges/keyframes/unknown fields differ from plan")
    return {"semantic_validation": "passed", "automatic_default_resources": len(additional),
            "native_removed_default_text_speeds": len(removed_text_speeds),
            "audio_source_microsecond_representations":audio_representations,
            "audio_material_duration_frame_alignments": audio_frame_alignments,
            "material_duration_frame_alignments": frame_alignments, "unknown_field_policy": "no_generic_discard"}
