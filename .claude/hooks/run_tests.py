#!/usr/bin/env python3
"""PostToolUse: run the suite after a Python edit, report only failures.

The suite is ~420 tests in about half a second, so there is no reason to
wait for CI to learn a change broke something. Silent on success —
a hook that talks when nothing is wrong gets ignored, and then it is
worth nothing when something is.

Skips itself for edits under .claude/ (config, not app code) and when
pytest isn't importable, so a fresh clone never sees noise.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TIMEOUT_S = 90


def _python() -> str:
    """Prefer a venv that actually has pytest + the app's deps."""
    for candidate in (REPO / ".venv/bin/python", Path("/tmp/rpvenv/bin/python")):
        if candidate.exists():
            return str(candidate)
    return sys.executable


# Claude's own config lives under .claude/hooks|agents|skills|commands.
# Do NOT test for "/.claude/" alone: git worktrees live at
# .claude/worktrees/<name>/, so every source file in a worktree contains
# that substring and the guard silently disabled this hook for the whole
# worktree. Match the config subdirectories instead — that stays correct
# even for a hook file nested inside a worktree.
_CFG = ("/.claude/hooks/", "/.claude/agents/",
        "/.claude/skills/", "/.claude/commands/")


def _is_claude_config(path_str: str) -> bool:
    return any(seg in path_str for seg in _CFG)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or payload
    path_str = str(tool_input.get("file_path") or "")
    if not path_str.endswith(".py") or _is_claude_config(path_str):
        return 0
    if not Path(path_str).exists():
        return 0                      # edit failed, or a phantom path
    # Never run the suite from inside the suite — tests/test_claude_hooks.py
    # exercises this script, and without this guard that recurses.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0

    python = _python()
    env = dict(os.environ, RUNSHEET_PILOT_STATS="0")
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "-p", "no:warnings",
             "--tb=no", "-x"],
            cwd=REPO, capture_output=True, text=True,
            timeout=TIMEOUT_S, env=env)
    except Exception:
        return 0                      # no pytest, no venv, no problem

    if proc.returncode == 0:
        return 0                      # silent on green

    tail = [ln for ln in (proc.stdout or "").splitlines()
            if ln.startswith(("FAILED", "ERROR")) or " failed" in ln]
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            "TESTS FAILING after this edit:\n"
            + "\n".join(tail[-8:] or ["(see pytest output)"])
            + "\n\nFix before moving on — the suite is the contract this "
              "codebase is built on."),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
