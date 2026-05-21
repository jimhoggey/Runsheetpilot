"""App-wide constants and the user-data directory.

Imported by every other submodule that needs to know where to write files
or what version we are. Kept dependency-free (stdlib only) so it can be
imported from anywhere without import-cycle worries.

`DATA_DIR` resolves the platform-appropriate writable location:
  Mac:     ~/Library/Application Support/ProPresenter Runsheet Builder/
  Windows: %APPDATA%\\ProPresenter Runsheet Builder\\
  Linux:   $XDG_CONFIG_HOME (or ~/.config) /ProPresenter Runsheet Builder/

Must work both when running as a script AND inside a frozen PyInstaller
bundle, where writing next to the executable is not allowed on signed
Mac bundles and is fragile on Windows."""

import os
import sys
import tempfile
from pathlib import Path


VERSION = "2.0.0"
APP_NAME = "ProPresenter Runsheet Builder"
DEFAULT_PORT = 5757
PORT_RANGE = 20  # try DEFAULT_PORT..DEFAULT_PORT+PORT_RANGE-1


def _user_data_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / APP_NAME
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


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
