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


# ── the worktree path guard ─────────────────────────────────────────────────
#
# Both PostToolUse hooks used to skip any path containing "/.claude/". Git
# worktrees live at .claude/worktrees/<name>/, so EVERY source file in a
# worktree matched and the hooks silently did nothing for the entire
# worktree. run_tests.py had been dead in every worktree session before this
# was found. The guard now matches the config subdirectories instead.

_WORKTREE = "/repo/.claude/worktrees/wt-1"


@pytest.mark.parametrize("script,name,body", [
    ("run_tests.py", "propresenterrunsheet/config.py", "x = 1\n"),
    ("frontend_guard.py", "static/app.css", ".a{color:#000;background:#111}\n"),
])
def test_hooks_are_not_disabled_by_a_worktree_path(tmp_path, script, name, body):
    """A source file inside .claude/worktrees/ must still be processed."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        script[:-3], Path(__file__).resolve().parent.parent / ".claude" / "hooks" / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert not mod._is_claude_config(f"{_WORKTREE}/{name}"), (
        f"{script} would skip a source file merely because the worktree "
        f"lives under .claude/")
    # ...while genuine Claude config is still skipped.
    assert mod._is_claude_config(f"{_WORKTREE}/.claude/hooks/thing.py")
    assert mod._is_claude_config("/repo/.claude/agents/reviewer.md")


# ── frontend_guard ──────────────────────────────────────────────────────────

def _guard(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return _run("frontend_guard.py", {"tool_input": {"file_path": str(f)}})


def test_frontend_guard_computes_real_contrast(tmp_path):
    """The shipped .save-dot pairing: --dim on --card is 2.47:1."""
    out = _guard(tmp_path, "a.css",
                 ":root{--dim:#5a5a6e;--card:#1c1c34;}\n"
                 ".save-dot{color:var(--dim);background:var(--card);}\n")
    assert "2.47:1" in out and "save-dot" in out


def test_frontend_guard_flags_a_killed_focus_ring(tmp_path):
    out = _guard(tmp_path, "a.css",
                 "button:focus-visible{box-shadow:0 0 0 3px rgba(1,2,3,.16);"
                 "outline:none;}\n")
    assert "focus" in out and "outline:none" in out


def test_frontend_guard_flags_an_img_with_no_src(tmp_path):
    out = _guard(tmp_path, "a.html", '<img id="p" alt="preview" width="160">')
    assert "no src" in out


def test_frontend_guard_is_silent_on_clean_css(tmp_path):
    assert _guard(tmp_path, "a.css",
                  ":root{--fg:#f2ece2;--bg:#12100f;}\n"
                  ".ok{color:var(--fg);background:var(--bg);}\n") == ""


# ── risk_radar ──────────────────────────────────────────────────────────────

def _radar(path, content=""):
    return _run("risk_radar.py",
                {"tool_input": {"file_path": path, "content": content}})


def test_risk_radar_routes_analytics_to_the_privacy_auditor():
    assert "privacy-auditor" in _radar("/r/propresenterrunsheet/stats.py")


def test_risk_radar_catches_a_track_call_added_anywhere():
    out = _radar("/r/propresenterrunsheet/routes/core.py",
                 'stats.track("thing", n=1)')
    assert "privacy-auditor" in out


def test_risk_radar_routes_the_release_workflow_to_the_build_reviewer():
    assert "frozen-build-reviewer" in _radar("/r/.github/workflows/release.yml")


def test_risk_radar_stays_silent_on_ordinary_files():
    assert _radar("/r/propresenterrunsheet/parsing/ocr.py", "x = 1") == ""
