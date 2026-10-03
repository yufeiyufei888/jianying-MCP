"""Check an explicit public file manifest; no private workspace or media scanning."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = {".git", ".local", ".tools", ".venv-jianying-mcp", ".venv", "__pycache__"}
FORBIDDEN_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".srt", ".png", ".jpg", ".jpeg", ".exe", ".dll", ".zip", ".pyc", ".pyo", ".log"}
PRIVATE_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", re.I)
TOKEN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,})")


def paths_to_check(root):
    if (root / ".git").exists():
        result = subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root),
                                 "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture_output=True, check=True)
        return set(result.stdout.decode("utf-8").strip("\0").split("\0")) - {""}
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and
            not any(part in RUNTIME_DIRS for part in p.relative_to(root).parts)}


def validate(root=ROOT):
    manifest = json.loads((root / "release-files.json").read_text(encoding="utf-8"))
    expected = manifest["files"]
    if manifest.get("schema") != "jianying-public-files/1" or len(expected) != len(set(expected)):
        raise ValueError("Invalid or duplicate whitelist")
    for name in expected:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError(f"Unsafe whitelist path: {name}")
    actual = paths_to_check(root)
    if actual != set(expected):
        raise ValueError(f"Whitelist mismatch; missing={sorted(set(expected)-actual)} extra={sorted(actual-set(expected))}")
    size = 0
    for name in sorted(actual):
        path = root / name
        if path.is_symlink() or path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name in {"native_acceptance.json", "config.local.json", ".env"}:
            raise ValueError(f"Private/runtime/binary artifact in public files: {name}")
        if any(part in RUNTIME_DIRS for part in path.relative_to(root).parts):
            raise ValueError(f"Runtime folder is tracked: {name}")
        data = path.read_bytes()
        if len(data) > 1_048_576:
            raise ValueError(f"Oversized public file: {name}")
        text = data.decode("utf-8")
        if PRIVATE_PATH.search(text) or TOKEN.search(text):
            raise ValueError(f"Private path or credential pattern: {name}")
        size += len(data)
    skill = (root / "skills/jianying-local/SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---\nname: jianying-local\n") or "description:" not in skill.split("---", 2)[1]:
        raise ValueError("Invalid skill entry metadata")
    if "Apache License" not in (root / "LICENSE").read_text(encoding="utf-8"):
        raise ValueError("Missing license")
    if json.loads((root / "config.example.json").read_text(encoding="utf-8")).get("work_root") != ".local/work":
        raise ValueError("Template must isolate work state")
    return {"status": "passed", "public_files": len(actual), "public_bytes": size,
            "private_data_scan": "tracked/whitelisted text only; not a proof against arbitrary secret formats"}


if __name__ == "__main__":
    try:
        print(json.dumps(validate(), ensure_ascii=False))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"release validation failed: {error}")
