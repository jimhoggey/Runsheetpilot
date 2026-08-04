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


# ── Swap plan + executor ────────────────────────────────────────────────────
def test_plan_swap_windows_exact_operation_order():
    exe = Path("C:/Users/av/Desktop/Runsheet Pilot.exe")
    old = Path("C:/Users/av/Desktop/Runsheet Pilot.exe.old")
    new = Path("C:/Users/av/AppData/Roaming/Runsheet Pilot/updates/Runsheet-Pilot-windows.exe")
    ops = updater.plan_swap(exe, new, "win32")
    assert ops == [
        ("rename", exe, old),
        ("move", new, exe),
        # Hand off to a relaunch script (carries `old` so it can delete the
        # previous binary) instead of spawning the new exe as a child of the
        # still-dying old process.
        ("relaunch_windows", exe, old),
        ("exit",),
    ]


def test_windows_relaunch_script_waits_then_starts_and_self_deletes():
    exe = "C:/Users/av/Desktop/Runsheet Pilot.exe"
    old = "C:/Users/av/Desktop/Runsheet Pilot.exe.old"
    script = updater._windows_relaunch_script(exe, old, pid=4242)
    # waits for OUR pid to be gone before doing anything
    assert 'tasklist /FI "PID eq 4242"' in script
    assert "goto wait" in script
    # sleeps via ping (timeout needs a console; detached procs have none)
    assert "ping -n 2 127.0.0.1" in script
    assert "timeout" not in script
    # then starts the NEW exe, deletes the old binary, deletes itself
    assert f'start "" "{exe}"' in script
    assert f'del "{old}"' in script
    assert 'del "%~f0"' in script


def test_default_spawn_relaunch_windows_writes_batch_and_launches_cmd(upd_env, monkeypatch):
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)) or object())
    monkeypatch.setattr(updater.os, "getpid", lambda: 777)
    exe = Path("C:/x/Runsheet Pilot.exe")
    old = Path("C:/x/Runsheet Pilot.exe.old")
    updater._default_spawn(("relaunch_windows", exe, old))
    bat = updater.UPDATES_DIR / "relaunch.bat"
    assert bat.exists()
    text = bat.read_text()
    assert "PID eq 777" in text
    # Use str(exe)/str(old), not a hardcoded forward-slash literal — the
    # script embeds str(Path), which is backslashes on Windows and forward
    # slashes on the Mac dev box. (Backslashes are correct for Windows cmd.)
    assert f'start "" "{exe}"' in text
    assert f'del "{old}"' in text
    # launched via `cmd /c <bat>`, detached
    argv = calls[0][0][0]
    assert argv[0] == "cmd" and argv[1] == "/c" and argv[2].endswith("relaunch.bat")


def test_plan_swap_mac_hands_off_to_relaunch_script():
    """Mac had the same race Windows had before v2.3.5: `open` fired while
    the OLD process was still alive, so LaunchServices just activated the
    dying instance instead of launching the new binary — the old process
    then exited and the operator was left staring at nothing ("stuck at
    the relaunch state"). Same medicine: a detached script that waits for
    our pid to die before calling open."""
    app = Path("/Applications/Runsheet Pilot.app")
    new = Path("/tmp/updates/extracted/Runsheet Pilot.app")
    ops = updater.plan_swap(app, new, "darwin")
    assert ops == [
        ("rename", app, Path("/Applications/Runsheet Pilot.app.old")),
        ("move", new, app),
        ("relaunch_mac", app),
        ("exit",),
    ]


def test_mac_relaunch_script_waits_then_execs_the_binary_directly():
    """Field evidence (2026-08-04 log): relaunches "worked" but took
    39-78 seconds. Cause: `open` consults LaunchServices, which still had
    the DYING instance registered (PyInstaller's outer bootloader lingers
    after the inner process exits) — LS poked the corpse and only
    launched fresh after its not-responding timeout. Executing the
    bundle's binary directly skips LS entirely: relaunch is immediate,
    and the operator never stares at an empty Dock wondering if the
    update ate the app."""
    script = updater._mac_relaunch_script("/Applications/Runsheet Pilot.app",
                                          pid=4242)
    # spin until OUR pid is gone — kill -0 probes without signalling
    assert "kill -0 4242" in script
    assert "sleep" in script
    # then exec the binary inside the bundle — NOT `open`
    assert '"/Applications/Runsheet Pilot.app/Contents/MacOS/Runsheet Pilot"' \
        in script
    assert "open " not in script
    # breadcrumbs for the next field investigation
    assert "relaunch.log" in script
    # …and with PyInstaller's env stripped. Field evidence (relaunch.log,
    # 2026-08-04 11:38): the relaunch fired in 1s but the new process died
    # with "Failed to load Python shared library …/_MEIvGBlib/Python".
    # The child inherited _PYI_APPLICATION_HOME_DIR pointing at the DYING
    # parent's onefile extraction dir, so it skipped its own extraction —
    # then the parent deleted that dir on exit. `open` never hit this
    # because LaunchServices launches with a clean environment.
    for var in ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
                "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC",
                "_MEIPASS2"):
        assert f"-u {var}" in script, f"{var} must be stripped"


def test_default_spawn_relaunch_mac_detaches_a_shell(upd_env, monkeypatch):
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)) or object())
    monkeypatch.setattr(updater.os, "getpid", lambda: 777)
    app = Path("/Applications/Runsheet Pilot.app")
    updater._default_spawn(("relaunch_mac", app))
    argv, kwargs = calls[0][0][0], calls[0][1]
    assert argv[0] == "/bin/sh" and argv[1] == "-c"
    assert "kill -0 777" in argv[2]
    assert "Contents/MacOS" in argv[2]   # direct binary exec, not `open`
    # detached from our dying process group, or macOS reaps it with us
    assert kwargs.get("start_new_session") is True


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
    assert spawned == [("relaunch_mac", installed)]
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

    old = fake_exe.with_name("Runsheet Pilot.exe.old")
    assert updater.get_state()["state"] == "applying"
    assert executed[0] == ("rename", fake_exe, old)
    assert executed[1][2] == fake_exe                 # move lands on exe path
    assert executed[2] == ("relaunch_windows", fake_exe, old)


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
