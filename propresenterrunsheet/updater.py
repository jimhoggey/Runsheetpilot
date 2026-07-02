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
