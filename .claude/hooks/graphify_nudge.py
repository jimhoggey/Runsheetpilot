#!/usr/bin/env python3
"""PreToolUse: suggest graphify when a search is genuinely exploratory.

The earlier version fired on EVERY Read and every grep of a source file,
which meant it mostly interrupted work that already knew where it was
going — opening a named file, or grepping one path for a symbol. A hook
that cries wolf on the common case gets tuned out, and is then worth
nothing on the case where it is right.

It now fires only on the shape graphify actually helps with: a repo-wide
search with no path argument ("where is X?"), and Glob, which is by
definition "find me files matching a pattern". Reading a specific file
is never intercepted.

Suggests, never blocks. Exits 0 on anything unexpected.
"""
import json
import re
import shlex
import sys
from pathlib import Path

SEARCH_TOOLS = ("grep", "rg", "ripgrep", "ack", "ag", "find", "fd")

MESSAGE = (
    "This repo has a graphify knowledge graph (graphify-out/graph.json). "
    "For a repo-wide question, `graphify query \"<question>\"` returns a "
    "scoped subgraph and is usually faster than reading the raw hits — "
    "also `graphify explain \"<symbol>\"` and `graphify path \"<A>\" "
    "\"<B>\"`. Grep stays the right tool once you know the file."
)


def _is_broad_search(command: str) -> bool:
    """True for a search with no explicit path — i.e. 'where is X?'"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False

    # Only consider the first command in a pipeline; `... | head` is
    # still the same search.
    head = []
    for tok in tokens:
        if tok in ("|", "&&", ";", "||"):
            break
        head.append(tok)
    if not head:
        return False

    tool = Path(head[0]).name
    if tool not in SEARCH_TOOLS:
        return False

    # The two families take their arguments differently, and getting this
    # wrong is what makes a hook noisy.
    if tool in ("find", "fd"):
        # `find [ROOT...] [-expr ...]` — everything before the first flag
        # is a root, and the flag VALUES that follow ("*.py" after -name)
        # are patterns, not paths. A bare `.` is find's mandatory root, so
        # it says nothing about intent.
        roots = []
        for tok in head[1:]:
            if tok.startswith("-"):
                break
            roots.append(tok)
        return all(r == "." for r in roots)

    # `grep PATTERN [PATH...]` — the first positional is the pattern; a
    # path after it means the caller already knows where to look.
    positionals = [t for t in head[1:] if not t.startswith("-")]
    for tok in positionals[1:]:
        if "/" in tok or re.search(r"\.[A-Za-z0-9]{1,5}$", tok):
            return False
    return True


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not Path("graphify-out/graph.json").exists():
        return 0

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    if tool == "Glob":
        fire = True
    elif tool == "Grep":
        # The Grep tool with no path is the same repo-wide question.
        fire = not tool_input.get("path")
    elif tool == "Bash":
        fire = _is_broad_search(str(tool_input.get("command") or ""))
    else:
        fire = False

    if not fire:
        return 0

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": MESSAGE,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
