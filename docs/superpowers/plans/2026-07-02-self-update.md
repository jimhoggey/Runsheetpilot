# Self-Update Mechanism Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-click self-update: the app checks GitHub Releases for a newer version, shows a banner, and on click downloads, verifies, swaps itself, and relaunches — Mac + Windows, with Windows as the production target.

**Architecture:** New `propresenterrunsheet/updater.py` (check → download → SHA-256 verify → rename-swap → relaunch, exposed as a small state machine), a `routes/update.py` blueprint (`GET /api/update`, `POST /api/update/apply`), a header banner driven by polling, and `release.yml` changes that publish stable-named assets (`Runsheet-Pilot-mac.zip`, `Runsheet-Pilot-windows.exe`, `SHA256SUMS.txt`). Spec: `docs/superpowers/specs/2026-07-02-self-update-design.md`.

**Tech Stack:** Python 3.11 (stdlib + `requests`, already shipped), Flask blueprints, vanilla JS, pytest, GitHub Actions.

**Conventions for every task:** run tests with `python3 -m pytest` from the repo root `/Users/fynnj/Documents/Claude/Projects/Pro Proprestor app copy`. All work happens on branch `claude/self-update`. The logger is `logging.getLogger("pp_runsheet")`. Windows swap logic must be provable on the Mac dev machine — hence pure "plan" functions + injectable side effects.

---

### Task 0: Branch + test scaffolding

**Files:**
- Create: `tests/test_updater.py` (fixtures only for now)

- [ ] **Step 1: Create the branch**

```bash
cd "/Users/fynnj/Documents/Claude/Projects/Pro Proprestor app copy"
git checkout -b claude/self-update main
```

- [ ] **Step 2: Write the shared fixture file**

Create `tests/test_updater.py`:

```python
"""Tests for the self-update mechanism (propresenterrunsheet/updater.py).

Windows is the production target but the dev machine is a Mac, so the
Windows swap logic is exercised through pure plan functions and injected
side effects — no test here ever renames a real running binary or hits
the network.
"""

import json
from pathlib import Path

import pytest

from propresenterrunsheet import updater


@pytest.fixture
def upd_env(tmp_path, monkeypatch):
    """Isolated updater module state: UPDATES_DIR in tmp, fresh state dict."""
    monkeypatch.setattr(updater, "UPDATES_DIR", tmp_path / "updates")
    monkeypatch.setattr(updater, "_AVAILABLE", {})
    monkeypatch.setattr(
        updater, "_state",
        {"state": "idle", "current": updater.VERSION, "latest": None,
         "notes_url": None, "error": None},
    )
    return tmp_path


class FakeResponse:
    """Minimal stand-in for requests.Response used by http_get injectors."""

    def __init__(self, *, json_data=None, text="", content=b"", status=200):
        self._json = json_data
        self.text = text
        self._content = content
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
```

- [ ] **Step 3: Run it to prove the import fails (module doesn't exist yet)**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'propresenterrunsheet.updater'` — this is the failing state TDD starts from. Do NOT commit yet (nothing passes); Task 1 makes it green and commits both files.

---

### Task 1: Semver parsing + comparison (updater.py is born)

**Files:**
- Create: `propresenterrunsheet/updater.py`
- Modify: `tests/test_updater.py` (append tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_updater.py`:

```python
# ── Semver ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tag,expected", [
    ("v2.3.0", (2, 3, 0)),
    ("2.3.0", (2, 3, 0)),
    ("V2.10.1", (2, 10, 1)),
    ("v2.3.0-rc1", None),      # pre-release — never offered
    ("v2.3", None),            # not 3 parts
    ("nonsense", None),
    ("", None),
    (None, None),
])
def test_parse_semver(tag, expected):
    assert updater.parse_semver(tag) == expected


def test_is_newer_orders_numerically_not_lexically():
    assert updater.is_newer("v2.10.0", current="2.9.9") is True
    assert updater.is_newer("v2.3.0", current="2.3.0") is False
    assert updater.is_newer("v2.2.9", current="2.3.0") is False
    assert updater.is_newer("garbage", current="2.3.0") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: ERROR `ModuleNotFoundError: No module named 'propresenterrunsheet.updater'`

- [ ] **Step 3: Create `propresenterrunsheet/updater.py`**

```python
"""Self-update via GitHub Releases.

Flow: a background check on launch hits the public Releases API; if the
latest tag is newer than VERSION, the UI shows an "Update & Restart"
banner. Clicking it downloads the platform asset, verifies its SHA-256
against SHA256SUMS.txt from the same release, then swaps the installed
bundle using the rename trick and relaunches:

  Windows (production target): a running .exe cannot be overwritten but
  CAN be renamed. Rename running exe -> .exe.old, move the verified new
  exe into place, spawn it detached, hard-exit. The new instance deletes
  the .old on boot.

  Mac: rename the .app -> .app.old, move the new .app in, `open` it,
  hard-exit. Extraction uses `ditto -x -k` (NOT zipfile) because Python's
  zipfile drops the executable bits ditto preserved at build time.

Fail-safe: the installed copy is never touched until a verified binary
sits on disk, and the swap is two renames with rollback. A failed CHECK
is silent (offline booths are normal); a failed UPDATE surfaces in the
banner with a manual-download link.

Only active in the frozen bundle — running from source never self-updates.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from .config import DATA_DIR, VERSION

log = logging.getLogger("pp_runsheet")

REPO = "jimhoggey/propresenter-runsheet-builder"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
ASSET_MAC = "Runsheet-Pilot-mac.zip"
ASSET_WIN = "Runsheet-Pilot-windows.exe"
SUMS_ASSET = "SHA256SUMS.txt"
UPDATES_DIR = DATA_DIR / "updates"

# UI-visible state machine:
#   idle -> available -> downloading -> verifying -> applying   (then the
# process restarts) with `error` reachable from any active step. Guarded
# by _lock; routes read a copy via get_state().
_state = {"state": "idle", "current": VERSION, "latest": None,
          "notes_url": None, "error": None}
_AVAILABLE = {}   # asset/sums URLs staged by check_for_update for apply
_lock = threading.Lock()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def parse_semver(tag):
    """'v2.3.0' -> (2, 3, 0). Pre-releases and junk -> None (never offered)."""
    if not tag or not isinstance(tag, str):
        return None
    t = tag.strip().lstrip("vV")
    if "-" in t:
        return None
    parts = t.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def is_newer(tag, current=VERSION) -> bool:
    remote = parse_semver(tag)
    local = parse_semver(current)
    return bool(remote and local and remote > local)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add propresenterrunsheet/updater.py tests/test_updater.py
git commit -m "feat(updater): semver parse + compare, module skeleton"
```

---

### Task 2: Release parsing + platform asset selection

**Files:**
- Modify: `propresenterrunsheet/updater.py` (append)
- Modify: `tests/test_updater.py` (append)

- [ ] **Step 1: Append failing tests**

```python
# ── Release payload parsing ─────────────────────────────────────────────────
def _release_payload(tag="v9.9.9", assets=None):
    if assets is None:
        assets = [
            {"name": "Runsheet-Pilot-mac.zip",
             "browser_download_url": "https://gh/mac.zip"},
            {"name": "Runsheet-Pilot-windows.exe",
             "browser_download_url": "https://gh/win.exe"},
            {"name": "SHA256SUMS.txt",
             "browser_download_url": "https://gh/sums.txt"},
            {"name": "Runsheet-Pilot-9.9.9.dmg",
             "browser_download_url": "https://gh/inst.dmg"},
        ]
    return {"tag_name": tag, "html_url": "https://gh/rel", "assets": assets}


def test_pick_platform_asset_darwin_and_win32():
    assets = _release_payload()["assets"]
    assert updater.pick_platform_asset(assets, "darwin")["name"] == "Runsheet-Pilot-mac.zip"
    assert updater.pick_platform_asset(assets, "win32")["name"] == "Runsheet-Pilot-windows.exe"
    assert updater.pick_platform_asset([], "darwin") is None


def test_parse_release_returns_info_when_newer():
    info = updater.parse_release(_release_payload(), platform="win32")
    assert info == {
        "version": "9.9.9",
        "notes_url": "https://gh/rel",
        "asset_name": "Runsheet-Pilot-windows.exe",
        "asset_url": "https://gh/win.exe",
        "sums_url": "https://gh/sums.txt",
    }


def test_parse_release_none_when_not_newer_or_missing_assets():
    assert updater.parse_release(_release_payload(tag="v0.0.1"), platform="win32") is None
    # newer tag but no stable asset for the platform -> invisible to updater
    no_win = _release_payload(assets=[
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://gh/sums.txt"}])
    assert updater.parse_release(no_win, platform="win32") is None
    # newer tag, asset present, but sums file missing -> refuse (can't verify)
    no_sums = _release_payload(assets=[
        {"name": "Runsheet-Pilot-windows.exe", "browser_download_url": "https://gh/w.exe"}])
    assert updater.parse_release(no_sums, platform="win32") is None
    assert updater.parse_release(None, platform="win32") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'pick_platform_asset'`

- [ ] **Step 3: Append implementation to `updater.py`**

```python
def pick_platform_asset(assets, platform=None):
    """Exact-name match for this platform's stable asset, else None."""
    want = ASSET_MAC if (platform or sys.platform) == "darwin" else ASSET_WIN
    for a in assets or []:
        if a.get("name") == want:
            return a
    return None


def parse_release(payload, platform=None):
    """Turn a /releases/latest payload into update info, or None if the
    release isn't newer, lacks this platform's stable asset, or lacks the
    checksums file (unverifiable -> not offered)."""
    tag = (payload or {}).get("tag_name") or ""
    ver = parse_semver(tag)
    if not ver or not is_newer(tag):
        return None
    assets = payload.get("assets") or []
    asset = pick_platform_asset(assets, platform)
    sums = next((a for a in assets if a.get("name") == SUMS_ASSET), None)
    if not asset or not sums:
        return None
    return {
        "version": ".".join(str(p) for p in ver),
        "notes_url": payload.get("html_url") or RELEASES_PAGE,
        "asset_name": asset["name"],
        "asset_url": asset["browser_download_url"],
        "sums_url": sums["browser_download_url"],
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add propresenterrunsheet/updater.py tests/test_updater.py
git commit -m "feat(updater): release payload parsing + platform asset selection"
```

---

### Task 3: Checksums — parse SHA256SUMS.txt + verified download

**Files:**
- Modify: `propresenterrunsheet/updater.py` (append)
- Modify: `tests/test_updater.py` (append)

- [ ] **Step 1: Append failing tests**

```python
# ── Checksums + download ────────────────────────────────────────────────────
def test_parse_sha256sums_handles_binary_marker_and_paths():
    good = "a" * 64
    text = (
        f"{good}  Runsheet-Pilot-windows.exe\n"
        f"{'b' * 64} *Runsheet-Pilot-mac.zip\n"
        f"{'c' * 64}  ./stable/Runsheet-Pilot-other.bin\n"
        "not a sums line\n"
    )
    sums = updater.parse_sha256sums(text)
    assert sums["Runsheet-Pilot-windows.exe"] == good
    assert sums["Runsheet-Pilot-mac.zip"] == "b" * 64
    assert sums["Runsheet-Pilot-other.bin"] == "c" * 64
    assert updater.parse_sha256sums("") == {}
    assert updater.parse_sha256sums(None) == {}


def test_download_and_verify_good_checksum(upd_env):
    import hashlib
    body = b"new-shiny-binary" * 1000
    expected = hashlib.sha256(body).hexdigest()
    got = updater.download_and_verify(
        "https://gh/win.exe", "Runsheet-Pilot-windows.exe", expected,
        http_get=lambda url, **kw: FakeResponse(content=body))
    assert got.read_bytes() == body
    assert got.name == "Runsheet-Pilot-windows.exe"
    assert not got.with_name(got.name + ".part").exists()


def test_download_and_verify_bad_checksum_deletes_and_raises(upd_env):
    with pytest.raises(ValueError, match="Checksum mismatch"):
        updater.download_and_verify(
            "https://gh/win.exe", "Runsheet-Pilot-windows.exe", "f" * 64,
            http_get=lambda url, **kw: FakeResponse(content=b"tampered"))
    leftovers = list(updater.UPDATES_DIR.glob("*")) if updater.UPDATES_DIR.exists() else []
    assert leftovers == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL with `AttributeError: ... 'parse_sha256sums'`

- [ ] **Step 3: Append implementation**

```python
def parse_sha256sums(text):
    """Parse `sha256sum` output: '<64-hex>  <name>' per line. Tolerates the
    binary marker (*name) and path prefixes — keys are bare filenames."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            name = parts[-1].lstrip("*").replace("\\", "/").rsplit("/", 1)[-1]
            out[name] = parts[0].lower()
    return out


def download_and_verify(url, name, expected_sha, http_get=None, timeout=120):
    """Stream `url` to UPDATES_DIR/<name> (via a .part file so a torn
    download can never be mistaken for a complete one) and verify SHA-256.
    Mismatch -> delete + ValueError."""
    get = http_get or requests.get
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    part = UPDATES_DIR / (name + ".part")
    final = UPDATES_DIR / name
    digest = hashlib.sha256()
    with get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    digest.update(chunk)
    if digest.hexdigest().lower() != (expected_sha or "").lower():
        part.unlink(missing_ok=True)
        raise ValueError(f"Checksum mismatch for {name}")
    part.replace(final)
    return final
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add propresenterrunsheet/updater.py tests/test_updater.py
git commit -m "feat(updater): SHA256SUMS parsing + verified streaming download"
```

---

### Task 4: Install location + writability guard

**Files:**
- Modify: `propresenterrunsheet/updater.py` (append)
- Modify: `tests/test_updater.py` (append)

- [ ] **Step 1: Append failing tests**

```python
# ── Install location ────────────────────────────────────────────────────────
def test_install_location_mac_walks_up_to_app_bundle(tmp_path):
    app = tmp_path / "Applications" / "Runsheet Pilot.app"
    exe = app / "Contents" / "MacOS" / "Runsheet Pilot"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    loc, writable = updater.install_location(executable=str(exe), platform="darwin")
    assert loc == app
    assert writable is True   # tmp_path is writable


def test_install_location_mac_readonly_volume_reports_unwritable(tmp_path, monkeypatch):
    app = tmp_path / "Volumes" / "Runsheet Pilot" / "Runsheet Pilot.app"
    exe = app / "Contents" / "MacOS" / "Runsheet Pilot"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(updater.os, "access", lambda p, m: False)
    loc, writable = updater.install_location(executable=str(exe), platform="darwin")
    assert loc == app
    assert writable is False


def test_install_location_windows_is_exe_path(tmp_path):
    exe = tmp_path / "Desktop" / "Runsheet Pilot.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    loc, writable = updater.install_location(executable=str(exe), platform="win32")
    assert loc == exe
    assert writable is True
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL with `AttributeError: ... 'install_location'`

- [ ] **Step 3: Append implementation**

```python
def install_location(executable=None, platform=None):
    """(install_root, writable). Mac: the .app bundle directory resolved
    by walking up from sys.executable; Windows: the exe path itself.
    `writable` checks the PARENT directory because the swap is two renames
    inside it. Read-only (dmg mount, network share, no rights) -> the UI
    degrades to notify-only instead of attempting a doomed swap."""
    exe = Path(executable or sys.executable)
    plat = platform or sys.platform
    if plat == "darwin":
        for parent in exe.parents:
            if parent.name.endswith(".app"):
                return parent, os.access(str(parent.parent), os.W_OK)
        return exe.parent, False   # not in a bundle (dev) — never writable
    if plat == "win32":
        return exe, os.access(str(exe.parent), os.W_OK)
    return exe, False
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add propresenterrunsheet/updater.py tests/test_updater.py
git commit -m "feat(updater): install-location resolution + writability guard"
```

---

### Task 5: The swap — pure plan + executor with rollback (Windows-provable)

**Files:**
- Modify: `propresenterrunsheet/updater.py` (append)
- Modify: `tests/test_updater.py` (append)

- [ ] **Step 1: Append failing tests**

```python
# ── Swap plan + executor ────────────────────────────────────────────────────
def test_plan_swap_windows_exact_operation_order():
    exe = Path("C:/Users/av/Desktop/Runsheet Pilot.exe")
    new = Path("C:/Users/av/AppData/Roaming/Runsheet Pilot/updates/Runsheet-Pilot-windows.exe")
    ops = updater.plan_swap(exe, new, "win32")
    assert ops == [
        ("rename", exe, Path("C:/Users/av/Desktop/Runsheet Pilot.exe.old")),
        ("move", new, exe),
        ("spawn_exe", exe),
        ("exit",),
    ]


def test_plan_swap_mac_uses_open():
    app = Path("/Applications/Runsheet Pilot.app")
    new = Path("/tmp/updates/extracted/Runsheet Pilot.app")
    ops = updater.plan_swap(app, new, "darwin")
    assert ops == [
        ("rename", app, Path("/Applications/Runsheet Pilot.app.old")),
        ("move", new, app),
        ("spawn_app", app),
        ("exit",),
    ]


def test_execute_swap_happy_path_filesystem_end_state(tmp_path):
    installed = tmp_path / "Runsheet Pilot.app"
    (installed / "Contents").mkdir(parents=True)
    (installed / "Contents" / "old-marker").write_text("v1")
    new = tmp_path / "staged" / "Runsheet Pilot.app"
    (new / "Contents").mkdir(parents=True)
    (new / "Contents" / "new-marker").write_text("v2")

    spawned, exited = [], []
    ops = updater.plan_swap(installed, new, "darwin")
    updater._execute_swap(ops, spawn=lambda op: spawned.append(op),
                          hard_exit=lambda code=0: exited.append(code))

    assert (installed / "Contents" / "new-marker").exists()
    old = installed.with_name(installed.name + ".old")
    assert (old / "Contents" / "old-marker").exists()
    assert spawned == [("spawn_app", installed)]
    assert exited == [0]


def test_execute_swap_rolls_back_rename_when_move_fails(tmp_path):
    installed = tmp_path / "Runsheet Pilot.app"
    (installed / "Contents").mkdir(parents=True)
    missing_new = tmp_path / "staged" / "does-not-exist.app"   # move will fail

    ops = updater.plan_swap(installed, missing_new, "darwin")
    with pytest.raises(Exception):
        updater._execute_swap(ops, spawn=lambda op: None,
                              hard_exit=lambda code=0: None)

    assert installed.exists()                                   # rolled back
    assert not installed.with_name(installed.name + ".old").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL with `AttributeError: ... 'plan_swap'`

- [ ] **Step 3: Append implementation**

```python
def plan_swap(install_path, new_payload, platform):
    """Pure: the exact operation sequence for the swap, as data. Kept free
    of side effects so the Windows order is asserted in tests that run on
    the Mac dev machine (Windows is the production deployment)."""
    old = install_path.with_name(install_path.name + ".old")
    spawn_kind = "spawn_exe" if platform == "win32" else "spawn_app"
    return [
        ("rename", install_path, old),
        ("move", new_payload, install_path),
        (spawn_kind, install_path),
        ("exit",),
    ]


def _default_spawn(op):
    kind, target = op[0], op[1]
    if kind == "spawn_exe":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — the child must
        # outlive this process or the relaunch dies with us.
        flags = 0x00000008 | 0x00000200
        subprocess.Popen([str(target)], creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(["open", str(target)])


def _execute_swap(ops, spawn=None, hard_exit=None):
    """Walk the op plan. Any failure rolls the filesystem ops back in
    reverse so the installed copy keeps working, then re-raises for the
    caller to surface in the banner."""
    spawn = spawn or _default_spawn
    hard_exit = hard_exit or os._exit
    done = []
    for op in ops:
        kind = op[0]
        try:
            if kind == "rename":
                op[1].rename(op[2])
                done.append(op)
            elif kind == "move":
                shutil.move(str(op[1]), str(op[2]))
                done.append(op)
            elif kind in ("spawn_exe", "spawn_app"):
                spawn(op)
            elif kind == "exit":
                log.info("Update applied — restarting")
                hard_exit(0)
        except Exception:
            for d in reversed(done):
                try:
                    if d[0] == "rename":
                        d[2].rename(d[1])
                    elif d[0] == "move":
                        shutil.move(str(d[2]), str(d[1]))
                except Exception:
                    log.exception("Rollback step failed (continuing)")
            raise
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add propresenterrunsheet/updater.py tests/test_updater.py
git commit -m "feat(updater): pure swap plan + executor with rollback"
```

---

### Task 6: Orchestration — check, payload prep, apply, boot hooks

**Files:**
- Modify: `propresenterrunsheet/updater.py` (append)
- Modify: `tests/test_updater.py` (append)

- [ ] **Step 1: Append failing tests**

```python
# ── Orchestration ───────────────────────────────────────────────────────────
def test_check_for_update_sets_available_state(upd_env):
    payload = _release_payload(tag="v99.0.0")
    info = updater.check_for_update(
        http_get=lambda url, **kw: FakeResponse(json_data=payload),
        platform="win32")
    assert info["version"] == "99.0.0"
    st = updater.get_state()
    assert st["state"] == "available"
    assert st["latest"] == "99.0.0"
    assert updater._AVAILABLE["asset_name"] == "Runsheet-Pilot-windows.exe"


def test_check_for_update_offline_is_silent(upd_env):
    def boom(url, **kw):
        raise OSError("no network")
    assert updater.check_for_update(http_get=boom, platform="win32") is None
    assert updater.get_state()["state"] == "idle"     # no error surfaced


def test_apply_update_happy_path_windows_ops(upd_env, tmp_path, monkeypatch):
    import hashlib
    body = b"exe-bytes"
    sha = hashlib.sha256(body).hexdigest()
    updater._AVAILABLE.update({
        "asset_name": "Runsheet-Pilot-windows.exe",
        "asset_url": "https://gh/win.exe",
        "sums_url": "https://gh/sums.txt",
        "version": "99.0.0",
    })
    fake_exe = tmp_path / "install" / "Runsheet Pilot.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"old")
    monkeypatch.setattr(updater, "install_location",
                        lambda executable=None, platform=None: (fake_exe, True))
    monkeypatch.setattr(updater.sys, "platform", "win32")

    def http_get(url, **kw):
        if url == "https://gh/sums.txt":
            return FakeResponse(text=f"{sha}  Runsheet-Pilot-windows.exe\n")
        return FakeResponse(content=body)

    executed = []
    monkeypatch.setattr(updater, "_execute_swap",
                        lambda ops, **kw: executed.extend(ops))
    updater.apply_update(http_get=http_get)

    assert updater.get_state()["state"] == "applying"
    assert executed[0] == ("rename", fake_exe,
                           fake_exe.with_name("Runsheet Pilot.exe.old"))
    assert executed[1][2] == fake_exe                 # move lands on exe path
    assert executed[2] == ("spawn_exe", fake_exe)


def test_apply_update_unwritable_location_errors_without_download(upd_env, monkeypatch):
    updater._AVAILABLE.update({
        "asset_name": "Runsheet-Pilot-windows.exe",
        "asset_url": "https://gh/win.exe",
        "sums_url": "https://gh/sums.txt",
        "version": "99.0.0",
    })
    monkeypatch.setattr(updater, "install_location",
                        lambda executable=None, platform=None: (Path("X:/ro.exe"), False))
    updater.apply_update(http_get=lambda url, **kw: FakeResponse(text="", content=b""))
    st = updater.get_state()
    assert st["state"] == "error"
    assert "writable" in st["error"].lower() or "move the app" in st["error"].lower()


def test_apply_update_checksum_mismatch_sets_error(upd_env, tmp_path, monkeypatch):
    updater._AVAILABLE.update({
        "asset_name": "Runsheet-Pilot-windows.exe",
        "asset_url": "https://gh/win.exe",
        "sums_url": "https://gh/sums.txt",
        "version": "99.0.0",
    })
    fake_exe = tmp_path / "install" / "Runsheet Pilot.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"old")
    monkeypatch.setattr(updater, "install_location",
                        lambda executable=None, platform=None: (fake_exe, True))

    def http_get(url, **kw):
        if url == "https://gh/sums.txt":
            return FakeResponse(text=("f" * 64) + "  Runsheet-Pilot-windows.exe\n")
        return FakeResponse(content=b"tampered")

    updater.apply_update(http_get=http_get)
    assert updater.get_state()["state"] == "error"
    assert fake_exe.read_bytes() == b"old"            # install untouched


def test_cleanup_leftovers_removes_old_and_updates_dir(upd_env, tmp_path, monkeypatch):
    exe = tmp_path / "install" / "Runsheet Pilot.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"new")
    old = exe.with_name(exe.name + ".old")
    old.write_bytes(b"old")
    updater.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    (updater.UPDATES_DIR / "junk.part").write_bytes(b"x")
    monkeypatch.setattr(updater, "install_location",
                        lambda executable=None, platform=None: (exe, True))
    updater.cleanup_leftovers(retry_delay=0)
    assert not old.exists()
    assert not updater.UPDATES_DIR.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL with `AttributeError: ... 'check_for_update'`

- [ ] **Step 3: Append implementation**

```python
def check_for_update(http_get=None, timeout=5, platform=None):
    """Hit /releases/latest; stage + expose 'available' if newer. Failures
    are logged and swallowed — a booth without internet must never see an
    update error it didn't ask for."""
    get = http_get or requests.get
    try:
        r = get(API_LATEST, timeout=timeout,
                headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        info = parse_release(r.json(), platform=platform)
    except Exception as e:
        log.info("Update check skipped: %s", e)
        return None
    if info:
        with _lock:
            _AVAILABLE.clear()
            _AVAILABLE.update(info)
        _set(state="available", latest=info["version"],
             notes_url=info["notes_url"], error=None)
        log.info("Update available: v%s", info["version"])
    return info


def _prepare_payload(archive_path, platform):
    """Windows: the downloaded exe IS the payload. Mac: extract the zip
    with `ditto -x -k` (zipfile would strip the executable bits) and
    sanity-check the bundle shape before we ever touch the installed app."""
    if platform != "darwin":
        return archive_path
    extract = UPDATES_DIR / "extracted"
    shutil.rmtree(extract, ignore_errors=True)
    extract.mkdir(parents=True)
    subprocess.run(["ditto", "-x", "-k", str(archive_path), str(extract)],
                   check=True, capture_output=True)
    apps = [p for p in extract.iterdir() if p.name.endswith(".app")]
    if len(apps) != 1 or not (apps[0] / "Contents" / "MacOS").is_dir():
        raise ValueError("Update zip did not contain a valid app bundle")
    return apps[0]


def apply_update(http_get=None, spawn=None, hard_exit=None):
    """download -> verify -> prepare -> swap. Runs in a worker thread from
    the route; every failure lands in state=error with the install intact."""
    with _lock:
        info = dict(_AVAILABLE)
    if not info.get("asset_url"):
        _set(state="error", error="No update staged — check for updates first.")
        return
    try:
        install, writable = install_location()
        if not writable:
            raise PermissionError(
                "Install location is not writable. Move the app to "
                "Applications (Mac) or a writable folder (Windows), or "
                "download the update manually.")
        _set(state="downloading", error=None)
        get = http_get or requests.get
        sums_resp = get(info["sums_url"], timeout=30)
        sums_resp.raise_for_status()
        expected = parse_sha256sums(sums_resp.text).get(info["asset_name"])
        if not expected:
            raise ValueError(f"{info['asset_name']} missing from {SUMS_ASSET}")
        archive = download_and_verify(info["asset_url"], info["asset_name"],
                                      expected, http_get=http_get)
        _set(state="verifying")
        payload = _prepare_payload(archive, sys.platform)
        _set(state="applying")
        _execute_swap(plan_swap(install, payload, sys.platform),
                      spawn=spawn, hard_exit=hard_exit)
    except Exception as e:
        log.exception("Update failed")
        _set(state="error", error=str(e))


def cleanup_leftovers(retry_delay=0.5):
    """Boot-time janitor: delete the previous version (.old) and the
    updates staging dir. The old process may still be exiting, so deleting
    the .old retries briefly. Never fatal."""
    try:
        install, _ = install_location()
        old = install.with_name(install.name + ".old")
        for _ in range(10):
            if not old.exists():
                break
            try:
                if old.is_dir():
                    shutil.rmtree(old)
                else:
                    old.unlink()
                log.info("Removed previous version leftover: %s", old.name)
                break
            except Exception:
                time.sleep(retry_delay)
        shutil.rmtree(UPDATES_DIR, ignore_errors=True)
    except Exception:
        log.exception("cleanup_leftovers failed (non-fatal)")


def start_background_check():
    """Fire-and-forget launch check. Frozen bundles only — running from
    source must never self-update."""
    if not getattr(sys, "frozen", False):
        return
    threading.Thread(target=check_for_update, daemon=True,
                     name="update-check").start()
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: all PASS. Also run the full suite: `python3 -m pytest tests/ -q` — expected: all PASS (138 existing + new).

- [ ] **Step 5: Commit**

```bash
git add propresenterrunsheet/updater.py tests/test_updater.py
git commit -m "feat(updater): check/apply orchestration, ditto extraction, boot cleanup"
```

---

### Task 7: API routes + blueprint registration + server wiring

**Files:**
- Create: `propresenterrunsheet/routes/update.py`
- Modify: `propresenterrunsheet/routes/__init__.py`
- Modify: `propresenterrunsheet/server.py` (main())
- Modify: `tests/test_updater.py` (append route tests)

- [ ] **Step 1: Append failing route tests**

```python
# ── Routes ──────────────────────────────────────────────────────────────────
def test_api_update_get_dev_mode(client):
    r = client.get("/api/update")
    assert r.status_code == 200
    body = r.get_json()
    assert body["state"] == "dev"
    assert body["current"] == updater.VERSION


def test_api_update_apply_409_in_dev_mode(client):
    r = client.post("/api/update/apply")
    assert r.status_code == 409


def test_api_update_get_frozen_returns_state(client, monkeypatch, upd_env):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    updater._set(state="available", latest="99.0.0",
                 notes_url="https://gh/rel", error=None)
    body = client.get("/api/update").get_json()
    assert body["state"] == "available"
    assert body["latest"] == "99.0.0"


def test_api_update_apply_starts_worker_when_available(client, monkeypatch, upd_env):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    updater._set(state="available", latest="99.0.0")
    calls = []
    monkeypatch.setattr(updater, "apply_update", lambda **kw: calls.append(1))

    import propresenterrunsheet.routes.update as upd_routes
    ran = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._t = target
        def start(self):
            self._t()
            ran.append(1)
    monkeypatch.setattr(upd_routes.threading, "Thread", FakeThread)

    r = client.post("/api/update/apply")
    assert r.status_code == 200
    assert calls == [1] and ran == [1]


def test_api_update_apply_409_when_nothing_available(client, monkeypatch, upd_env):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    updater._set(state="idle")
    assert client.post("/api/update/apply").status_code == 409
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_updater.py -q`
Expected: FAIL — `/api/update` 404s (blueprint doesn't exist).

- [ ] **Step 3: Create `propresenterrunsheet/routes/update.py`**

```python
"""Flask blueprint for /api/update — self-update status + trigger.

GET  /api/update        -> current updater state (safe to poll)
GET  /api/update?refresh=1 -> re-check GitHub synchronously, then state
POST /api/update/apply  -> start download+verify+swap in a worker thread

Dev mode (running from source) reports state="dev" and refuses apply —
the updater only ever operates on the frozen bundle."""

import logging
import sys
import threading

from flask import Blueprint, jsonify, request

from .. import updater


bp = Blueprint("update", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/api/update", methods=["GET"])
def api_update_get():
    if not getattr(sys, "frozen", False):
        return jsonify({"state": "dev", "current": updater.VERSION})
    if request.args.get("refresh"):
        updater.check_for_update()
    return jsonify(updater.get_state())


@bp.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    if not getattr(sys, "frozen", False):
        return jsonify({"ok": False,
                        "error": "Self-update only runs in the installed "
                                 "app, not from source."}), 409
    state = updater.get_state()
    if state["state"] not in ("available", "error"):
        return jsonify({"ok": False,
                        "error": "No update available to apply."}), 409
    threading.Thread(target=updater.apply_update, daemon=True,
                     name="update-apply").start()
    return jsonify({"ok": True})
```

- [ ] **Step 4: Register the blueprint**

In `propresenterrunsheet/routes/__init__.py`, add the import line (alphabetical, after `settings`):

```python
from .update import bp as update_bp
```

and extend the registration tuple to end with `update_bp`:

```python
    for bp in (core_bp, settings_bp, library_bp, parse_bp,
               playlist_bp, runsheet_bp, clocks_bp, license_bp, update_bp):
        app.register_blueprint(bp)
```

- [ ] **Step 5: Wire boot hooks in `propresenterrunsheet/server.py`**

Add to the imports block near the other `from .` imports:

```python
from .updater import cleanup_leftovers, start_background_check
```

In `main()`, directly after the `start_clocks_loop()` call, add:

```python
        # Self-update housekeeping (frozen bundles only — both are no-ops
        # from source): delete the previous version's .old leftover and the
        # updates staging dir, then check GitHub for a newer release in the
        # background. Never blocks startup; check failures are silent.
        if getattr(sys, "frozen", False):
            cleanup_leftovers()
        start_background_check()
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS (existing 138 + all new updater tests).

- [ ] **Step 7: Commit**

```bash
git add propresenterrunsheet/routes/update.py propresenterrunsheet/routes/__init__.py propresenterrunsheet/server.py tests/test_updater.py
git commit -m "feat(updater): /api/update routes, blueprint registration, boot hooks"
```

---

### Task 8: Frontend — banner, progress, Settings row

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`
- Modify: `static/app.css`

- [ ] **Step 1: Add the banner markup to `templates/index.html`**

Directly AFTER the closing `</header>` tag, insert:

```html
<!-- Update banner — hidden unless the background check found a newer
     release (state=available from /api/update). One click downloads,
     verifies, swaps the app, and relaunches. Driven by app.js. -->
<div id="update-banner" hidden>
  <span id="update-banner-text">⬆ A new version is available.</span>
  <a id="update-notes-link" href="#" target="_blank" rel="noopener">Release notes</a>
  <span style="flex:1"></span>
  <button class="btn btn-acc btn-sm" id="update-apply-btn" onclick="applyUpdate()">
    Update &amp; Restart
  </button>
  <button class="btn btn-dim btn-sm" id="update-later-btn"
          onclick="dismissUpdateBanner()" title="Hide until next launch">Later</button>
</div>
```

- [ ] **Step 2: Add the Settings row**

In `templates/index.html`, inside the Settings modal, directly BEFORE the `<div class="settings-credits">` line, insert:

```html
      <!-- UPDATES ──────────────────────────────────────────────── -->
      <section class="settings-section">
        <h3>Updates</h3>
        <div class="row" style="gap:8px;align-items:center">
          <span style="font-size:.8rem;color:var(--muted)">
            Current version: v<span id="update-current-version">—</span>
          </span>
          <button class="btn btn-dim btn-sm" onclick="checkForUpdatesNow()">
            Check for updates
          </button>
          <span id="update-check-msg" style="font-size:.74rem"></span>
        </div>
      </section>
```

- [ ] **Step 3: Add styles to `static/app.css`** (append at the end)

```css
/* ── Self-update banner ─────────────────────────────────────────────── */
#update-banner {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 18px;
  background: var(--acc-soft);
  border-bottom: 1px solid var(--acc-glow);
  font-size: 0.82rem;
}
#update-banner[hidden] { display: none; }
#update-banner-text { font-weight: 600; }
#update-notes-link { color: var(--acc-2); font-size: 0.76rem; }
```

- [ ] **Step 4: Add the JS to `static/app.js`** (append at the end, after `smInit();`)

```javascript
// ─── 11. Self-update ───────────────────────────────────────────────────────
// Banner appears when the background check (server-side, on launch) found
// a newer GitHub release. "Update & Restart" downloads + verifies + swaps
// the app; while the server restarts we poll until it answers again, then
// reload the page into the new version.

let _updateDismissed = false;

async function loadUpdateState() {
  try {
    const st = await fetch('/api/update').then(r => r.json());
    renderUpdateState(st);
  } catch (e) { /* server unreachable — ignore */ }
}

function renderUpdateState(st) {
  const cur = document.getElementById('update-current-version');
  if (cur && st.current) cur.textContent = st.current;
  const banner = document.getElementById('update-banner');
  if (!banner) return;
  if (st.state === 'available' && !_updateDismissed) {
    document.getElementById('update-banner-text').textContent =
      '⬆ Version ' + st.latest + ' is available.';
    const link = document.getElementById('update-notes-link');
    if (st.notes_url) { link.href = st.notes_url; link.hidden = false; }
    banner.hidden = false;
  } else if (st.state === 'error') {
    document.getElementById('update-banner-text').textContent =
      '⚠ Update failed: ' + (st.error || 'unknown error') + ' ';
    const link = document.getElementById('update-notes-link');
    link.textContent = 'Download manually';
    link.href = 'https://github.com/jimhoggey/propresenter-runsheet-builder/releases/latest';
    link.hidden = false;
    const btn = document.getElementById('update-apply-btn');
    btn.disabled = false;
    btn.textContent = 'Try again';
    banner.hidden = false;
  } else if (st.state !== 'downloading' && st.state !== 'verifying'
             && st.state !== 'applying') {
    banner.hidden = true;
  }
}

async function applyUpdate() {
  const btn = document.getElementById('update-apply-btn');
  btn.disabled = true;
  btn.textContent = 'Downloading…';
  try {
    const r = await fetch('/api/update/apply', {method: 'POST'});
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      btn.disabled = false;
      btn.textContent = 'Update & Restart';
      setStatus('Update could not start: ' + (body.error || r.status), 'var(--org)');
      return;
    }
  } catch (e) { /* fall through to polling — server may already be swapping */ }
  _pollUpdateProgress();
}

async function _pollUpdateProgress() {
  const btn = document.getElementById('update-apply-btn');
  try {
    const st = await fetch('/api/update').then(r => r.json());
    if (st.state === 'error') { renderUpdateState(st); return; }
    if (st.state === 'downloading' || st.state === 'verifying') {
      btn.textContent = 'Downloading…';
    } else if (st.state === 'applying') {
      btn.textContent = 'Restarting…';
    }
    setTimeout(_pollUpdateProgress, 1000);
  } catch (e) {
    // Server went away — the swap + relaunch is happening. Reload until
    // the new version answers.
    btn.textContent = 'Restarting…';
    _reloadWhenBack(20);
  }
}

function _reloadWhenBack(attempts) {
  if (attempts <= 0) {
    setStatus('The app is restarting — reload this page in a moment.', 'var(--org)');
    return;
  }
  setTimeout(async () => {
    try {
      await fetch('/api/health', {cache: 'no-store'});
      location.reload();
    } catch (e) {
      _reloadWhenBack(attempts - 1);
    }
  }, 1500);
}

function dismissUpdateBanner() {
  _updateDismissed = true;
  document.getElementById('update-banner').hidden = true;
}

async function checkForUpdatesNow() {
  const msg = document.getElementById('update-check-msg');
  msg.style.color = 'var(--muted)';
  msg.textContent = 'Checking…';
  try {
    const st = await fetch('/api/update?refresh=1').then(r => r.json());
    if (st.state === 'dev') {
      msg.textContent = 'Running from source — self-update is disabled.';
    } else if (st.state === 'available') {
      msg.style.color = 'var(--grn)';
      msg.textContent = 'Version ' + st.latest + ' available — see the banner above.';
      _updateDismissed = false;
      renderUpdateState(st);
    } else {
      msg.style.color = 'var(--grn)';
      msg.textContent = 'You are up to date.';
    }
  } catch (e) {
    msg.style.color = 'var(--org)';
    msg.textContent = 'Could not reach GitHub — check your connection.';
  }
}

loadUpdateState();
setInterval(loadUpdateState, 60 * 60 * 1000);   // re-render if a check lands later
```

- [ ] **Step 5: Verify the page serves with the new elements**

```bash
cd "/Users/fynnj/Documents/Claude/Projects/Pro Proprestor app copy"
pkill -9 -f propresenter_app.py 2>/dev/null; sleep 1
nohup python3 propresenter_app.py --headless > /tmp/upd_smoke.log 2>&1 & disown
sleep 5
curl -sS localhost:5757/ | grep -c 'update-banner\|update-apply-btn\|checkForUpdatesNow'
curl -sS localhost:5757/api/update
curl -sS -o /dev/null -w '%{http_code}\n' -X POST localhost:5757/api/update/apply
curl -sS -X POST localhost:5757/api/quit >/dev/null
```

Expected: grep count ≥ 3; `GET /api/update` → `{"current":"2.2.0","state":"dev"}`; POST → `409`.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add templates/index.html static/app.js static/app.css
git commit -m "feat(updater): update banner, progress UI, Settings check row"
```

---

### Task 9: release.yml — stable assets + SHA256SUMS

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Zip the .app in `build-mac`**

In the `build-mac` job, directly AFTER the "Build .app + .dmg" step, add:

```yaml
      - name: Zip .app for the self-updater
        # ditto preserves executable bits + symlinks that a plain `zip`
        # (and Python's zipfile) can mangle; the in-app updater extracts
        # with `ditto -x -k` for the same reason.
        run: ditto -c -k --keepParent "dist/Runsheet Pilot.app" "dist/Runsheet-Pilot-mac.zip"
```

and extend the mac upload-artifact step's `path` to:

```yaml
          path: |
            dist/Runsheet-Pilot-*.dmg
            dist/Runsheet-Pilot-mac.zip
```

- [ ] **Step 2: Stable-named exe in `build-windows`**

In the `build-windows` job, directly AFTER the "Build .exe with PyInstaller" step, add:

```yaml
      - name: Stable-named exe for the self-updater
        shell: pwsh
        run: Copy-Item "dist\Runsheet Pilot.exe" "dist\Runsheet-Pilot-windows.exe"
```

and extend the windows upload-artifact step's `path` to:

```yaml
          path: |
            dist/Runsheet Pilot.exe
            dist/Runsheet-Pilot-windows.exe
```

- [ ] **Step 3: Generate SHA256SUMS.txt in the `release` job**

Directly AFTER the "List downloaded artifacts" step, add:

```yaml
      - name: Generate SHA256SUMS over stable assets
        # Covers whichever stable assets exist — the Windows exe may be
        # absent when its non-blocking build fails. The updater treats a
        # missing sum entry the same as a missing asset: no update offered
        # on that platform.
        run: |
          mkdir -p stable
          find artifacts -type f -name 'Runsheet-Pilot-*' -exec cp {} stable/ \;
          cd stable
          ls -la
          sha256sum Runsheet-Pilot-* > SHA256SUMS.txt
          cat SHA256SUMS.txt
```

and extend the `softprops/action-gh-release` step's `files:` list to:

```yaml
          files: |
            artifacts/windows-exe/Runsheet Pilot.exe
            artifacts/mac-dmg/*.dmg
            stable/Runsheet-Pilot-*
            stable/SHA256SUMS.txt
```

- [ ] **Step 4: Validate YAML**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml')); print('YAML OK')"`
Expected: `YAML OK`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): stable-named updater assets + SHA256SUMS.txt"
```

---

### Task 10: Version bump, checklist, PR

**Files:**
- Modify: `propresenterrunsheet/config.py` (VERSION)
- Modify: `RELEASE_CHECKLIST.md`

- [ ] **Step 1: Bump VERSION**

In `propresenterrunsheet/config.py` change:

```python
VERSION = "2.3.0"
```

- [ ] **Step 2: Add checklist steps**

In `RELEASE_CHECKLIST.md`, after the "## 7. Tag the release" section's last bullet, add:

```markdown
- [ ] Release has the self-updater assets: `Runsheet-Pilot-mac.zip`,
      `Runsheet-Pilot-windows.exe` (if the Windows build passed), and
      `SHA256SUMS.txt`
- [ ] **Update-path check (Windows — the production machine):** with the
      PREVIOUS version installed and running, the update banner appears;
      one click downloads, restarts, and lands on the new version; the
      `.exe.old` leftover is gone after the restart
```

- [ ] **Step 3: Full suite + push + PR**

```bash
python3 -m pytest tests/ -q
git add propresenterrunsheet/config.py RELEASE_CHECKLIST.md
git commit -m "chore: bump to 2.3.0, release-checklist update steps"
git push -u origin claude/self-update
gh pr create --title "One-click self-update via GitHub Releases" --base main --head claude/self-update --body "Implements docs/superpowers/specs/2026-07-02-self-update-design.md. Check on launch -> banner -> download -> SHA-256 verify -> rename-swap -> relaunch. Windows (production target) logic is unit-proven via pure op plans; Mac verified locally. Release workflow now publishes stable assets + SHA256SUMS.txt. First self-updating release will be v2.3.0."
```

Expected: all tests pass, PR opens cleanly.

---

## Manual verification after merge (cannot be automated from the Mac)

1. Merge PR → tag `v2.3.0` → confirm the release carries the three new assets.
2. Mac: install v2.3.0 from the .dmg. Later, tag a trivial `v2.3.1`; in the running app the banner appears (or Settings → Check for updates) → click → app restarts on 2.3.1, `.app.old` gone.
3. **Windows (church PC, required):** same drill with the `.exe` — this is the production path and the one step this Mac cannot prove.
