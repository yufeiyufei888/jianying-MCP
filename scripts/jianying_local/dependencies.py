"""Resolve only named dependencies; missing media is diagnosable, not fatal to parsing."""
from __future__ import annotations
import copy
from pathlib import Path
from .runtime import canonical, decode, digest, file_stamp, probe, require, safe_path, write_new


GENERATED_COVERS = {"draft_cover.jpg", "draft_cover.png"}


def missing_generated_cover(meta, folder):
    """Only the editor's exact relative catalog-thumbnail placeholder is optional.

    It is not a rendering asset. The containing draft tree is bound to the plan,
    so appearance of a real cover after preview invalidates the source snapshot.
    Absolute covers, other filenames and all content-level assets stay strict.
    """
    value = meta.get("draft_cover")
    if not isinstance(value, str) or value not in GENERATED_COVERS:
        return []
    path = safe_path(folder / value)
    return [value] if not path.exists() else []


def inventory(doc, meta, folder, settings, *, allow_missing=False, persist=True, probe_fn=None, copied_resources=None,
              allow_missing_generated_cover=False):
    from .core import walk_paths
    media, deps, missing = {}, [], []
    paths = set()
    history = settings.work_root / "media_evidence"
    for kind in ("videos", "audios"):
        for mat in doc["materials"].get(kind, []):
            require(mat.get("type") not in {"compound", "draft", "combination"}, "unsupported_structure", "Compound media is not supported")
            p = safe_path(mat.get("path", ""))
            require(not p.is_relative_to(settings.drafts_root), "internal_dependency", "Video/audio inside a native project is outside no-copy scope")
            key = str(p)
            paths.add(key)
            if not p.is_file():
                missing.append({"material_id": mat["id"], "kind": kind, "path": key})
                continue
            info = (probe_fn or probe)(p)
            require(info["video"] if kind == "videos" else info["audio"], "invalid_media", "Expected media stream missing")
            require(abs(mat.get("duration", 0) - info["duration_us"]) <= 100_000,
                    "unsupported_structure", "Material duration disagrees with complete original")
            media[key] = info
            if persist:
                history.mkdir(parents=True, exist_ok=True)
                evidence = history / (digest(key.encode("utf-8")) + ".json")
                # Versioned evidence, never overwrite an earlier matching receipt.
                if not evidence.exists():
                    write_new(evidence, canonical(info))
    text_contents = [decode(m["content"].encode("utf-8")) for m in doc["materials"].get("texts", []) if m.get("content")]
    for data, is_meta in [(doc, False), (meta, True), *((v,False) for v in text_contents)]:
        references = list(walk_paths(data))
        # walk_paths deliberately does not treat every relative string as a
        # dependency. Resolve this one known metadata field explicitly.
        if is_meta and isinstance(meta.get("draft_cover"), str) and meta["draft_cover"] in GENERATED_COVERS:
            entry = (("draft_cover",), meta["draft_cover"])
            if entry not in references:
                references.append(entry)
        for trail, value in references:
            if is_meta and trail in {("draft_fold_path",), ("draft_root_path",)}:
                continue
            if trail == ("path",) and value == "":
                continue
            generated_cover = is_meta and trail == ("draft_cover",) and value in GENERATED_COVERS
            if generated_cover:
                p = safe_path(folder / value)
            else:
                p = safe_path(value)
            key = str(p)
            if key in paths:
                continue
            # Case/slash variants in the native media-bin are references to the
            # already inventoried source, not independent dependencies.
            if any(key.casefold() == x.casefold() for x in paths):
                continue
            is_cover = trail[-1] in {"static_cover_image_path", "draft_cover", "cover_path"}
            is_font = (trail[-1] == "font_path" or len(trail) >= 2 and trail[-2:] == ("font", "path")) and p.suffix.lower() in {".ttf", ".ttc", ".otf"}
            is_effect = len(trail) >= 4 and trail[0] == "materials" and trail[1] in {"transitions", "video_effects", "text_effects"} and trail[-1] == "path"
            require(is_cover or is_font or is_effect, "unknown_dependency", f"Unvalidated dependency field: {trail}")
            if generated_cover and allow_missing_generated_cover and not p.exists():
                continue  # Listed in preview; only a missing flat catalog thumbnail.
            require(p.exists(), "missing_dependency", f"Missing non-media dependency: {p}")
            if is_cover and p.is_relative_to(folder):
                require(p.name in {"draft_cover.jpg", "draft_cover.png"}, "unknown_dependency", "Unknown internal cover")
                relative = p.relative_to(folder).as_posix()
                require(relative in (copied_resources or {}), "internal_dependency",
                        "Internal cover has not been bound and budgeted by the layout adapter")
                continue  # Native layout adapter binds and budgets these bytes.
            require(not p.is_relative_to(settings.drafts_root), "internal_dependency", "Unknown internal dependency")
            if p.is_dir():
                require(is_effect, "unknown_dependency", "Only validated effect fields may reference directories")
                values = sorted(p.rglob("*"))
                require(len(values) <= 2000, "dependency_limit", "External effect dependency tree is too large")
                for child in values:
                    safe_path(child)
                    if child.is_file():
                        require(child.stat().st_size <= 32 * 1024 * 1024, "dependency_limit", "Effect dependency too large")
                        deps.append(file_stamp(child, hash_bytes=True))
                require(any(x.is_file() for x in values), "missing_dependency", "Effect cache is empty")
                deps.append({"directory": str(p), "entries": [x.relative_to(p).as_posix() for x in values]})
            else:
                deps.append(file_stamp(p, hash_bytes=True))
            paths.add(key)
    require(allow_missing or not missing, "missing_media", "Missing original media; use explicit relink preview")
    return media, deps, missing


def check_dependency(value):
    from .runtime import check_stamp
    if "directory" in value:
        p = safe_path(value["directory"], exists=True)
        current = [x.relative_to(p).as_posix() for x in sorted(p.rglob("*"))]
        require(current == value["entries"], "stale_input", "Effect dependency layout changed")
    else:
        check_stamp(value)


def media_signature(info):
    def video(v):
        return {k: v.get(k) for k in ("codec_name", "width", "height", "pix_fmt", "r_frame_rate", "avg_frame_rate")} if v else None
    def audio(a):
        return {k: a.get(k) for k in ("codec_name", "sample_rate", "channels")} if a else None
    return {"size": info["stamp"]["size"], "sampled_sha256": info["stamp"]["sampled_sha256"],
            "duration_us": info["duration_us"], "video": video(info["video"]), "audio": audio(info["audio"])}


def relink(doc, meta, request, settings):
    from .core import array, obj
    obj(request, (), ("files", "directories"), "relinks")
    pairs = copy.deepcopy(array(request.get("files", []), "relink.files", maximum=10000))
    directory_maps = array(request.get("directories", []), "relink.directories", maximum=100)
    materials = [m for k in ("videos", "audios") for m in doc["materials"].get(k, [])]
    for mapping in directory_maps:
        obj(mapping, ("old_dir", "new_dir"), ("confirm_unverified",), "directory relink")
        old, new = safe_path(mapping["old_dir"]), safe_path(mapping["new_dir"], exists=True)
        require(new.is_dir(), "invalid_path", "New media root must be a directory")
        # Several material IDs may legitimately reference one full original.
        # Deduplicate within this directory rule, not conflicting user rules.
        mapped_paths = set()
        for m in materials:
            p = safe_path(m["path"])
            if p.is_relative_to(old) and str(p).casefold() not in mapped_paths:
                mapped_paths.add(str(p).casefold())
                pairs.append({"old_path": str(p), "new_path": str(new / p.relative_to(old)),
                              "confirm_unverified": mapping.get("confirm_unverified", False)})
    updates, seen = [], set()
    for pair in pairs:
        obj(pair, ("old_path", "new_path"), ("confirm_unverified",), "file relink")
        require(type(pair.get("confirm_unverified", False)) is bool, "invalid_input", "Confirmation must be boolean")
        old, new = safe_path(pair["old_path"]), safe_path(pair["new_path"], exists=True)
        require(str(old).casefold() not in seen, "ambiguous_relink", "Multiple mappings target the same original")
        seen.add(str(old).casefold())
        matches = [m for m in materials if safe_path(m["path"]) == old]
        require(matches, "unmatched_relink", "Mapping does not identify any original material")
        require(old.suffix.lower() == new.suffix.lower(), "media_mismatch", "Media types differ")
        current = probe(new)
        evidence_path = settings.work_root / "media_evidence" / (digest(str(old).encode()) + ".json")
        evidence = probe(old) if old.is_file() else decode(evidence_path.read_bytes()) if evidence_path.exists() else None
        if evidence:
            require(media_signature(evidence) == media_signature(current), "media_mismatch", "New media is not the evidenced original; same filename is insufficient")
            strength = "bounded_sample_and_probe"
        else:
            require(pair.get("confirm_unverified") is True, "insufficient_evidence", "Original missing without historical fingerprint; explicit confirmation required")
            strength = "explicit_user_confirmation_without_full_identity_proof"
        for m in matches:
            require(abs(m["duration"] - current["duration_us"]) <= 100_000, "media_mismatch", "Duration differs from saved original")
            for k in ("width", "height"):
                if m.get(k):
                    require(current["video"] and current["video"][k] == m[k], "media_mismatch", "Resolution differs")
        # Only established material path and native-bin file_Path associations.
        for m in matches:
            m["path"] = str(new)
        for group in meta.get("draft_materials", []):
            for row in group.get("value", []):
                if row.get("file_Path") and safe_path(row["file_Path"]) == old:
                    row["file_Path"] = str(new)
        updates.append({"old_path": str(old), "new_path": str(new), "material_ids": [m["id"] for m in matches],
                        "evidence": strength, "new_media": current, "old_media": evidence if old.exists() else None})
    return updates
