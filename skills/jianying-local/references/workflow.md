# Mechanical workflow

Resolve the repository root separately from this optional installed skill. A trusted user startup config determines drafts/work/backend/worker/application/codec/Codex paths. Tool-call arguments cannot replace commands or roots.

## Calls

1. `doctor {}`: supported version, editor state, native gate. If missing runtime, explain and request installation choice; no implicit download.
2. `list_drafts {limit}` then `inspect_draft {name,limit,cursor?}`: choose source by exact identity, inspect latest content source, actual track/segment IDs and missing dependencies. A cursor is tied to one saved fingerprint.
3. `plan_draft {request:<create/enrich/patch request>}` through MCP; CLI receives the request itself. Use an operation UUID. Existing source and new target names must differ. Do not invent a short target duration.
4. Present original/changed ranges, affected companion tracks, warnings and duration; adding music requires explicit local file and no inferred publishing rights. Ducking needs actual provided speech intervals.
5. `apply_plan {plan_id,expected_plan_sha256}`: only when user change authorization covers the preview and editor is exited. Source/index/media/config changes stop; never retry under a new UUID blindly after interruption.
6. `verify_draft {name,plan_id,validation:"exact"}`: exact generated files and originals unchanged. After user saves/reopens/exits, repeat with `validation:"after_save"`.

CLI example (replace placeholders, use an existing Python):

```powershell
& '<worker-python>' -I -B -X utf8 '<tool-root>\scripts\jianying_local\cli.py' inspect_draft --config '<trusted-config>' --request-file '<request-file>'
```

Requests and result envelopes are documented in the repository `docs/api.md` with JSON examples. Relative config paths are resolved against the config file; full media paths are preferred. Do not edit source media, plans or receipts to bypass a failed check.

## Native gate / recovery

Fresh installations remain pending. Local canary scripts are explicit administrative tests, not MCP tools, and need permission to create separate tests. All five native capability groups must be verified; a code/test count alone does not activate production writes.

When dependencies are opaque, resources missing, companions cross discontinuous edits, or a relink identity is ambiguous, stop with exact missing evidence. Do not rebuild the original using a high-level loader. Keep failed target, local receipt and index backup; manually inspect uncertain registration before any recovery.

Never register globally as a side effect of editing. Explicit registration/rollback uses the same trusted config and removes only its owned service block, not drafts, media or peer services.
