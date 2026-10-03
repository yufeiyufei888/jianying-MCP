"""Strict I/O boundaries and lazy, read-only media inspection."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class ToolError(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


def require(condition, code: str, reason: str):
    if not condition:
        raise ToolError(code, reason)


def canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ToolError("invalid_input", "Only finite UTF-8 JSON values are supported") from exc


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "invalid_json", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError) as exc:
        raise ToolError("unreadable_json", "Not supported plain UTF-8 JSON; no repair attempted") from exc


def safe_path(value: str | Path, *, exists=False) -> Path:
    p = Path(value)
    require(p.is_absolute(), "invalid_path", "An absolute local path is required")
    p = Path(os.path.abspath(p))
    require(not str(p).startswith(("\\\\", "//")), "invalid_path", "Network paths are unsupported")
    for part in (p, *p.parents):
        if os.path.lexists(part):
            s = part.lstat()
            require(not part.is_symlink() and not getattr(s, "st_file_attributes", 0) &
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024),
                    "reparse_path", f"Reparse paths are unsupported: {part}")
    require(not exists or p.exists(), "missing_file", f"Missing path: {p}")
    return p


def leaf(value: str) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 120 and
            value == value.strip().strip(".") and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value),
            "invalid_name", "Draft name must be one nonempty Windows filename")
    require(value.upper().split(".")[0] not in
            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}, "invalid_name", "Reserved Windows name")
    return value


def integer(value, label: str, minimum=0) -> int:
    require(type(value) is int and value >= minimum, "invalid_input", f"{label} must be an integer >= {minimum}")
    return value


def file_stamp(path: Path, *, hash_bytes=False) -> dict:
    path = safe_path(path, exists=True)
    require(path.is_file(), "invalid_path", f"Not a file: {path}")
    s = path.stat()
    result = {"path": str(path), "size": s.st_size, "mtime_ns": s.st_mtime_ns}
    if hash_bytes:
        result["sha256"] = digest(path.read_bytes())
    else:
        # Bounded reads, not a repeated full-video/whole-disk hash. Detect common
        # same-size/restored-mtime replacements without pretending to hash all frames.
        positions = sorted({0, max(0, s.st_size // 2 - 8192), max(0, s.st_size - 16384)})
        sampled = hashlib.sha256()
        with path.open("rb") as handle:
            for offset in positions:
                handle.seek(offset)
                sampled.update(str(offset).encode("ascii") + b":" + handle.read(16384))
        result.update(sampled_sha256=sampled.hexdigest(), sample_policy="head-middle-tail-16KiB",
                      file_id=[s.st_dev, s.st_ino])
    return result


def check_stamp(stamp):
    require(file_stamp(Path(stamp["path"]), hash_bytes="sha256" in stamp) == stamp,
            "stale_input", f"File changed since preview: {stamp['path']}")


def write_new(path: Path, data: bytes):
    safe_path(path)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write(path: Path, data: bytes):
    safe_path(path)
    fd, temp = tempfile.mkstemp(prefix="jy_local_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)  # Only this invocation's mkstemp file.


PROJECT = Path(__file__).resolve().parents[2]


def local_appdata():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


@dataclass(frozen=True)
class Settings:
    # Positional compatibility with the original four-field Settings is retained.
    drafts_root: Path = field(default_factory=lambda: local_appdata() / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft")
    work_root: Path = field(default_factory=lambda: PROJECT / ".local" / "work")
    skill_root: Path = field(default_factory=lambda: Path(os.environ.get("JY_SKILL_ROOT") or codex_home() / "skills" / "jianying-editor"))
    canary: bool = False
    worker_python: Path = field(default_factory=lambda: Path(sys.executable))
    app_root: Path = field(default_factory=lambda: local_appdata() / "JianyingPro" / "Apps" / "11.5.0.14471")
    codex_config: Path = field(default_factory=lambda: codex_home() / "config.toml")
    codec_root: Path = field(default_factory=lambda: PROJECT / ".tools" / "jianying-codec" / "v0.1.1")
    config_path: Path | None = None

    @property
    def vendor(self):
        return self.skill_root / "scripts" / "vendor" / "pyJianYingDraft"

    def validate(self):
        for p in (self.drafts_root, self.work_root, self.skill_root, self.worker_python,
                  self.app_root, self.codex_config, self.codec_root):
            safe_path(p)
        require(self.drafts_root != self.work_root and
                not self.work_root.is_relative_to(self.drafts_root) and
                not self.drafts_root.is_relative_to(self.work_root),
                "invalid_path", "Work and draft roots must be separate")
        require(re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", self.worker_python.name, re.IGNORECASE),
                "invalid_config", "worker_python must identify a trusted Python executable, not a command or argument list")


CONFIG_KEYS = {"drafts_root", "work_root", "skill_root", "worker_python", "app_root", "codex_config", "codec_root"}


def load_settings(config_path=None, *, canary=False, **overrides):
    """Trusted startup configuration, never an MCP tool argument or shell command.

    Explicit CLI path overrides win; absent fields use portable system defaults.
    Config paths may be relative to that config file and use ${ENVIRONMENT}.
    """
    selected = config_path or os.environ.get("JIANYING_LOCAL_CONFIG")
    values = {}
    if selected:
        path = safe_path(Path(selected).absolute(), exists=True)
        require(path.stat().st_size <= 65536, "invalid_config", "Startup config exceeds 64 KiB")
        doc = decode(path.read_bytes())
        require(isinstance(doc, dict) and not (set(doc) - CONFIG_KEYS), "invalid_config", "Unknown startup config field")
        for key, value in doc.items():
            require(isinstance(value, str) and value.strip(), "invalid_config", f"{key} must be one path string")
            expanded = os.path.expandvars(value)
            require(not re.search(r"\$\{[^}]+\}|%[^%]+%", expanded), "invalid_config", f"Unresolved environment variable in {key}")
            p = Path(expanded).expanduser()
            values[key] = p if p.is_absolute() else path.parent / p
        values["config_path"] = path
    require(not (set(overrides) - CONFIG_KEYS), "invalid_config", "Unknown CLI path override")
    values.update({key: Path(value).absolute() for key, value in overrides.items() if value is not None})
    settings = Settings(**values, canary=canary)
    settings.validate()
    return settings


def app_pids() -> list[int]:
    require(os.name == "nt", "unsupported_host", "Native writes require Windows")
    # A system process snapshot avoids PowerShell cold-start latency and starts
    # no shell/window. Any API failure remains fail-closed, never an empty list.
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    require(handle not in (None, 0, ctypes.c_void_p(-1).value), "process_check_failed", "Cannot create process snapshot")
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    found, count = set(), 0
    try:
        ctypes.set_last_error(0)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid:
            count += 1
            require(count <= 100000, "process_check_failed", "Unexpected process snapshot size")
            if entry.szExeFile.casefold() == "jianyingpro.exe":
                require(entry.th32ProcessID > 0, "process_check_failed", "Invalid editor process identity")
                found.add(int(entry.th32ProcessID))
            ctypes.set_last_error(0)
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
        require(ctypes.get_last_error() == 18, "process_check_failed", "Process enumeration failed before normal end")  # ERROR_NO_MORE_FILES
    finally:
        require(kernel.CloseHandle(handle), "process_check_failed", "Cannot close owned process snapshot")
    return sorted(found)


def require_closed():
    require(not app_pids(), "editor_running", "Save and completely exit Jianying; no automatic window action")


@contextlib.contextmanager
def writer_lock(settings: Settings):
    # A shared Windows byte-range lock serializes our CLI/MCP instances, not the editor.
    import msvcrt
    settings.work_root.mkdir(parents=True, exist_ok=True)
    # Same native index => same lock, even if CLI callers chose different job roots.
    path = safe_path(settings.drafts_root / ".jianying-local.lock")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ToolError("busy", "Another local draft writer is active") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


_PROBES = {}


def probe(path: str | Path) -> dict:
    p = safe_path(path, exists=True)
    stamp = file_stamp(p)
    key = (str(p), stamp["size"], stamp["mtime_ns"], stamp["sampled_sha256"], tuple(stamp["file_id"]))
    if key in _PROBES:
        return _PROBES[key]
    exe = shutil.which("ffprobe")
    require(exe, "missing_runtime", "Existing FFprobe was not found on PATH")
    result = subprocess.run([exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)],
                            capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    require(result.returncode == 0, "invalid_media", f"FFprobe could not parse {p.name}")
    doc = decode(result.stdout)
    duration = float(doc.get("format", {}).get("duration", 0))
    require(math.isfinite(duration) and duration > 0, "invalid_media", "A measured positive media duration is required")
    videos = [s for s in doc.get("streams", []) if s.get("codec_type") == "video" and
              not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in doc.get("streams", []) if s.get("codec_type") == "audio"]
    result = {"stamp": stamp, "duration_us": round(duration * 1_000_000),
              "video": videos[0] if videos else None, "audio": audios[0] if audios else None}
    _PROBES[key] = result
    return result


def profile(settings: Settings) -> str:
    # Hash small code/templates, never footage, caches or site-packages.
    files = sorted(p for p in settings.vendor.rglob("*") if p.is_file() and
                   "__pycache__" not in p.parts and (p.suffix in {".py", ".json"} or "template" in p.name))
    files += sorted(Path(__file__).parent.glob("*.py"))
    rows = [(str(p.relative_to(settings.vendor)) if p.is_relative_to(settings.vendor)
             else p.name, digest(p.read_bytes())) for p in files]
    rows.append(("startup_paths", digest(canonical({key: str(getattr(settings, key)) for key in sorted(CONFIG_KEYS)}))))
    if settings.worker_python.is_file():
        rows.append(("worker_python", digest(canonical(file_stamp(settings.worker_python, hash_bytes=True)))))
    if settings.config_path is not None:
        rows.append(("startup_config", digest(settings.config_path.read_bytes())))
    return digest(canonical(rows))
