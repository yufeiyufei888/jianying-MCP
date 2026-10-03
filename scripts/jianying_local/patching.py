"""Local continuous-main-track edits, with explicit piecewise companion mapping."""
from __future__ import annotations
import copy
from . import backend
from .runtime import canonical, digest, integer, require


def clip_range(seg):
    return seg["target_timerange"].get("start", 0), seg["target_timerange"]["duration"]


def plain(seg, resources):
    require(seg.get("speed", 1) == 1 and not seg.get("reverse") and seg.get("source", "segmentsourcenormal") == "segmentsourcenormal",
            "unsupported_clip", "Local edit requires ordinary forward 1x media, not compound/reverse/retimed clips")
    for ref in seg.get("extra_material_refs", []):
        kind, row = resources[ref]
        if kind == "speeds":
            require(row.get("speed", 1) == 1 and not row.get("curve_speed") and not row.get("curve_speed_config"),
                    "unsupported_clip", "Speed curves are not locally editable")
    require(seg.get("source_timerange", {}).get("duration") == seg["target_timerange"]["duration"],
            "unsupported_clip", "Source and timeline lengths must agree")


def transitions(track, resources):
    result = {}
    for i, s in enumerate(track["segments"]):
        refs = [r for r in s.get("extra_material_refs", []) if resources[r][0] == "transitions"]
        if refs:
            require(i + 1 < len(track["segments"]) and len(refs) == 1, "transition_conflict", "Unclear existing transition cut")
            result[s["id"]] = (track["segments"][i + 1]["id"], refs)
    return result


def time_map(old, new):
    lookup = {s["id"]: s for s in new}
    pieces = []
    for s in old:
        n = lookup[s["id"]]
        start, length = clip_range(s)
        ns, _ = clip_range(n)
        a = s["source_timerange"].get("start", 0)
        b = n["source_timerange"].get("start", 0)
        left = max(a, b)
        right = min(a + s["source_timerange"]["duration"], b + n["source_timerange"]["duration"])
        if left < right:
            old_start, new_start = start + left - a, ns + left - b
            pieces.append({"old_start_us": old_start, "old_end_us": start + right - a,
                           "new_start_us": new_start, "new_end_us": ns + right - b, "segment_id": s["id"]})
    return sorted(pieces, key=lambda p: p["old_start_us"])


def map_range(start, end, pieces):
    # Merge adjacent pieces only when they are the same affine translation.
    merged = []
    for p in pieces:
        delta = p["new_start_us"] - p["old_start_us"]
        if merged and merged[-1][1] == p["old_start_us"] and merged[-1][2] == delta:
            merged[-1] = (merged[-1][0], p["old_end_us"], delta)
        else:
            merged.append((p["old_start_us"], p["old_end_us"], delta))
    match = [p for p in merged if p[0] <= start < end <= p[1]]
    require(len(match) == 1, "companion_crosses_edit", "A companion crosses a removed/discontinuous region; no automatic split")
    return start + match[0][2], end + match[0][2]


def apply(doc, operations, settings):
    from .core import array, normalize, obj, resource_map, time_range
    previews = []
    for op in array(operations, "operations", maximum=1000):
        obj(op, ("type", "track_id", "companion_tracks"),
            ("segment_id", "anchor_segment_id", "position", "clip", "in_us", "out_us"), "local operation")
        require(op["type"] in {"insert_clip", "move_clip", "trim_clip"}, "invalid_input", "Only insert_clip, move_clip, trim_clip are supported")
        track = next((t for t in doc["tracks"] if t["id"] == op["track_id"]), None)
        require(track is not None and track["type"] == "video", "missing_track", "An explicit video track is required")
        require(not track.get("flag", 0) and not track.get("is_locked", False), "locked_track", "Locked tracks are not edited")
        policies = op["companion_tracks"]
        require(isinstance(policies, dict) and set(policies) == {t["id"] for t in doc["tracks"] if t is not track} and
                all(v in {"follow", "fixed"} for v in policies.values()), "companion_policy_required", "Every other track needs explicit follow/fixed policy per operation")
        resources = resource_map(doc)
        before = copy.deepcopy(track["segments"])
        cursor = 0
        for s in before:
            plain(s, resources)
            start, length = time_range(s["target_timerange"], "main")
            require(start == cursor and resources[s["material_id"]][0] == "videos", "unsupported_track", "Main track must be ordinary, continuous video")
            cursor += length
        require(cursor == doc["duration"], "unsupported_track", "Video main track must cover the complete timeline")
        existing_cuts = transitions(track, resources)
        if op["type"] == "insert_clip":
            require("clip" in op and "segment_id" not in op and "in_us" not in op and "out_us" not in op,
                    "invalid_input", "insert_clip requires clip, not existing segment or trim fields")
            seed = normalize({"request_id": __import__("uuid").uuid4().hex, "mode": "create", "name": "insert-helper",
                              "canvas": {**{k: doc["canvas_config"][k] for k in ("width", "height")}, "fps": int(doc.get("fps", 30))},
                              "clips": [op["clip"]]}, settings)
            built = backend.build(seed, settings)
            inserted = built["tracks"][0]["segments"][0]
            for kind, values in built["materials"].items():
                for row in values:
                    require(row["id"] not in resources, "duplicate_id", "Inserted helper resource collides")
                if values:
                    doc["materials"].setdefault(kind, []).extend(values)
            moving = inserted
        else:
            require("segment_id" in op and "clip" not in op, "invalid_input", "Existing clip ID is required")
            moving = next((s for s in track["segments"] if s["id"] == op["segment_id"]), None)
            require(moving is not None, "missing_segment", "Selected clip does not belong to track")
        if op["type"] == "trim_clip":
            require("anchor_segment_id" not in op and "position" not in op and "in_us" in op and "out_us" in op,
                    "invalid_input", "trim_clip requires explicit in_us/out_us only")
            a, b = integer(op["in_us"], "in_us"), integer(op["out_us"], "out_us", 1)
            mat = resources[moving["material_id"]][1]
            require(a < b <= mat["duration"], "out_of_range", "Trim exceeds full original")
            keyframes = moving.get("common_keyframes", [])
            require(not keyframes or a == moving["source_timerange"].get("start", 0), "keyframe_conflict", "Changing in-point with keyframes requires unverified keyframe retiming")
            require(all(k.get("time_offset", 0) <= b-a for group in keyframes for k in group.get("keyframe_list", [])),
                    "keyframe_conflict", "New duration would discard an existing keyframe")
            require(not any(resources[r][0] in {"animations", "effects", "video_effects"} for r in moving.get("extra_material_refs", [])),
                    "unsupported_clip", "Effect/animation trim boundaries are not verified")
            # Retain all unknown fields in these range objects as well.
            moving["source_timerange"].update(start=a, duration=b-a)
            moving["target_timerange"]["duration"] = b-a
        else:
            require("anchor_segment_id" in op and op.get("position") in {"before", "after"} and "in_us" not in op and "out_us" not in op,
                    "invalid_input", "Insert/move needs an explicit existing anchor and before/after")
            if op["type"] == "move_clip":
                require(moving["id"] != op["anchor_segment_id"], "invalid_input", "A clip cannot anchor itself")
                track["segments"].remove(moving)
            index = next((i for i, s in enumerate(track["segments"]) if s["id"] == op["anchor_segment_id"]), None)
            require(index is not None, "missing_segment", "Insertion anchor does not belong to track")
            track["segments"].insert(index + (op["position"] == "after"), moving)
        cursor = 0
        for s in track["segments"]:
            old_start, length = clip_range(s)
            if old_start != cursor:
                s["target_timerange"]["start"] = cursor
            cursor += length
        require(transitions(track, resource_map(doc)) == existing_cuts, "transition_conflict", "Local operation would change an existing transition cut")
        for s in before:
            if s["id"] in existing_cuts or any(s["id"] == v[0] for v in existing_cuts.values()):
                n = next(x for x in track["segments"] if x["id"] == s["id"])
                require(n["source_timerange"] == s["source_timerange"] and n["target_timerange"]["duration"] == s["target_timerange"]["duration"],
                        "transition_conflict", "Trim near an existing transition is not authorized")
        pieces = time_map(before, track["segments"])
        companions = []
        for other in doc["tracks"]:
            if other is track:
                continue
            policy = policies[other["id"]]
            for s in other["segments"]:
                start, length = clip_range(s)
                if policy == "follow":
                    new_start, new_end = map_range(start, start+length, pieces)
                    if new_start != start:
                        s["target_timerange"]["start"] = new_start
                        companions.append({"segment_id": s["id"], "before_start_us": start, "after_start_us": new_start})
                else:
                    new_start, new_end = start, start+length
                require(0 <= new_start < new_end <= cursor, "fixed_track_out_of_range", "Companion exceeds new main duration")
        doc["duration"] = cursor
        previews.append({"operation": op["type"], "track_id": track["id"], "duration_before_us": sum(clip_range(s)[1] for s in before),
                         "duration_after_us": cursor, "time_mapping": pieces, "companions": companions,
                         "segments": [{"id": s["id"], "before": next(({"source": x["source_timerange"], "target": x["target_timerange"]} for x in before if x["id"] == s["id"]), None),
                                       "after": {"source": s["source_timerange"], "target": s["target_timerange"]}} for s in track["segments"]]})
    return previews


def changes(base, output, trail=()):
    """Exact JSON pointer changes, including unknown fields, for audit/validation."""
    if type(base) is not type(output):
        return [{"pointer": list(trail), "before": base, "after": output}]
    if isinstance(base, dict):
        result = []
        for k in sorted(base.keys() | output.keys()):
            if k not in base or k not in output:
                result.append({"pointer": list(trail + (k,)), "before_present": k in base,
                               "before": base.get(k), "after_present": k in output, "after": output.get(k)})
            else:
                result.extend(changes(base[k], output[k], trail + (k,)))
        return result
    if isinstance(base, list):
        if len(base) != len(output):
            return [{"pointer": list(trail), "before": base, "after": output}]
        result = []
        for i, (b, o) in enumerate(zip(base, output)):
            result.extend(changes(b, o, trail + (i,)))
        return result
    return [] if base == output else [{"pointer": list(trail), "before": base, "after": output}]
