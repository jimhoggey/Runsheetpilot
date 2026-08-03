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
        # Empty means "choose automatically" — resolved at parse time from
        # OpenRouter's live catalogue (see parsing/models.py). This used to
        # hardcode "google/gemini-2.0-flash-exp:free"; OpenRouter retired that
        # model, so every install that never touched this setting began failing
        # with a 404 that only a new release could fix. Naming any single model
        # here would eventually repeat that.
        "or_model":      "",
        "library_dir":   lib_dirs[0] if lib_dirs else "",
        "export_dir":    str(Path.home() / "Desktop"),
        "threshold":     0.55,
        # Library auto-load mode. "auto" tries PP REST first then falls back
        # to disk — works for the most operators out of the box. Was "disk"
        # before the UI redesign; switched to "auto" so the Settings modal
        # radio defaults to the recommended option for first-time users.
        "lib_source":    "auto",
        "create_timers": True,
        # User-customised AI prompt. Empty string = use built-in DEFAULT_PROMPT.
        "ai_prompt":     "",
        # UUID of the PP playlist used as a template at parse time. When
        # set (or auto-detected — first playlist whose name contains
        # "library" / "template"), the parser feeds the template's
        # section header names into the LLM prompt and expands matching
        # runsheet items into the template's media items at playlist-
        # build time. Empty string = no template (parse normally).
        "template_playlist_uuid": "",
        # Reusable-templates feature (planned) — list of saved PP playlist
        # references the user marks as templates. UI lands in a future change.
        "templates":     [],
        # Service Mate paid add-on (see propresenterrunsheet/licensing.py).
        # license_key: the buyer's signed RP1 key, empty until they activate.
        # sm_trial_start: ISO date the 14-day trial began (stamped on first
        # active use); empty means the trial hasn't started yet.
        "license_key":     "",
        "sm_trial_start":  "",
        # Hide the Service Mate panel from the main screen entirely —
        # for operators who don't own a GeekMagic clock. UI-only flag;
        # the daemon's own enabled switch lives in clocks.json.
        "sm_hide":         False,
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
