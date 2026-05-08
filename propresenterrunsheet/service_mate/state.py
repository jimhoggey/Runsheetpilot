"""Service Mate persistent state.

Two JSON files live in the user's app-data folder:
  runsheet_state.json  — current cue index, items, auto-track state, standby flag
  clocks.json          — GeekMagic clock IPs, role assignments, brightness

Both are read by the daemon every tick and written by the route handlers; the
read/write pair uses a temp-file + atomic replace so a crash mid-write can't
leave a corrupt JSON file behind.

`RUNSHEET_STATE_FILE` and `CLOCKS_CONFIG_FILE` are exposed as module-level
attributes so tests can monkeypatch them onto an isolated tmpdir
(see tests/conftest.py)."""

import json
import logging

from ..config import DATA_DIR

from .constants import ROLE_CUE_TABLES, SM_VERBOSITY_DEFAULT


log = logging.getLogger("pp_runsheet")


RUNSHEET_STATE_FILE = DATA_DIR / "runsheet_state.json"
CLOCKS_CONFIG_FILE  = DATA_DIR / "clocks.json"


def _default_clocks_config() -> dict:
    return {
        "clocks": [
            {"id": "screen", "ip": "", "role": "screen",
             "name": "Screen station", "verbosity": SM_VERBOSITY_DEFAULT},
            {"id": "sound",  "ip": "", "role": "sound",
             "name": "Sound station",  "verbosity": SM_VERBOSITY_DEFAULT},
            {"id": "lights", "ip": "", "role": "lights",
             "name": "Lights station", "verbosity": SM_VERBOSITY_DEFAULT},
        ],
        "brightness": 70,
        "enabled":    True,
    }


def _read_runsheet_state() -> dict:
    if not RUNSHEET_STATE_FILE.exists():
        return {}
    try:
        return json.loads(RUNSHEET_STATE_FILE.read_text())
    except Exception:
        log.exception("Failed to read runsheet_state.json — ignoring")
        return {}


def _write_runsheet_state(state: dict) -> None:
    tmp = RUNSHEET_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(RUNSHEET_STATE_FILE)


def _read_clocks_config() -> dict:
    if not CLOCKS_CONFIG_FILE.exists():
        return _default_clocks_config()
    try:
        cfg = json.loads(CLOCKS_CONFIG_FILE.read_text())
        merged = _default_clocks_config()
        merged.update({k: v for k, v in cfg.items() if v is not None})
        return merged
    except Exception:
        log.exception("Failed to read clocks.json — using defaults")
        return _default_clocks_config()


def _write_clocks_config(cfg: dict) -> None:
    tmp = CLOCKS_CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CLOCKS_CONFIG_FILE)


def _cue_for(role: str, item: dict) -> str:
    """Return the short cue line to show for an item, for a given role.
    Prefers LLM-generated cues stored on the item under `cues.{role}`; else
    falls through to the rule table by item type."""
    if not item:
        return ""
    cues = item.get("cues") or {}
    text = (cues.get(role) or "").strip()
    if text:
        return text[:60]
    table = ROLE_CUE_TABLES.get(role, {})
    t = (item.get("type") or "other").lower()
    return table.get(t, "Get ready")


def _ensure_item_cues(item: dict) -> dict:
    """Ensure the item has cues for all three roles (LLM-fed or fallback)."""
    cues = dict(item.get("cues") or {})
    for role in ("screen", "sound", "lights"):
        if not (cues.get(role) or "").strip():
            cues[role] = _cue_for(role, item)
    item["cues"] = cues
    return item


def _next_visible_item(items, idx):
    """The next runsheet item after idx, or None at the end."""
    if 0 <= idx < len(items) - 1:
        return items[idx + 1]
    return None
