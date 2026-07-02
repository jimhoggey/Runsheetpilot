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
