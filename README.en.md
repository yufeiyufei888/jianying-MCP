# jianying-MCP

[中文](README.md) · [Installation](docs/install.md) · [API](docs/api.md) · [Native acceptance](docs/native-acceptance.md) · [Compatibility](docs/compatibility.md)

A Windows-first local Jianying draft CLI, thin stdio MCP, and self-authored [`jianying-local` calling skill](skills/jianying-local/SKILL.md). **Independent community tool, not an official Jianying/CapCut API.** No cloud editor, automatic export, or arbitrary command execution.

Full original video/audio files remain in place. Mutations always create a new draft or independent copy, preserve manual edits and unknown fields, and never fix a missing shot by relaying the entire timeline.

## Capabilities

- `create`: reviewed original-media ranges, ordered clips, rectangular crops and transforms.
- `enrich`: append local BGM, fades, explicit speech ducking and named adjacent transitions to a copy.
- `patch`: insert, move or trim ordinary continuous-main-track clips with explicit follow/fixed policies for every companion track.
- Explicit BGM loops and crossfades; streaming loudness/peak measurements, without automatic normalization.
- Timeline SRT or per-original SRT mapped to each actual usage; independent editable bottom subtitle tracks.
- Evidence-checked file/directory relinking without disk-wide search or media movement.
- Legacy plain schema and pinned single-timeline nested saved-state reading, not a stale root JSON fallback.

No ASR models, cloud music, uploads, arbitrary decryption endpoint, automatic export or startup service. Multiple timelines, compound/reversed clips and complex retiming remain unsupported. Necessary small draft resources are capped at **20 MiB per copy**; unknown dependencies fail closed.

[VlogForge AI](https://github.com/yufeiyufei888/vlogforge-ai-video-skill) owns travel review, narrative and selection rules. This repository owns mechanical execution and does not duplicate that complete skill or redistribute the upstream `jianying-editor`.

## Interface and startup

The same six operations serve CLI and MCP: `doctor`, `list_drafts`, `inspect_draft`, `plan_draft`, `apply_plan`, `verify_draft`. UTF-8 JSON, integer microseconds, linear volume coefficients. Frozen previews bind source, media, configuration and implementation fingerprints. Stale plans, running editor, naming conflicts and unverified dependencies stop writes; repeated requests reuse the original receipt.

```powershell
git clone https://github.com/yufeiyufei888/jianying-MCP.git
Set-Location .\jianying-MCP
Copy-Item .\config.example.json .\config.local.json # only if absent
py -3.12 -I -B -X utf8 .\scripts\jianying_local\cli.py doctor --config .\config.local.json
```

Tested baseline: Windows 10/11, Python 3.12, existing FFmpeg/FFprobe. Backend source, Jianying, DLLs and codec binaries are **not bundled**; consult [THIRD_PARTY.md](THIRD_PARTY.md). A fresh installation is **unaccepted**. Follow [installation](docs/install.md) and the [native acceptance procedure](docs/native-acceptance.md) before production writes or explicit global registration. The pinned official `mcp==1.26.0` SDK lives in a separate environment. No WSL or speech-recognition installation is needed.

Registration backs up and adds only the owned `jianying-local` entry; it never silently replaces an existing server. [Rollback](docs/rollback.md) removes that entry only, without deleting drafts, media or later user changes.

## Verification and publication

```powershell
py -3.12 -B -X utf8 .\scripts\validate_release.py
py -3.12 -B -X utf8 -m unittest discover -s .\scripts\tests -v
& '<SDK Python>' -I -B -X utf8 .\scripts\jianying_local\smoke_readonly.py
```

All previous 108 tests, including 11 reference regressions, are retained. Additional checks cover startup paths, fresh-install gates, public-file boundaries and SDK transport. Missing external backend causes 14 explicit dependency skips; unavailable Windows symlink privilege causes one additional skip. CI uses synthetic metadata only; it neither launches Jianying nor establishes native write compatibility. See the [compatibility report](docs/compatibility.md).

An explicit [file whitelist](release-files.json) excludes private media, music, real drafts/subtitles/screenshots, logs, receipts, credentials, environments and binaries. File validation, editor acceptance, actual listening and music publishing rights remain separate. A visible waveform is not listening evidence.

Self-authored content: [Apache-2.0](LICENSE). External dependencies retain their licenses. Use only owned/authorized drafts; no application patching, account restriction bypass or subscription bypass.
