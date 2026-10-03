---
name: jianying-local
description: "Safely inspect and modify Windows Jianying native drafts through the local six-tool MCP or shared Python CLI. Use for reviewed clip-list creation, copy-only local timeline edits, explicit BGM/ducking/transitions, SRT mapping, media relinking, and draft verification; not for ASR, narrative selection or automatic export."
---

# Jianying Local

Execute mechanical changes to owned or authorized native drafts. This is a community adapter, not an official Jianying API. Travel review and narrative belong to [travel-vlog-pipeline](https://github.com/yufeiyufei888/vlogforge-ai-video-skill); do not decide shot selection or compress duration here.

## Before acting

1. Resolve `<tool-root>` and its trusted startup configuration. Read [workflow.md](references/workflow.md) before any mutation. Do not install packages, register a server or change configuration merely because this skill is invoked.
2. Prefer the available, locally verified `jianying-local` MCP. Otherwise call the same CLI under `<tool-root>/scripts/jianying_local/cli.py`; preserve UTF-8 JSON, integer microseconds and linear volume coefficients.
3. Run `doctor`. Fresh installs are unaccepted: diagnose only until the local native tests are completed. Never fabricate acceptance, copy another user's gate, add `--canary` to production, or assume CI validates editor playback.
4. Inspect latest saved state and explicit clip IDs. Keep Jianying fully exited for writes; stop if running or the source changes. Never close the editor, seize the mouse or use a stale root JSON without authorization.

## Mutation workflow

- `create`: only an already reviewed list of full originals and explicit in/out/crop parameters.
- `enrich`: append confirmed local music/ducking/transitions to a separate copy; leave existing tracks unchanged.
- `patch`: explicit insert/move/trim on ordinary continuous main video, plus follow/fixed policy for every other track; optional evidence-checked relink, SRT and music. Repair missing shots locally, not by rebuilding the main track.
- Call `plan_draft`, review the affected ranges/tracks, duration, warnings, dependencies and resource budget, then apply that exact plan ID/hash only when the requested scope authorizes it.
- Call `verify_draft` with exact checks before delivery and `after_save` semantic checks after native save/reopen. User export stays manual unless explicitly requested and separately supported.

## Non-negotiable boundaries

Full video/audio remains in place. Every edit makes a new draft/copy; preserve the original, existing IDs, parameters and unknown fields. Unknown dependency, unverified resource, complex timeline, range conflict, stale plan, naming collision or >20 MiB necessary small resources means stop and diagnose.

No arbitrary commands, arbitrary decryption, uploading, cloud music, ASR models or automatic export. Use only explicit speech windows; an SRT is not evidence of voice identity. Subtitles are editable bottom white text with black outline; flag long/overlap/partial cues instead of dropping words.

Report file validation, draft registration, editor acceptance, listening and publishing rights separately. A visible waveform is not listening proof. Failures retain generated copies, diagnostics and backups; never force restore an index or delete user media.

## References

- [workflow.md](references/workflow.md): shared startup paths, CLI examples, six calls and failure recovery.
- [Tool installation and native acceptance](https://github.com/yufeiyufei888/jianying-MCP/tree/main/docs): dependency pins, five-group tests and explicit registration/rollback.
