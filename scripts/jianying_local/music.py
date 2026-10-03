"""Reference-only loop scheduling, linear gain joins and cached audio measurement."""
from __future__ import annotations
import math
import re
import shutil
from pathlib import Path
from .runtime import canonical, check_stamp, decode, digest, file_stamp, integer, probe, require, safe_path, write_new


def schedule(items, fps=None):
    result = []
    for i, item in enumerate(items):
        start, end = item["start_us"], item["start_us"]+item["duration_us"]
        source_length = item["loop_out_us"]-item["in_us"] if item.get("loop") else item["duration_us"]
        join = item.get("loop_crossfade_us", 0)
        cursor, cycle = start, 0
        rows = []
        while cursor < end:
            require(len(rows) < 10000, "loop_limit", "BGM requires too many loop clips")
            length = min(source_length, end-cursor)
            row = {"item": i, "cycle": cycle, "start_us": cursor, "duration_us": length, "in_us": item["in_us"],
                   "fade_in_us": min(length, item["fade_in_us"] if cycle == 0 else join), "fade_out_us": 0}
            rows.append(row)
            if cursor+length == end:
                break
            require(item.get("loop"), "out_of_range", "BGM length exceeds nonloop source")
            row["fade_out_us"] = join
            cursor += length-join
            cycle += 1
        rows[-1]["fade_out_us"] = item["fade_out_us"]
        require(all(r["fade_in_us"]+r["fade_out_us"] <= r["duration_us"] for r in rows), "out_of_range", "Loop ending is too short for requested fades")
        if item.get("crossfade_previous_us", 0):
            overlap = item["crossfade_previous_us"]
            previous = [r for r in result if r["item"] == i-1]
            require(previous, "invalid_join", "No previous BGM to crossfade")
            p = previous[-1]
            require(p["start_us"]+p["duration_us"]-overlap == rows[0]["start_us"], "invalid_join", "Explicit BGM ranges do not have exactly the requested overlap")
            require(overlap <= min(p["duration_us"]-p["fade_in_us"], rows[0]["duration_us"]-rows[0]["fade_out_us"]), "invalid_join", "Crossfade does not fit")
            require(p["fade_out_us"] in {0, overlap} and rows[0]["fade_in_us"] in {0, overlap}, "invalid_join", "Explicit fades conflict with crossfade")
            p["fade_out_us"] = overlap
            rows[0]["fade_in_us"] = overlap
        result.extend(rows)
    if fps is not None:
        for row in result:
            old = dict(row)
            snap = lambda v: math.floor(math.floor(v*fps/1_000_000 + .5)*1_000_000/fps)
            end = snap(row["start_us"]+row["duration_us"])
            row["start_us"] = snap(row["start_us"])
            row["duration_us"] = end-row["start_us"]
            require(row["duration_us"] > 0, "invalid_join", "BGM segment collapses on the native frame grid")
            row["requested_range_us"] = [old["start_us"], old["start_us"]+old["duration_us"]]
            row["effective_range_us"] = [row["start_us"], end]
        # Actual overlap determines complementary linear ramps, including when
        # 250ms is not an integer frame count. Do not change old audio tracks.
        for index,row in enumerate(result):
            if row["cycle"] or items[row["item"]].get("crossfade_previous_us",0):
                previous = result[index-1]
                overlap = previous["start_us"]+previous["duration_us"]-row["start_us"]
                if row["fade_in_us"]:
                    require(overlap > 0, "invalid_join", "Frame alignment removed requested crossfade")
                    row["fade_in_us"] = previous["fade_out_us"] = overlap
            require(row["fade_in_us"]+row["fade_out_us"] <= row["duration_us"], "invalid_join", "Aligned fades exceed BGM range")
    return result


def interpolate(points, time):
    ordered = sorted(points.items())
    if time <= ordered[0][0]:
        return ordered[0][1]
    for (a, x), (b, y) in zip(ordered, ordered[1:]):
        if time <= b:
            return x+(y-x)*(time-a)/(b-a)
    return ordered[-1][1]


def envelope(item, row):
    start, length = row["start_us"], row["duration_us"]
    global_points = {item["start_us"]: item["volume"], item["start_us"]+item["duration_us"]: item["volume"]}
    for w in item["ducking"]:
        global_points.update({w["start_us"]-w["attack_us"]: item["volume"], w["start_us"]: w["volume"],
                              w["end_us"]: w["volume"], w["end_us"]+w["release_us"]: item["volume"]})
    fade_points = {0: 1.0, length: 1.0}
    if row["fade_in_us"]:
        fade_points.update({0: 0.0, row["fade_in_us"]: 1.0})
    if row["fade_out_us"]:
        fade_points.update({length-row["fade_out_us"]: 1.0, length: 0.0})
    points = {0, length, *fade_points}
    points.update(t-start for t in global_points if start <= t <= start+length)
    # Combined duck+fade ramps are sampled only where necessary; every emitted
    # keyframe has linear interpolation and all absolute speech boundaries remain.
    for a, b in zip(sorted(points), sorted(points)[1:]):
        if interpolate(global_points, start+a) != interpolate(global_points, start+b) and interpolate(fade_points, a) != interpolate(fade_points, b):
            points.update(range(a, b, 20_000))
    return {t: interpolate(global_points, start+t)*interpolate(fade_points, t) for t in sorted(points)}


def add(doc, items, settings):
    from .backend import load
    api = load(settings)
    helper = api.ScriptFile(doc["canvas_config"]["width"], doc["canvas_config"]["height"], int(doc.get("fps", 30)), True)
    rows = schedule(items, fps=doc.get("fps",30))
    mats, lanes, names = {}, [], []
    used = {t.get("name") for t in doc["tracks"]}
    for row in rows:
        item = items[row["item"]]
        if item["path"] not in mats:
            mats[item["path"]] = api.AudioMaterial(item["path"])
        index = next((i for i, end in enumerate(lanes) if end <= row["start_us"]), None)
        if index is None:
            index = len(lanes)
            name = "BGM循环_"+str(index+1)
            while name in used:
                name += "_新增"
            helper.add_track(api.TrackType.audio, name)
            names.append(name)
            lanes.append(0)
            used.add(name)
        segment = api.AudioSegment(mats[item["path"]], api.Timerange(row["start_us"], row["duration_us"]),
                                   source_timerange=api.Timerange(row["in_us"], row["duration_us"]), volume=item["volume"])
        # Joins and fades use explicit LINE gain keyframes, not undocumented fade
        # curves. No extracted/duplicated audio is produced.
        for offset, gain in envelope(item, row).items():
            segment.add_keyframe(offset, gain)
        helper.add_segment(segment, names[index])
        lanes[index] = row["start_us"]+row["duration_us"]
        row["segment_id"] = segment.segment_id
    for kind in ("audios", "speeds", "audio_fades"):
        values = [x.export_json() for x in getattr(helper.materials, kind)]
        if values:
            doc["materials"].setdefault(kind, []).extend(values)
    tracks = [t.export_json() for t in helper.tracks.values()]
    doc["tracks"].extend(tracks)
    return [t["id"] for t in tracks], rows


def analyze(settings, requests):
    from .codec import binary_stamp, run_monitored
    from .core import array, obj
    ffmpeg = shutil.which("ffmpeg")
    require(ffmpeg, "missing_runtime", "Existing FFmpeg is required for measurement")
    tool = binary_stamp(Path(ffmpeg))
    output = []
    cache_root = safe_path(settings.work_root / "audio_metrics")
    cache_root.mkdir(parents=True, exist_ok=True)
    for item in array(requests, "audio_analysis", maximum=100):
        obj(item, ("path", "in_us", "duration_us"), label="audio measurement")
        p = safe_path(item["path"], exists=True)
        a, length = integer(item["in_us"], "in_us"), integer(item["duration_us"], "duration_us", 1)
        info = probe(p)
        require(info["audio"] and a+length <= info["duration_us"] and length <= 3_600_000_000,
                "out_of_range", "Measurement range exceeds audio or one-hour call limit")
        binding = {"media": info, "in_us": a, "duration_us": length, "ffmpeg": tool, "filter": "ebur128=peak=true"}
        cache = cache_root / (digest(canonical(binding))+".json")
        if cache.exists():
            result = decode(cache.read_bytes())
            output.append({**result, "cached": True})
            continue
        code, _, error, peak = run_monitored([ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-ss", f"{a/1e6:.6f}",
                   "-i", str(p), "-t", f"{length/1e6:.6f}", "-map", "0:a:0", "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                   cwd=settings.work_root, timeout=180)
        require(code == 0, "analysis_failed", "FFmpeg loudness measurement failed")
        text = error.decode("utf-8", "replace").rsplit("Summary:", 1)[-1]
        def metric(pattern):
            match = re.search(pattern, text)
            require(match is not None, "analysis_failed", "Loudness summary is missing")
            value = float(match.group(1))
            return value if math.isfinite(value) else None
        result = {"path": str(p), "range_us": [a, a+length], "integrated_lufs": metric(r"\bI:\s*([-\w.]+)\s+LUFS"),
                  "lra_lu": metric(r"\bLRA:\s*([-\w.]+)\s+LU"), "true_peak_dbfs": metric(r"Peak:\s*([-\w.]+)\s+dBFS"),
                  "worker_peak_bytes": peak, "binding": binding, "normalization": "not_performed",
                  "listening": "pending", "publication_rights": "not_inferred", "audio_files_created": 0,
                  "advice": "Compare music and speech at intended gain; measurement is not listening or automatic gain correction"}
        check_stamp(info["stamp"])
        write_new(cache, canonical(result))
        output.append({**result, "cached": False})
    return output
