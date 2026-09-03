#!/usr/bin/env python3
"""PostToolUse router: name the right reviewer when a risky file is touched.

The operating principle this encodes: small changes should move fast, large
or consequential ones should be thought through. Most edits in this repo are
small and want no ceremony at all — this hook stays silent for them.

A handful of files are different. Each one below has a specific, expensive
failure mode that has actually happened, or that no test can catch:

  stats.py / stats.track()  analytics is the one place operator content can
                            leave the machine. A scrubber bug once leaked
                            spaced media filenames.
  build scripts + release   build_mac.sh and release.yml drifted apart and
                            shipped a DMG missing the Local Network key, so
                            Service Mate could not reach any clock. The same
                            drift silently dropped certifi, pypdfium2 and the
                            whole OCR stack from both platforms.
  updater.py                a bad release auto-installs itself onto every
                            machine. There is no undo.
  licensing.py              signed with a key that lives outside the repo;
                            a verification bug means reissuing every licence.
  net.py / clocks           reaches the operator's LAN. Past CodeQL findings
                            for SSRF live here.
  static/ + templates/      no test covers 4,500 lines of UI.

Silent unless one of those is touched. Advisory only — it never blocks.
"""

import json
import re
import sys


# Claude's own config lives under .claude/hooks|agents|skills|commands.
# Do NOT test for "/.claude/" alone: git worktrees live at
# .claude/worktrees/<name>/, so every source file in a worktree contains
# that substring and the guard silently disables the hook for the whole
# worktree.
_CFG = ("/.claude/hooks/", "/.claude/agents/",
        "/.claude/skills/", "/.claude/commands/")


def _is_claude_config(path_str: str) -> bool:
    return any(seg in path_str for seg in _CFG)


# (path fragment, reviewer, why it matters)
ROUTES = (
    ("propresenterrunsheet/stats.py", "privacy-auditor",
     "the EVENTS tuple is the whole privacy surface — a value added here "
     "leaves the operator's machine"),
    ("propresenterrunsheet/logging_setup.py", "privacy-auditor",
     "log scrubbing is what keeps runsheet content out of crash reports"),
    ("build_mac.sh", "frozen-build-reviewer",
     "flags here must stay in sync with the macOS job in release.yml — "
     "tests/test_release_workflow.py enforces it, run it"),
    ("build_win.bat", "frozen-build-reviewer",
     "flags here must stay in sync with the Windows job in release.yml — "
     "tests/test_release_workflow.py enforces it, run it"),
    (".github/workflows/release.yml", "frozen-build-reviewer",
     "this file builds the binaries the public downloads; it does NOT run "
     "the build scripts, so every flag exists twice"),
    ("propresenterrunsheet/updater.py", "frozen-build-reviewer",
     "a bad release auto-installs onto every machine and cannot be undone"),
    ("propresenterrunsheet/licensing.py", None,
     "signed with a key stored outside the repo; a verification bug means "
     "reissuing every customer licence"),
    ("propresenterrunsheet/propresenter/net.py", None,
     "this reaches the operator's LAN — keep the resolve-and-pin SSRF "
     "boundary intact"),
    ("static/", "frontend-reviewer",
     "no test covers the front end"),
    ("templates/", "frontend-reviewer",
     "no test covers the front end"),
)

_TRACK = re.compile(r"stats\.track\s*\(")


def advise(path_str: str, new_text: str):
    hits = []
    for fragment, agent, why in ROUTES:
        if fragment in path_str:
            hits.append((agent, why))
    # A stats.track() call added anywhere is a privacy change wherever it lives.
    if not any(a == "privacy-auditor" for a, _ in hits) and _TRACK.search(new_text or ""):
        hits.append(("privacy-auditor",
                     "this adds a stats.track() call — every prop it sends "
                     "leaves the machine"))
    return hits


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or {}
    path_str = str(tool_input.get("file_path") or "")
    if not path_str or _is_claude_config(path_str):
        return 0
    body = str(tool_input.get("content")
               or tool_input.get("new_string") or "")

    hits = advise(path_str, body)
    if not hits:
        return 0

    lines = []
    for agent, why in hits:
        lead = f"Consider the {agent} subagent" if agent else "Take care"
        lines.append(f"  {lead} — {why}.")
    seen, uniq = set(), []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            uniq.append(ln)

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            "risk-radar — this file has an expensive failure mode.\n"
            + "\n".join(uniq)
            + "\n\nSmall, obvious edits here still don't need ceremony. "
              "This fires because the cost of being wrong is high, not "
              "because the change is large."),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
