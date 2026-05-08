"""Read/write the user's settings.json.

Defaults pull the local ProPresenter library directory from
propresenter.paths so first-time users don't have to type a path.
The save path is atomic (tmp file + replace) so a crash mid-write
can't leave a corrupt JSON file behind."""

import json
import logging
from pathlib import Path

from .config import SETTINGS_FILE
from .propresenter.paths import find_library_dirs, find_pp_root


log = logging.getLogger("pp_runsheet")


def _default_settings() -> dict:
    pp_root = find_pp_root()
    lib_dirs = find_library_dirs(pp_root)
    return {
        "pp_host":       "localhost",
        "pp_port":       "50001",
        "or_key":        "",
        "or_model":      "google/gemini-2.0-flash-exp:free",
        "library_dir":   lib_dirs[0] if lib_dirs else "",
        "export_dir":    str(Path.home() / "Desktop"),
        "threshold":     0.55,
        "lib_source":    "disk",
        "create_timers": True,
        # User-customised AI prompt. Empty string = use built-in DEFAULT_PROMPT.
        "ai_prompt":     "",
        # Reusable-templates feature (planned) — list of saved PP playlist
        # references the user marks as templates. UI lands in a future change.
        "templates":     [],
    }


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            merged = _default_settings()
            merged.update({k: v for k, v in saved.items() if v is not None})
            return merged
        except Exception:
            log.exception("Failed to read settings.json — using defaults")
    return _default_settings()


def save_settings(data: dict) -> None:
    # Preserve any keys already on disk that weren't sent in the patch
    existing = {}
    if SETTINGS_FILE.exists():
        try:
            existing = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            existing = {}
    existing.update(data or {})
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    tmp.replace(SETTINGS_FILE)
