"""Latest-state resolution and narrowly scoped single-timeline copy layout."""
from __future__ import annotations
import copy
import re
import uuid
from pathlib import Path
from . import codec
from .runtime import canonical, check_stamp, decode, digest, file_stamp, require, safe_path

MAX_RESOURCE_BYTES = 20 * 1024 * 1024
ROOT_JSON = {"draft_agency_config.json", "attachment_pc_common.json", "draft_biz_config.json", "draft_virtual_store.json", "attachment_editing.json"}
TIMELINE_JSON = {"attachment_editing.json", "attachment_pc_common.json"}
ATTACHMENTS = {"attachment_id_mapping.json", "attachment_pc_timeline.json", "attachment_script_video.json", "attachment_action_scene.json", "coperate_create.json", "attachment_plugin_draft.json"}
# Observed identical empty/default editing attachment on all five native-saved
# 11.5.0.14471 canaries. Pin the complete canonical JSON, not just its filename
# or a selection of flags: populated cover/plugin/AI metadata needs a separate
# dependency adapter and must still stop. Original attachment bytes are retained.
EMPTY_EDITING_ATTACHMENT_SHA256 = "1da48245340efa0f1d661d64d012e8264e8f25c2edcec94cdc3e7cc846e2b8c8"
EMPTY_PLUGIN_ATTACHMENT = {"plugin_draft": {"plugin_segments": [], "version": "1.0.0"}}
REGENERABLE = {".backup", "performance_opt_info.json", "draft.extra", "agent_adoption_ledger.db", "agent_adoption_ledger.db-wal",
               "agent_adoption_ledger.db-shm", "template.tmp", "template-2.tmp", "adjust_mask", "matting", "qr_upload",
               "smart_crop", "deepagent"}


def tree(folder):
    result = []
    def walk(path):
        for p in sorted(path.iterdir(), key=lambda x: x.name):
            safe_path(p)
            relative = p.relative_to(folder).as_posix()
            result.append(relative + ("/" if p.is_dir() else ""))
            if p.is_dir() and p.name not in REGENERABLE and p.name != "Resources":
                walk(p)
    walk(folder)
    return result


def check_aux(raw, folder, settings):
    """Unknown auxiliary dependencies are not hidden inside a copied JSON blob."""
    from .core import walk_paths
    if "draft_settings" in raw:
        text = raw["draft_settings"].decode("utf-8-sig")
        require(text.startswith("[General]") and ":\\" not in text and ":/" not in text,
                "unknown_dependency", "Unknown draft_settings dependency")
    for name, value in raw.items():
        if not value or not name.endswith(".json") or Path(name).name in {"draft_content.json", "draft_info.json", "draft_meta_info.json"}:
            continue
        for trail, path in walk_paths(decode(value)):
            p = safe_path(path)
            require(trail[-1] in {"path", "draft_fold_path", "draft_root_path"} and
                    (p == folder or p == settings.drafts_root or
                     p.is_relative_to(folder / "Timelines") and p.is_dir()),
                    "unknown_dependency", f"Unvalidated auxiliary reference: {name}/{trail}")


def check_default_attachment(name, value):
    """Only the two actually observed dependency-free native defaults are known."""
    if name == "attachment_editing.json":
        require(digest(canonical(value)) == EMPTY_EDITING_ATTACHMENT_SHA256,
                "unknown_dependency", "Editing attachment is not the verified empty/default 11.5 schema")
    elif name == "attachment_plugin_draft.json":
        require(canonical(value) == canonical(EMPTY_PLUGIN_ATTACHMENT),
                "unknown_dependency", "Nonempty or unknown plugin attachment is unsupported")


def read_layout(settings, name):
    folder = safe_path(settings.drafts_root / name, exists=True)
    entries = tree(folder)
    project_path = folder / "Timelines" / "project.json"
    if not project_path.exists():
        # Legacy behavior remains intentionally strict. A stray new content file
        # is never resolved by mtime or by falling back to draft_info.
        from .core import files_of, small
        paths, content_name = files_of(folder)
        raw = {p.name: small(p) for p in paths}
        check_aux(raw, folder, settings)
        return {"folder": folder, "raw": raw, "doc": decode(raw[content_name]),
                "meta": decode(raw["draft_meta_info.json"]), "content_name": content_name,
                "format": "flat-360000", "encoded": {}, "project": None, "timeline_id": None,
                "source_tree": entries, "files": [file_stamp(p, hash_bytes=True) for p in paths],
                "copy_resources": {}, "resource_copy_bytes": 0, "ignored": [], "format_identity": None}
    from .core import small
    pin = codec.identity(settings)
    project = decode(small(project_path))
    rows = project.get("timelines")
    require(isinstance(rows, list) and len(rows) == 1 and not rows[0].get("is_marked_delete"),
            "unsupported_structure", "Only one active, nondeleted timeline is writable")
    tid = project.get("main_timeline_id")
    require(isinstance(tid, str) and re.fullmatch(r"[0-9A-Fa-f-]{36}", tid) and
            project.get("id") == tid == rows[0].get("id"), "unsupported_structure", "Conflicting timeline identity")
    prefix = "Timelines/" + tid + "/"
    content_name = prefix + "draft_content.json"
    primary = safe_path(folder / content_name, exists=True)
    root_content = safe_path(folder / "draft_content.json", exists=True)
    doc, encoded_doc = codec.unpack(small(primary), settings)
    root_doc, encoded_root = codec.unpack(small(root_content), settings)
    require(doc == root_doc and encoded_doc == encoded_root, "save_state_conflict", "Root and active timeline disagree; no stale-file fallback")
    meta, encoded_meta = codec.unpack(small(folder / "draft_meta_info.json"), settings)
    require(doc.get("id") == tid == meta.get("draft_id"), "unsupported_structure", "Active timeline/content/metadata identity conflict")
    require(doc.get("last_modified_platform", {}).get("app_version") == "11.5.0",
            "unsupported_editor", "Saved nested content is not the locally pinned 11.5 profile")
    raw, encoded, resources, ignored = {}, {}, {}, []
    for relative in entries:
        if relative.endswith("/"):
            continue
        p = folder / relative
        parts = Path(relative).parts
        if parts[0] in REGENERABLE or p.name.endswith(".bak") or p.name in REGENERABLE or p.name == "draft_info.json":
            ignored.append(relative)
            continue
        allowed = relative in {"draft_content.json", "draft_meta_info.json", "draft_settings", "key_value.json", "timeline_layout.json", "Timelines/project.json"}
        allowed |= relative in ROOT_JSON
        allowed |= relative.startswith(prefix) and p.name in TIMELINE_JSON and len(parts) == 3
        allowed |= p.name in ATTACHMENTS and (
            relative.startswith("common_attachment/") and len(parts) == 2 or
            relative.startswith(prefix + "common_attachment/") and len(parts) == 4)
        allowed |= relative == content_name
        if p.name == "draft_cover.jpg" and relative in {"draft_cover.jpg", prefix + "draft_cover.jpg"}:
            require(p.stat().st_size <= MAX_RESOURCE_BYTES, "resource_budget", "Cover exceeds resource budget")
            resources[relative] = p.read_bytes()
            continue
        require(allowed, "unknown_dependency", f"Unvalidated native dependency: {relative}")
        value = small(p)
        raw[relative] = value
        if relative in {"draft_content.json", content_name}:
            encoded[relative] = encoded_doc
        elif relative == "draft_meta_info.json":
            encoded[relative] = encoded_meta
        elif p.suffix == ".json" and value:
            aux = decode(value)
            check_default_attachment(p.name, aux)
            if p.name == "coperate_create.json":
                require(aux.get("roomInfo", {}).get("room_id", "") == "", "unsupported_structure", "Collaborative projects are not writable")
    # A nonempty Resources/subdraft directory would need format-specific asset
    # resolution. Detect it without copying cache/proxy payloads.
    for d in (folder / "Resources", folder / "subdraft"):
        if d.exists():
            for p in d.rglob("*"):
                safe_path(p)
                require(not p.is_file(), "unknown_dependency", f"Unresolved native asset: {p.relative_to(folder)}")
    total = sum(len(v) for v in resources.values())
    check_aux(raw, folder, settings)
    require(total <= MAX_RESOURCE_BYTES, "resource_budget", "Small resource copies exceed 20 MiB")
    files = [file_stamp(folder / k, hash_bytes=True) for k in sorted(raw.keys() | resources.keys())]
    return {"folder": folder, "raw": raw, "doc": doc, "meta": meta, "content_name": content_name,
            "format": "nested-single-11.5", "encoded": encoded, "project": project, "timeline_id": tid,
            "source_tree": entries, "files": files, "copy_resources": resources, "resource_copy_bytes": total,
            "ignored": ignored, "format_identity": pin}


def remap_aux(value, old, new, old_folder, target):
    """Only explicitly established identity/path association fields are rewritten."""
    data = copy.deepcopy(value)
    def walk(v):
        if isinstance(v, dict):
            for k, child in list(v.items()):
                if k in {"id", "main_timeline_id", "activeTimeline", "timeline_id", "draft_id"} and child == old:
                    v[k] = new
                elif k == "timelineIds" and isinstance(child, list):
                    v[k] = [new if x == old else x for x in child]
                elif k in {"draft_fold_path", "draft_root_path", "path"} and isinstance(child, str) and child:
                    p = safe_path(child)
                    if p.is_relative_to(old_folder):
                        v[k] = str(target / p.relative_to(old_folder)).replace(old, new)
                else:
                    walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)
    walk(data)
    return data


def payload(source, doc, meta, target, settings):
    if source["format"] == "flat-360000":
        value = dict(source["raw"])
        value[source["content_name"]] = canonical(doc)
        value["draft_meta_info.json"] = canonical(meta)
        return value, source["content_name"]
    old, new = source["timeline_id"], doc["id"]
    result = {}
    encoded_doc = codec.pack(doc, settings, source["encoded"][source["content_name"]])
    for k, v in source["raw"].items():
        dest = k.replace("Timelines/" + old + "/", "Timelines/" + new + "/", 1)
        if k in {source["content_name"], "draft_content.json"}:
            result[dest] = encoded_doc
        elif k == "draft_meta_info.json":
            result[dest] = codec.pack(meta, settings, source["encoded"][k])
        elif Path(k).name in {"attachment_editing.json", "attachment_plugin_draft.json"}:
            # These accepted defaults contain no draft IDs or dependency paths.
            # Copy byte-for-byte; do not reserialize or discard native sidecars.
            result[dest] = v
        elif k.endswith(".json") and v:
            aux = decode(v)
            # Unsupported path/identity fields remain intact and are checked by
            # the dependency validator; no blanket UUID/string replacement.
            aux = remap_aux(aux, old, new, source["folder"], target)
            result[dest] = canonical(aux)
        else:
            result[dest] = v
    for k, v in source["copy_resources"].items():
        result[k.replace("Timelines/" + old + "/", "Timelines/" + new + "/", 1)] = v
    return result, "Timelines/" + new + "/draft_content.json"


def fingerprint(source):
    return digest(canonical({"files": source["files"], "tree": source["source_tree"],
                             "content": digest(canonical(source["doc"])), "format": source["format_identity"]}))
