"""One UTF-8 JSON request/result, fixed operations, no arbitrary shell or GUI."""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

# Allows `python -I -B absolute/cli.py`: ignore injected PYTHONPATH/cwd.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.jianying_local.core import OPERATIONS, dispatch
from scripts.jianying_local.runtime import CONFIG_KEYS, ToolError, canonical, decode, load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("--config", type=Path, help="Trusted shared startup JSON; never accepted by an MCP tool")
    parser.add_argument("--request-file", type=Path, help="Read JSON arguments from a file instead of stdin")
    for key in sorted(CONFIG_KEYS):
        parser.add_argument("--" + key.replace("_", "-"), type=Path)
    parser.add_argument("--canary", action="store_true", help="Explicit native compatibility test only; never exposed by MCP")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, canary=args.canary, **{k: getattr(args, k) for k in CONFIG_KEYS})
        raw = args.request_file.read_bytes() if args.request_file else sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ToolError("invalid_input", "Request exceeds 32 MiB")
        request = decode(raw) if raw.strip() else {}
        # Third-party payload code cannot contaminate stdout JSON/protocol.
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(settings, args.operation, request)
        output = {"ok": True, "result": result}
        code = 0
    except ToolError as exc:
        output = {"ok": False, "error": {"code": exc.code, "message": exc.reason}}
        code = 2
    except Exception as exc:
        output = {"ok": False, "error": {"code": "unexpected_error", "message": str(exc), "type": type(exc).__name__}}
        code = 3
    sys.stdout.buffer.write(canonical(output) + b"\n")
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
