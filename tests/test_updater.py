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
