"""Tests for the Claude Code hooks in .claude/hooks/.

A hook that silently stops firing is worse than no hook: you keep
trusting a guard that isn't there. These are cheap, so they run with
everything else.

The behaviour that matters is as much about staying QUIET as firing.
The graphify nudge's predecessor fired on every Read and every grep,
which trained everyone to ignore it; the value is entirely in the
precision.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


# The graphify hook only speaks when graphify-out/graph.json exists, and
# that directory is gitignored — so on a CI checkout it is absent and the
# hook is correctly silent. Tests therefore run from a tmp cwd carrying a
# stand-in graph, which makes them independent of whether the developer
# happens to have built one.
@pytest.fixture(scope="module")
def graphed(tmp_path_factory):
    root = tmp_path_factory.mktemp("graphed")
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
    return root


def _run(script: str, payload: dict, cwd: Path = None) -> str:
    """Run a hook with `payload` on stdin; return stdout."""
    proc = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(cwd or HOOKS.parent.parent), timeout=120)
    assert proc.returncode == 0, f"{script} exited {proc.returncode}"
    return proc.stdout


def _fired(out: str) -> bool:
    return "hookSpecificOutput" in out


# ── graphify nudge: precision is the whole point ─────────────────────────

def _bash(cmd, cwd):
    return _fired(_run("graphify_nudge.py",
                       {"tool_name": "Bash", "tool_input": {"command": cmd}},
                       cwd=cwd))


def test_no_graph_means_no_nudge(tmp_path):
    """The hook is for repos that have a graph; everywhere else it is
    invisible. This is why the other tests need a stand-in graph."""
    assert not _bash('grep -rn "anything"', cwd=tmp_path)


@pytest.mark.parametrize("cmd", [
    'grep -rn "fetch_media_bin"',
    'rg "def track"',
    'find . -name "*.py"',          # `.` is find's mandatory root, not intent
    'fd -e py',
])
def test_repo_wide_searches_get_the_nudge(cmd, graphed):
    assert _bash(cmd, cwd=graphed), cmd


@pytest.mark.parametrize("cmd", [
    'grep -n "def main" propresenterrunsheet/server.py',
    'grep -c stats propresenterrunsheet/routes/parse.py',
    'find propresenterrunsheet -name "*.py"',
    'pytest -q',
    'git status',
    'python3 -c "print(1)"',
])
def test_targeted_and_unrelated_commands_stay_quiet(cmd, graphed):
    assert not _bash(cmd, cwd=graphed), cmd


def test_glob_is_exploratory_by_definition(graphed):
    assert _fired(_run("graphify_nudge.py",
                       {"tool_name": "Glob", "tool_input": {"pattern": "**/*.py"}},
                       cwd=graphed))


def test_grep_tool_fires_only_without_a_path(graphed):
    assert _fired(_run("graphify_nudge.py", {
        "tool_name": "Grep", "tool_input": {"pattern": "stats.track"}},
        cwd=graphed))
    assert not _fired(_run("graphify_nudge.py", {
        "tool_name": "Grep",
        "tool_input": {"pattern": "x", "path": "propresenterrunsheet/stats.py"}},
        cwd=graphed))


def test_reading_a_named_file_is_never_intercepted(graphed):
    """The predecessor fired here, on every source Read. That is the
    common case and it already knows where it is going."""
    assert not _fired(_run("graphify_nudge.py", {
        "tool_name": "Read",
        "tool_input": {"file_path": "propresenterrunsheet/stats.py"}},
        cwd=graphed))


# ── encoding guard ───────────────────────────────────────────────────────

def test_encoding_guard_flags_a_locale_codec_read(tmp_path):
    risky = tmp_path / "risky.py"
    risky.write_text("from pathlib import Path\n"
                     "data = Path('x').read_text()\n", encoding="utf-8")
    out = _run("check_encoding.py", {"tool_input": {"file_path": str(risky)}})
    assert _fired(out)
    assert "cp1252" in out


def test_encoding_guard_accepts_an_explicit_encoding(tmp_path):
    safe = tmp_path / "safe.py"
    safe.write_text("from pathlib import Path\n"
                    "data = Path('x').read_text(encoding='utf-8')\n",
                    encoding="utf-8")
    assert not _fired(
        _run("check_encoding.py", {"tool_input": {"file_path": str(safe)}}))


def test_encoding_guard_allows_binary_mode(tmp_path):
    """open(p, 'rb') takes no encoding — flagging it would be noise."""
    binary = tmp_path / "b.py"
    binary.write_text("raw = open('x', 'rb').read()\n", encoding="utf-8")
    assert not _fired(
        _run("check_encoding.py", {"tool_input": {"file_path": str(binary)}}))


def test_encoding_guard_ignores_non_python(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("Path('x').read_text()\n", encoding="utf-8")
    assert not _fired(
        _run("check_encoding.py", {"tool_input": {"file_path": str(md)}}))


# ── every hook must be crash-proof ───────────────────────────────────────

@pytest.mark.parametrize("script", ["graphify_nudge.py", "check_encoding.py",
                                    "run_tests.py"])
@pytest.mark.parametrize("payload", [{}, {"tool_input": {}},
                                     {"tool_input": {"file_path": "/nope.py"}}])
def test_hooks_survive_junk_input(script, payload):
    """A hook that raises breaks the tool call it was watching."""
    _run(script, payload)          # _run asserts exit 0
