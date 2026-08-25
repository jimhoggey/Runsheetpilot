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


VERSION = "2.15.0"
APP_NAME = "Runsheet Pilot"

# Release notes, newest first. ONE source of truth: the what's-new popup
# shown after an update reads the entry for the running VERSION, and the
# version badge reads the most recent few for its "what changed lately"
# popup.
#
# THE RELEASE RULE: every release adds an entry here — max three bullets,
# short, operator-facing, in the app's own voice (see the idle greetings in
# app.js for the register), MOST IMPACTFUL FIRST. That ordering is load
# bearing now: the badge popup surfaces each version's first bullet as its
# headline, so bullet one is what a returning operator actually reads.
# The same lines go in the GitHub release body.
# tests/test_whats_new.py enforces the cap AND that VERSION has an entry.
RELEASE_NOTES = {
    "2.15.0": [
        "Behind by a few versions? Updating now takes you straight to the "
        + "newest one. It works out which release is actually newest "
        + "instead of trusting GitHub to say so.",
        "Click the version number, top right, to see what changed in the "
        + "last few releases.",
    ],
    "2.14.1": [
        "The anonymous usage stats now record WHICH setting you changed — "
        + "the name of it, never what you typed. Your API key, licence key "
        + "and folder paths still never leave this machine.",
    ],
    "2.14.0": [
        "Auto won't reach for a template that isn't yours any more. A young "
        + "adults runsheet no longer comes back full of youth media just "
        + "because youth is the only template you've built.",
        "It works out which service you're running from the top of the "
        + "runsheet, where you already write it.",
        "No template for this service yet? Still fine — you get your "
        + "headers, timers and songs, and a line saying why there's no "
        + "template media.",
    ],
    "2.13.0": [
        "Service Mate clocks can now run new firmware that counts down on "
        + "the device itself. No more stutter, and every clock ticks "
        + "together.",
        "Your existing clocks keep working exactly as they are — the app "
        + "spots which kind each one is and talks to it the right way.",
        "Cues can say more than one thing per station now, and the clock "
        + "rotates through them.",
    ],
    "2.12.1": [
        "macOS was silently blocking Service Mate from reaching your "
        + "clocks. The app now asks permission properly — say yes.",
        "Clock errors say what actually went wrong instead of pasting a "
        + "Python traceback at you.",
        "If macOS is the one blocking it, I now tell you exactly which "
        + "setting to flip.",
    ],
    "2.12.0": [
        "Fund your OpenRouter key and a short list of recommended models "
        + "appears, priced per runsheet. Or paste any model you like.",
        "Automatic still only ever uses free models — it will never start "
        + "spending without being asked.",
        "Every model is checked against OpenRouter as you open the list, "
        + "and I now record which one ran and what it cost.",
    ],
}


def _version_key(v: str) -> tuple:
    """'2.15.0' -> (2, 15, 0). Unparseable sorts last, never first."""
    try:
        return tuple(int(p) for p in str(v).split("."))
    except ValueError:
        return (-1,)


def recent_release_notes(limit: int = 3) -> list:
    """The newest `limit` releases as [{version, notes, headline}], newest
    first.

    Sorted by parsed version rather than trusting dict insertion order —
    an entry added in the wrong place would otherwise silently reorder the
    operator's changelog.
    """
    out = []
    for ver in sorted(RELEASE_NOTES, key=_version_key, reverse=True)[:limit]:
        notes = [n for n in RELEASE_NOTES.get(ver) or [] if n and n.strip()]
        if notes:
            out.append({"version": ver, "notes": notes[:3],
                        "headline": notes[0]})
    return out


# Shown once by the what's-new popup on the first launch after an update.
# .get() not [] on purpose: a VERSION bump with no notes yet must not stop
# the app from booting. The test suite is what catches the omission.
WHATS_NEW = RELEASE_NOTES.get(VERSION, [])
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
            # Explicit utf-8 both ways: read_text() uses the locale codec,
            # which is cp1252 on Windows, and a saved `ai_prompt` carries
            # em-dashes and arrows. That decode raised, the bare `except`
            # below swallowed it, and the migration silently did nothing.
            SETTINGS_FILE.write_text(old.read_text(encoding="utf-8"),
                                     encoding="utf-8")
    except Exception:
        pass
