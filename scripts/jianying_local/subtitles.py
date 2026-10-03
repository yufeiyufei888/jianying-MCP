"""Strict SRT input and per-occurrence source-to-timeline mapping, never ASR."""
from __future__ import annotations
import codecs
import re
from fractions import Fraction
from pathlib import Path
from .runtime import file_stamp, require, safe_path

STAMP = re.compile(r"^(\d{2,3}):([0-5]\d):([0-5]\d)[,.](\d{3})$")


def timestamp(value):
    match = STAMP.fullmatch(value.strip())
    require(match is not None, "invalid_srt", "Invalid SRT timestamp")
    h, m, s, ms = map(int, match.groups())
    return ((h * 60 + m) * 60 + s) * 1_000_000 + ms * 1000


def parse(data, encoding="utf-8-sig"):
    require(len(data) <= 4 * 1024 * 1024, "invalid_srt", "SRT exceeds 4 MiB")
    try:
        name = codecs.lookup(encoding).name
        require(name in {"utf-8", "utf-8-sig", "gb18030", "gbk", "utf-16", "utf-16-le", "utf-16-be"},
                "invalid_encoding", "Unsupported explicitly selected text encoding")
        text = data.decode(encoding, errors="strict").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    except (UnicodeError, LookupError) as exc:
        from .runtime import ToolError
        raise ToolError("invalid_encoding", "SRT decoding failed; select the actual encoding explicitly") from exc
    require("\x00" not in text, "invalid_srt", "NUL in subtitle text")
    result, ids = [], set()
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.split("\n")
        require(len(lines) >= 3 and lines[0].strip().isdigit(), "invalid_srt", "Every cue needs a numeric ID, timestamps and text")
        cue_id = lines[0].strip()
        require(cue_id not in ids, "invalid_srt", "Duplicate SRT cue ID")
        ids.add(cue_id)
        times = lines[1].split("-->")
        require(len(times) == 2, "invalid_srt", "Invalid SRT range")
        a, b = map(timestamp, times)
        require(a < b, "invalid_srt", "SRT cue must have positive duration")
        content = "\n".join(lines[2:])
        require(content.strip(), "invalid_srt", "Empty cue text")
        result.append({"cue_id": cue_id, "start_us": a, "end_us": b, "text": content})
    require(result and len(result) <= 100000, "invalid_srt", "SRT is empty or has too many cues")
    return result


def map_cues(doc, items):
    from .core import array, obj, resource_map, segments
    resources = resource_map(doc)
    mapped, warnings, unmatched, inputs = [], [], [], []
    for index, item in enumerate(array(items, "subtitles", maximum=1000)):
        obj(item, ("path", "basis"), ("encoding", "material_id", "media_path", "partial_policy", "track_name"), "subtitle input")
        require(item["basis"] in {"timeline", "source"}, "invalid_input", "Subtitle basis must be timeline or source")
        require(item.get("partial_policy", "preserve_text_clip_time") == "preserve_text_clip_time",
                "invalid_input", "Only original-text/clipped-display-time policy is supported")
        require(isinstance(item.get("track_name", "导入字幕"), str) and 0 < len(item.get("track_name", "导入字幕")) <= 80,
                "invalid_input", "Subtitle track_name must be 1..80 characters")
        path = safe_path(item["path"], exists=True)
        require(path.suffix.lower() == ".srt", "invalid_srt", "Only explicit local SRT files are accepted")
        require(path.stat().st_size <= 4 * 1024 * 1024, "invalid_srt", "SRT exceeds 4 MiB")
        cues = parse(path.read_bytes(), item.get("encoding", "utf-8-sig"))
        inputs.append(file_stamp(path, hash_bytes=True))
        candidates = []
        if item["basis"] == "source":
            require(("material_id" in item) != ("media_path" in item), "invalid_input", "Source captions require exactly one material_id or full media_path")
            if "media_path" in item:
                original = safe_path(item["media_path"])
                matched = {key for key, (kind, m) in resources.items() if kind in {"videos", "audios"} and safe_path(m["path"]) == original}
            else:
                matched = {item["material_id"]}
                require(item["material_id"] in resources and resources[item["material_id"]][0] in {"videos", "audios"},
                        "missing_material", "Source subtitle material is absent")
            require(matched, "missing_material", "Original identity is not used in this draft")
            for t, s in segments(doc):
                if s["material_id"] not in matched:
                    continue
                require(not s.get("reverse") and s.get("source", "segmentsourcenormal") == "segmentsourcenormal",
                        "unsupported_mapping", "Reverse/compound caption mapping is unsupported")
                require(not any(resources[r][0] == "speeds" and (resources[r][1].get("curve_speed") or resources[r][1].get("curve_speed_config")) for r in s.get("extra_material_refs", [])),
                        "unsupported_mapping", "Curve-speed caption mapping is unsupported")
                source = s["source_timerange"]
                target = s["target_timerange"]
                factor = Fraction(target["duration"], source["duration"])
                require(s.get("speed", 1) > 0 and abs(source["duration"] / target["duration"] - s.get("speed", 1)) <= 0.0001,
                        "unsupported_mapping", "Constant speed/duration association is unclear")
                candidates.append((s, source.get("start", 0), source.get("start", 0)+source["duration"], target.get("start", 0), factor))
        else:
            require("material_id" not in item and "media_path" not in item, "invalid_input", "Timeline SRT has no original selector")
            candidates = [(None, 0, doc["duration"], 0, Fraction(1))]
        for cue in cues:
            count = 0
            for seg, low, high, start, factor in candidates:
                a, b = max(low, cue["start_us"]), min(high, cue["end_us"])
                if a >= b:
                    continue
                # Rational rounding is bounded to at most half a microsecond,
                # rather than accumulating rounded seconds over the timeline.
                out_a, out_b = start+round((a-low)*factor), start+round((b-low)*factor)
                if out_a >= out_b:
                    warnings.append({"kind": "sub_microsecond_cue", "input": index, "cue_id": cue["cue_id"]})
                    continue
                row = {**cue, "start_us": out_a, "end_us": out_b, "input_index": index,
                       "source_segment_id": seg["id"] if seg else None, "source_range_us": [a, b],
                       "track_name": item.get("track_name", "导入字幕"), "partial": a != cue["start_us"] or b != cue["end_us"]}
                mapped.append(row)
                count += 1
                if row["partial"]:
                    warnings.append({"kind": "partial_sentence_pending_review", "input": index, "cue_id": cue["cue_id"],
                                     "segment_id": row["source_segment_id"], "display_range_us": [out_a, out_b], "text": cue["text"]})
                if len(cue["text"].replace("\n", "")) > 32 or cue["text"].count("\n") > 1:
                    warnings.append({"kind": "long_or_multiline_bottom_safe_area_pending_review", "input": index, "cue_id": cue["cue_id"]})
            if count == 0:
                unmatched.append({"input": index, "cue_id": cue["cue_id"], "source_range_us": [cue["start_us"], cue["end_us"]], "text": cue["text"]})
    mapped.sort(key=lambda c: (c["start_us"], c["end_us"], c["input_index"], c["cue_id"]))
    active = []
    for cue in mapped:
        active = [c for c in active if c["end_us"] > cue["start_us"]]
        for prior in active:
            warnings.append({"kind": "overlapping_cues_pending_review", "cues": [[prior["input_index"], prior["cue_id"]], [cue["input_index"], cue["cue_id"]]]})
        active.append(cue)
    return {"mapped": mapped, "warnings": warnings, "unmapped": unmatched, "input_files": inputs,
            "voice_identity": "not_inferred", "speech_detection": "not_performed"}


def add(doc, mapped, settings):
    if not mapped:
        return []
    from .backend import load
    import importlib
    api = load(settings)
    text = importlib.import_module("pyJianYingDraft.text_segment")
    canvas = doc["canvas_config"]
    helper = api.ScriptFile(canvas["width"], canvas["height"], int(doc.get("fps", 30)), True)
    # Native inspection found size 5 legible on 16:9 but too small on 9:16.
    # The portrait candidate stays behind the native-acceptance gate until it
    # has been viewed and saved/reopened; changing the preset invalidates plans.
    font_size = 9 if canvas["height"] > canvas["width"] else 5
    import os
    font = safe_path(Path(os.environ.get("WINDIR", os.environ.get("SYSTEMROOT", ""))) / "Fonts" / "msyh.ttc", exists=True)
    # Independent lanes preserve overlapping cues instead of losing/truncating text.
    lanes = []
    used = {t.get("name") for t in doc["tracks"]}
    for cue in mapped:
        lane = next((i for i, (end, label) in enumerate(lanes) if end <= cue["start_us"] and label == cue["track_name"]), None)
        if lane is None:
            lane = len(lanes)
            name = cue["track_name"] + "_" + str(lane+1)
            while name in used:
                name += "_新增"
            used.add(name)
            helper.add_track(api.TrackType.text, name)
            lanes.append((0, cue["track_name"]))
        names = list(helper.tracks)
        seg = text.TextSegment(cue["text"], api.Timerange(cue["start_us"], cue["end_us"]-cue["start_us"]),
                               style=text.TextStyle(size=font_size, color=(1,1,1), align=1, auto_wrapping=True, max_line_width=0.82),
                               clip_settings=api.ClipSettings(transform_y=-0.8), border=text.TextBorder(width=20))
        helper.add_segment(seg, names[lane])
        # The pinned native editor removes speed=1 resources on text tracks.
        # Text has no source media to retime. Avoid a transient speed reference
        # altogether rather than accepting broken material links on reload.
        lanes[lane] = (cue["end_us"], cue["track_name"])
    values = [s.export_material() for track in helper.tracks.values() for s in track.segments]
    for material in values:
        # Use a real installed font path; no vendor placeholder font IDs/paths.
        import json
        content = json.loads(material["content"])
        for style in content["styles"]:
            style["font"] = {"path": str(font), "id": ""}
        material["content"] = json.dumps(content, ensure_ascii=False)
    doc["materials"].setdefault("texts", []).extend(values)
    for kind, resources in helper.materials.export_json().items():
        if kind != "texts" and resources:
            doc["materials"].setdefault(kind, []).extend(resources)
    tracks = [t.export_json() for t in helper.tracks.values()]
    speed_refs = {s.segment_id: s.speed.global_id for t in helper.tracks.values() for s in t.segments}
    for track in tracks:
        for seg in track["segments"]:
            seg["extra_material_refs"] = [r for r in seg.get("extra_material_refs", [])
                                          if r != speed_refs[seg["id"]]]
    doc["tracks"].extend(tracks)
    return [t["id"] for t in tracks]
