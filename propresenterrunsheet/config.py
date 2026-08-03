"""App-wide constants and the user-data directory.

Imported by every other submodule that needs to know where to write files
or what version we are. Kept dependency-free (stdlib only) so it can be
imported from anywhere without import-cycle worries.

`DATA_DIR` resolves the platform-appropriate writable location:
  Mac:     ~/Library/Application Support/Runsheet Pilot/
  Windows: %APPDATA%\\Runsheet Pilot\\
  Linux:   $XDG_CONFIG_HOME (or ~/.config) /Runsheet Pilot/

Must work both when running as a script AND inside a frozen PyInstaller
bundle, where writing next to the executable is not allowed on signed
Mac bundles and is fragile on Windows.

Renamed v2.0.3 → v2.1.0: the app was previously called "ProPresenter
Runsheet Builder". `_LEGACY_APP_NAME` and the migration block below
move existing users' settings/logs into the new folder on first launch.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path


VERSION = "2.3.7"
APP_NAME = "Runsheet Pilot"
# Old name kept solely for the one-time DATA_DIR migration. Do not use
# in any UI / log / build flag — that's what APP_NAME is for.
_LEGACY_APP_NAME = "ProPresenter Runsheet Builder"
DEFAULT_PORT = 5757
PORT_RANGE = 20  # try DEFAULT_PORT..DEFAULT_PORT+PORT_RANGE-1


def _platform_data_root() -> Path:
    """Return the platform's user-data root (not including the app folder)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or str(Path.home()))
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def _user_data_dir() -> Path:
    """Return the per-user data dir for THIS app, migrating from the
    legacy "ProPresenter Runsheet Builder" folder on first launch if it
    exists. Migration is best-effort: any failure just falls through to
    a fresh empty dir — the app still works, the user just loses their
    old settings file (one-line fix from the in-app Settings dialog).
    """
    # Explicit override — points the ENTIRE data dir (settings, logs,
    # runsheet/clock state) somewhere isolated. The test suite sets this
    # so `pytest` never writes to the real app.log; without it, test runs
    # polluted the CI Windows smoke test's app.log dump and made frozen-
    # exe failures impossible to diagnose. Also handy for portable installs.
    override = os.environ.get("RUNSHEET_PILOT_DATA_DIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d

    root = _platform_data_root()
    new = root / APP_NAME
    old = root / _LEGACY_APP_NAME

    # One-time rename. Only triggers if the new folder doesn't exist AND
    # the old one does — once new/ has been created (even empty),
    # subsequent launches see it and skip migration.
    if not new.exists() and old.exists():
        try:
            shutil.move(str(old), str(new))
        except Exception:
            pass

    new.mkdir(parents=True, exist_ok=True)
    return new


DATA_DIR = _user_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "app.log"
UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "pp_runsheet_uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# One-time migration: if the old in-tree settings file exists and the new
# one doesn't, copy it across so existing users don't lose their config.
if not getattr(sys, "frozen", False):
    try:
        # The package is one level deep from the project root, so step up
        # twice to find any legacy settings.json next to propresenter_app.py.
        old = Path(__file__).resolve().parent.parent / "settings.json"
        if old.exists() and not SETTINGS_FILE.exists():
            SETTINGS_FILE.write_text(old.read_text())
    except Exception:
        pass
