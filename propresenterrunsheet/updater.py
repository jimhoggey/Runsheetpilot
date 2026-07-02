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
