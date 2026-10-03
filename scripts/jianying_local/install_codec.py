"""Administrator provisioning: one checksum-locked portable codec, no toolchain."""
from __future__ import annotations
import io
import argparse
import base64
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.jianying_local.codec import ROOT, binary_stamp
from scripts.jianying_local.runtime import canonical, decode, digest, load_settings, require, require_closed, safe_path, write_new

URL = "https://github.com/wenshui330/jy-draftc/releases/download/v0.1.1/jy-draftc-amd64-windows.zip"
SHA = "63546a8f6013e860e825954fdf755a29751963cd56eca8f83e26771485c53dba"

def fetch(url):
    if os.name == "nt":
        require(url.startswith(("https://github.com/wenshui330/", "https://raw.githubusercontent.com/wenshui330/",
                                "https://api.github.com/repos/wenshui330/")) and "'" not in url,
                "invalid_url", "Only fixed public upstream release URLs are permitted")
        # Use Windows' existing HTTP/proxy stack; no proxy setup or browser launch.
        code = "$ErrorActionPreference='Stop'; $jyWeb=Invoke-WebRequest -UseBasicParsing -TimeoutSec 25 -Uri '" + url + "'; [Convert]::ToBase64String($jyWeb.RawContentStream.ToArray())"
        response = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                                  capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
        require(response.returncode == 0, "download_failed", "Windows HTTP fetch failed for pinned public resource")
        value = base64.b64decode(response.stdout.strip(), validate=True)
        require(len(value) <= 4 * 1024 * 1024, "download_limit", "Release resource exceeds expected limit")
        return value
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "jianying-local/2 codec-provision"}), timeout=30) as response:
        value = response.read(4 * 1024 * 1024 + 1)
    require(len(value) <= 4 * 1024 * 1024, "download_limit", "Release resource exceeds expected limit")
    return value

def main(settings):
    global ROOT
    ROOT = settings.codec_root
    app = settings.app_root
    require_closed()
    require(app.name == "11.5.0.14471", "unsupported_editor", "Only the pinned editor build is supported")
    require(not ROOT.exists(), "target_exists", "Codec already provisioned; do not overwrite")
    archive = fetch(URL)
    require(digest(archive) == SHA, "checksum_failed", "Published release checksum mismatch")
    release = decode(fetch("https://api.github.com/repos/wenshui330/jy-draftc/releases/tags/v0.1.1"))
    asset = next(a for a in release["assets"] if a["name"] == "jy-draftc-amd64-windows.zip")
    require(asset["digest"] == "sha256:" + SHA and asset["browser_download_url"] == URL,
            "checksum_failed", "Published GitHub release digest mismatch")
    license_data = fetch("https://raw.githubusercontent.com/wenshui330/jy-draftc/v0.1.1/LICENSE")
    source = fetch("https://raw.githubusercontent.com/wenshui330/jy-draftc/v0.1.1/src/jy-draftc.cpp")
    files = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for member in z.infolist():
            p = Path(member.filename)
            require(not p.is_absolute() and ".." not in p.parts and ":" not in member.filename,
                    "unsafe_archive", "Release contains unsafe paths")
            require(member.file_size <= 4 * 1024 * 1024, "download_limit", "Release entry too large")
            if member.filename.endswith("jy-draftc.exe"):
                require("jy-draftc.exe" not in files, "unsafe_archive", "Ambiguous executable")
                files["jy-draftc.exe"] = z.read(member)
    require("jy-draftc.exe" in files, "unsafe_archive", "Expected executable missing")
    for p in (app / "JianyingPro.exe", app / "videoeditor.dll"):
        safe_path(p, exists=True)
    ROOT.mkdir(parents=True)
    for name, value in {**files, "LICENSE": license_data, "reviewed_source.cpp": source,
                        ".env": ("JY_INSTALL_DIR=" + str(app) + "\n").encode("utf-8")}.items():
        write_new(ROOT / name, value)
    pin = {"tag": "v0.1.1", "app_version": app.name, "install_dir": str(app), "release_url": URL,
           "release_sha256": SHA, "reviewed_source_sha256": digest(source),
           "binaries": {"exe": binary_stamp(ROOT / "jy-draftc.exe"), "dll": binary_stamp(app / "videoeditor.dll"),
                        "app": binary_stamp(app / "JianyingPro.exe")}}
    write_new(ROOT / "provenance.json", canonical(pin))
    print(canonical({"installed": str(ROOT), "files": len(list(ROOT.iterdir())),
                     "bytes": sum(p.stat().st_size for p in ROOT.iterdir()), "release_sha256": SHA}).decode())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    main(load_settings(parser.parse_args().config))
