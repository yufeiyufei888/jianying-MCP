"""Pinned native metadata codec, always in a short-lived isolated process.

Only our metadata byte snapshots cross this boundary. No native draft path,
DLL path or command is supplied by CLI/MCP callers.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .runtime import ToolError, canonical, decode, digest, file_stamp, require, safe_path, write_new

ROOT = Path(__file__).resolve().parents[2] / ".tools" / "jianying-codec" / "v0.1.1"
MAX_BYTES = 32 * 1024 * 1024
_HASHES = {}


def binary_stamp(path):
    p = safe_path(path, exists=True)
    s = p.stat()
    key = (str(p), s.st_size, s.st_mtime_ns, s.st_ino)
    if key not in _HASHES:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        _HASHES[key] = h.hexdigest()
    return {"path": str(p), "size": s.st_size, "mtime_ns": s.st_mtime_ns, "sha256": _HASHES[key]}


def identity(settings=None):
    root = settings.codec_root if settings is not None else ROOT
    lock = safe_path(root / "provenance.json", exists=True)
    data = decode(lock.read_bytes())
    require(data.get("tag") == "v0.1.1" and data.get("app_version") == "11.5.0.14471",
            "codec_unverified", "Only the pinned local codec/editor build is enabled")
    exe = safe_path(root / "jy-draftc.exe", exists=True)
    dll = safe_path(Path(data["install_dir"]) / "videoeditor.dll", exists=True)
    app = safe_path(dll.parent / "JianyingPro.exe", exists=True)
    env = safe_path(root / ".env", exists=True)
    if settings is not None:
        require(Path(data["install_dir"]) == settings.app_root, "codec_unverified", "Configured editor and codec editor differ")
    require(env.read_bytes() == ("JY_INSTALL_DIR=" + str(dll.parent) + "\n").encode("utf-8"),
            "codec_unverified", "Codec configuration changed")
    current = {"exe": binary_stamp(exe), "dll": binary_stamp(dll), "app": binary_stamp(app)}
    require(current == data["binaries"], "codec_unverified", "Codec/editor binaries changed; revalidate before use")
    return {"tag": data["tag"], "app_version": data["app_version"], "binaries": current,
            "lock_sha256": digest(lock.read_bytes()), "env_sha256": digest(env.read_bytes())}


def status(settings=None):
    try:
        return {"ready": True, "identity": identity(settings), "loads_dll_on_startup": False}
    except (ToolError, OSError) as exc:
        return {"ready": False, "reason": str(exc), "loads_dll_on_startup": False}


def process_memory(process):
    if os.name != "nt":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                    *[(n, ctypes.c_size_t) for n in ("peak_working_set", "working_set", "peak_paged", "paged",
                                                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    fn = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    fn.restype = ctypes.c_int
    if fn(ctypes.c_void_p(int(process._handle)), ctypes.byref(value), value.cb):
        return {"working_set": value.working_set, "peak_working_set": value.peak_working_set,
                "private_commit": value.pagefile, "peak_private_commit": value.peak_pagefile}
    return None


def memory_for_pid(pid):
    if os.name != "nt":
        return None
    from types import SimpleNamespace
    kernel = ctypes.WinDLL("kernel32")
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle:
        return None
    try:
        return process_memory(SimpleNamespace(_handle=handle))
    finally:
        kernel.CloseHandle(handle)


def run_monitored(command, *, cwd, timeout=30, env=None):
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result, failure = [], []
    def communicate():
        try:
            result.append(process.communicate(timeout=timeout))
        except subprocess.TimeoutExpired as exc:
            process.kill()  # Only this task's child, never the editor.
            result.append(process.communicate())
            failure.append(exc)
    thread = threading.Thread(target=communicate, daemon=True)
    thread.start()
    peak = {}
    while thread.is_alive():
        sample = process_memory(process)
        for k, v in (sample or {}).items():
            peak[k] = max(peak.get(k, 0), v)
        thread.join(0.01)
    require(not failure, "worker_timeout", "Format/analysis child timed out; retained diagnostics, no native write")
    return process.returncode, result[0][0], result[0][1], peak


def convert(data, settings, *, encrypt=False):
    require(isinstance(data, bytes) and 0 < len(data) <= MAX_BYTES, "invalid_metadata", "Metadata size is unsupported")
    pin = identity(settings)
    base = safe_path(settings.work_root / "codec_snapshots")
    base.mkdir(parents=True, exist_ok=True)
    key = digest(canonical({"cache_schema": 2, "input": digest(data), "codec": pin, "encrypt": encrypt}))
    # Decryption is deterministic. Encoding is performed once per unique input and
    # staged bytes are reused by apply, not re-encrypted during publication.
    cache = safe_path(base / (key + ".json"))
    if cache.exists():
        receipt = decode(cache.read_bytes())
        require(isinstance(receipt.get("directory"), str) and re.fullmatch(r"[0-9a-f]{32}", receipt["directory"]) and
                receipt.get("codec") == pin and receipt.get("input_sha256") == digest(data),
                "codec_cache_changed", "Codec snapshot binding changed")
        require(digest((base / receipt["directory"] / "input.bin").read_bytes()) == digest(data),
                "codec_cache_changed", "Cached metadata input changed")
        output = safe_path(base / receipt["directory"] / "output.bin", exists=True).read_bytes()
        require(digest(output) == receipt["output_sha256"], "codec_cache_changed", "Codec cache bytes changed")
        return output
    folder = safe_path(base / uuid.uuid4().hex)
    folder.mkdir()
    source, target = folder / "input.bin", folder / "output.bin"
    write_new(source, data)
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LOCALAPPDATA"}}
    try:
        code, stdout, stderr, peak = run_monitored(
            [str(settings.codec_root / "jy-draftc.exe"), "-e" if encrypt else "-d", str(source), str(target)],
            cwd=folder, env=env)
        write_new(folder / "process.json", canonical({"exit_code": code, "peak_bytes": peak,
                  "stdout": stdout.decode("utf-8", "replace")[-4096:], "stderr": stderr.decode("utf-8", "replace")[-4096:]}))
        require(code == 0 and target.is_file(), "codec_failed", "Native format conversion failed; see retained snapshot/process.json")
        require(target.stat().st_size <= MAX_BYTES, "invalid_metadata", "Codec output exceeds limit")
        output = target.read_bytes()
        if encrypt:
            require(decode(convert(output, settings)) == decode(data), "codec_roundtrip", "Metadata roundtrip changed JSON")
        else:
            require(isinstance(decode(output), dict), "invalid_metadata", "Decoded metadata is not a JSON object")
        write_new(cache, canonical({"directory": folder.name, "input_sha256": digest(data), "output_sha256": digest(output), "codec": pin}))
        return output
    except Exception as exc:
        if not (folder / "failure.json").exists():
            write_new(folder / "failure.json", canonical({"code": getattr(exc, "code", "conversion_error"), "message": str(exc)}))
        raise


def unpack(data, settings):
    try:
        return decode(data), False
    except ToolError as exc:
        if exc.code != "unreadable_json":
            raise
        return decode(convert(data, settings)), True


def pack(value, settings, encoded):
    data = canonical(value)
    return convert(data, settings, encrypt=True) if encoded else data
