#!/usr/bin/env python3
"""PostToolUse guard: flag file reads/writes with no explicit encoding.

WHY THIS EXISTS — it cost a release. `Path.read_text()` uses the locale
codec, which is cp1252 on Windows. `server.py` and `parsing/ai.py` both
contain UTF-8 (⚠ — ✅), so tests/test_stats.py passed on macOS and on the
Linux PR runners, then died with UnicodeDecodeError on the Windows build
gate and blocked v2.9.0. The Windows job is the ONLY place this class of
bug shows up, and it runs last.

Reads the PostToolUse payload on stdin, warns on stdout. Never blocks —
JSON round-trips are genuinely safe (json.dumps escapes non-ASCII), so
this is advice, not a gate.
"""
import json
import re
import sys
from pathlib import Path

# `open(...)` in binary mode takes no encoding, and these helpers are
# fine as-is; everything else that touches text should say so explicitly.
_RISKY = re.compile(
    r"\.(?:read_text|write_text)\(\s*(?![^)]*encoding=)"
    r"|(?<![\w.])open\(\s*(?![^)]*[\"']\s*[rwax]?b)(?![^)]*encoding=)")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or payload
    path_str = str(tool_input.get("file_path") or "")
    if not path_str.endswith(".py"):
        return 0

    path = Path(path_str)
    try:
        source = path.read_text(encoding="utf-8")
    except Exception:
        return 0

    hits = []
    for lineno, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _RISKY.search(line):
            hits.append(f"  {path.name}:{lineno}  {stripped[:88]}")

    if not hits:
        return 0

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            "ENCODING CHECK — these read or write text without an explicit "
            "encoding, so they use the locale codec (cp1252 on Windows) and "
            "can raise UnicodeDecodeError there while passing on macOS and "
            "Linux. This exact bug failed the Windows build gate and blocked "
            "v2.9.0.\n" + "\n".join(hits[:12]) +
            "\n\nAdd encoding=\"utf-8\" unless the call is binary mode or "
            "round-trips JSON (json.dumps escapes non-ASCII, so those are "
            "safe). Advisory only — nothing is blocked."),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
