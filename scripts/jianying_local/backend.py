"""Only low-level draft payload construction, with no vendor package GUI import."""
from __future__ import annotations

import importlib
import json
import sys
import types
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from .runtime import Settings, ToolError, decode, probe, require, safe_path


def load(settings: Settings):
    # Load submodules without executing pyJianYingDraft.__init__ (which imports GUI controllers).
    if "pyJianYingDraft" not in sys.modules:
        package = types.ModuleType("pyJianYingDraft")
        package.__path__ = [str(settings.vendor)]
        sys.modules["pyJianYingDraft"] = package
    else:
        require(list(sys.modules["pyJianYingDraft"].__path__) == [str(settings.vendor)],
                "backend_conflict", "A different backend is already imported; use a fresh worker process")
    if "pymediainfo" not in sys.modules:
        shim = types.ModuleType("pymediainfo")

        class MediaInfo:
            @staticmethod
            def can_parse():
                return True

            @staticmethod
            def parse(path, **_kwargs):
                info = probe(path)
                duration = info["duration_us"] / 1000
                v = info["video"]
                return SimpleNamespace(video_tracks=[SimpleNamespace(
                    duration=duration, width=v["width"], height=v["height"])] if v else [],
                    audio_tracks=[SimpleNamespace(duration=duration)] if info["audio"] else [],
                    image_tracks=[], general_tracks=[SimpleNamespace(duration=duration)])

        shim.MediaInfo = MediaInfo
        sys.modules["pymediainfo"] = shim
    mod = lambda name: importlib.import_module("pyJianYingDraft." + name)
    return SimpleNamespace(ScriptFile=mod("script_file").ScriptFile,
                           VideoMaterial=mod("local_materials").VideoMaterial,
                           AudioMaterial=mod("local_materials").AudioMaterial,
                           CropSettings=mod("local_materials").CropSettings,
                           VideoSegment=mod("video_segment").VideoSegment,
                           AudioSegment=mod("audio_segment").AudioSegment,
                           ClipSettings=mod("segment").ClipSettings,
                           Timerange=mod("time_util").Timerange,
                           TrackType=mod("track").TrackType)


def build(request: dict, settings: Settings) -> dict:
    api = load(settings)
    canvas = request["canvas"]
    script = api.ScriptFile(canvas["width"], canvas["height"], canvas["fps"], True)
    script.add_track(api.TrackType.video, "主视频")
    cursor = 0
    for item in request["clips"]:
        mat = api.VideoMaterial(item["path"])
        if item.get("crop"):
            mat.crop_settings = api.CropSettings(**item["crop"])
        duration = item["out_us"] - item["in_us"]
        segment = api.VideoSegment(mat, api.Timerange(cursor, duration),
                                   source_timerange=api.Timerange(item["in_us"], duration),
                                   volume=item.get("volume", 1.0),
                                   clip_settings=api.ClipSettings(**item.get("transform", {})))
        script.add_segment(segment, "主视频")
        cursor += duration
    return json.loads(script.dumps())  # Only a newly constructed draft, never an imported draft.


def add_music(doc: dict, items: list, settings: Settings) -> list[str]:
    if not items:
        return []
    if any(i.get("loop") or i.get("crossfade_previous_us") for i in items):
        from . import music
        tracks, _ = music.add(doc, items, settings)
        return tracks
    api = load(settings)
    from .music import schedule
    rows = schedule(items, fps=doc.get("fps", 30))
    helper = api.ScriptFile(doc["canvas_config"]["width"], doc["canvas_config"]["height"],
                            int(doc.get("fps", 30)), True)
    for index, item in enumerate(items):
        row = rows[index]
        used = {t.get("name") for t in doc["tracks"]}
        track = f"BGM_{index + 1}"
        while track in used:
            track += "_新增"
        helper.add_track(api.TrackType.audio, track)
        material = api.AudioMaterial(item["path"])
        segment = api.AudioSegment(material, api.Timerange(row["start_us"], row["duration_us"]),
                                   source_timerange=api.Timerange(item["in_us"], row["duration_us"]),
                                   volume=item["volume"])
        if item["fade_in_us"] or item["fade_out_us"]:
            segment.add_fade(item["fade_in_us"], item["fade_out_us"])
        # Explicit, disjoint speech intervals; missing intervals do not imply silence.
        if item["ducking"]:
            points = {0: item["volume"], row["duration_us"]: item["volume"]}
            for window in item["ducking"]:
                start = window["start_us"] - row["start_us"]
                end = window["end_us"] - row["start_us"]
                points[max(0, start - window["attack_us"])] = item["volume"]
                points[start] = window["volume"]
                points[end] = window["volume"]
                points[min(row["duration_us"], end + window["release_us"])] = item["volume"]
            for offset, volume in sorted(points.items()):
                segment.add_keyframe(offset, volume)
        helper.add_segment(segment, track)
    # Export only new resources; never replace the source's entire materials object.
    for name in ("audios", "speeds", "audio_fades"):
        values = [value.export_json() for value in getattr(helper.materials, name)]
        if values:
            doc["materials"].setdefault(name, []).extend(values)
    tracks = [track.export_json() for track in helper.tracks.values()]
    doc["tracks"].extend(tracks)
    return [track["id"] for track in tracks]


def transition_payload(item: dict, settings: Settings, source: dict | None) -> dict:
    """Clone a locally evidenced transition, or one explicit native-test candidate."""
    key = item["resource_id"]
    candidates = (source or {}).get("materials", {}).get("transitions", [])
    original = next((v for v in candidates if v.get("resource_id") == key), None)
    if original is None and not settings.canary:
        evidence_path = safe_path(settings.work_root / "native_acceptance.json", exists=True)
        original = decode(evidence_path.read_bytes()).get("transition_resources", {}).get(key)
    if original:
        payload = deepcopy(original)
    elif settings.canary and key in {"6724845717472416269", "6726711499676455435", "6724845376098013708"}:
        load(settings)
        meta = importlib.import_module("pyJianYingDraft.metadata").TransitionType
        transition = importlib.import_module("pyJianYingDraft.video_segment").Transition
        name = {"6724845717472416269": "叠化", "6726711499676455435": "左移", "6724845376098013708": "闪白"}[key]
        candidate = meta[name]
        require(not candidate.value.is_vip, "unverified_transition", "Membership candidates are not enabled")
        payload = transition(candidate, item["duration_us"]).export_json()
        # Existing locally downloaded cache only. Never implicitly download.
        import os
        cache = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "User Data" / "Cache" / "effect" / payload["effect_id"]
        if key != "6724845717472416269":
            info = decode(safe_path(cache / "cache.json", exists=True).read_bytes())
            require(info.get("resource_id") == key, "unverified_transition", "Cache resource identity differs")
            uri = info["file_url"]["uri"]
            require(isinstance(uri, str) and uri.isalnum(), "unverified_transition", "Invalid cache URI")
            payload["path"] = str(safe_path(cache / uri, exists=True))
        evidence_path = settings.work_root / "canary_transition_evidence.json"
        if evidence_path.exists():
            evidence = decode(safe_path(evidence_path, exists=True).read_bytes())
            if key in evidence.get("resources", {}):
                # Admin-only native-test fixture. Read the declared owned test
                # again; never promote an enum, guessed URI or arbitrary file.
                from . import core, codec
                manifest_path = safe_path(evidence["manifest_path"], exists=True)
                require(manifest_path.parent == settings.work_root and manifest_path.name.startswith("canaries_phase2"),
                        "unverified_transition", "Candidate must belong to an owned test manifest")
                manifest = decode(manifest_path.read_bytes())
                require(any(d["name"] == evidence["draft_name"] and d["feature"] == "music_and_transition"
                            and not d["helper_only"] for d in manifest["drafts"]),
                        "unverified_transition", "Candidate is not this manifest's native music test")
                require(evidence["format_identity"] == codec.identity(), "unverified_transition", "Native test version changed")
                native = core.read_source(settings, evidence["draft_name"])
                require(native["format"] == "nested-single-11.5" and any(
                        r == evidence["resources"][key] for r in native["doc"]["materials"].get("transitions", [])),
                        "unverified_transition", "Native candidate changed since snapshot")
                payload = deepcopy(evidence["resources"][key])
    else:
        raise ToolError("unverified_transition", "Use a transition already evidenced in the current draft; no enum-only guessing")
    # New resource identity; all editor-provided fields are retained.
    import uuid
    payload["id"] = uuid.uuid4().hex
    payload["duration"] = item["duration_us"]
    return payload
