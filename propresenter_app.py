#!/usr/bin/env python3
"""
ProPresenter Runsheet Builder
=============================
Cross-platform local web app. Reads a church service runsheet PDF, uses
OpenRouter to parse it into structured items, fuzzy-matches songs against
the local ProPresenter library, then creates a playlist via the
ProPresenter 7 REST API.

End-user delivery: bundled .app/.dmg (Mac) and .exe (Windows) via PyInstaller.
Dev usage:
  Mac:     ./launch_mac.sh   (or python3 propresenter_app.py)
  Windows: run.bat

File map (for new contributors)
───────────────────────────────
PyInstaller still uses this file as the entry point; the launchers
(launch_mac.sh / run.bat) run it directly. The UI now lives in real
files so editors give it syntax highlighting:

  templates/index.html  — page structure
  static/app.css        — styles
  static/app.js         — behaviour

The build scripts pass `--add-data` for `templates` and `static` so
the frozen .app / .exe ships them alongside the binary.

Navigate the Python by the "# ── ..." section dividers. Top-level
regions in order:
  1. Constants, user data dir, logging
  2. Flask app + global error handlers
  3. PDF extraction
  4. ProPresenter paths (cross-platform)
  5. Library scan from disk
  6. Fuzzy matching (song title → library)
  7. AI prompt template + per-type colour map
  8. Time/duration parsing + PP timer creation
  8b. Service Mate re-export shim (actual code is in
      propresenterrunsheet/service_mate/ + propresenterrunsheet/routes/)
  9. Settings load/save
 10. API routes (one block per /api/* endpoint, except /api/runsheet/*
     and /api/clocks/* which are blueprints in propresenterrunsheet/routes)
 11. Server bootstrap (port, browser, waitress, clocks loop)

Common feature touch-points:
  - new API endpoint    → region 10  + JS caller in static/app.js
  - new UI panel        → templates/index.html + JS handler in static/app.js
  - new settings field  → _default_settings() in region 9 + UI in templates
  - new runsheet type   → DEFAULT_PROMPT (region 7), TYPE_COLORS (region 7),
                          tagClass() in static/app.js, .tag-* in static/app.css,
                          *_CUES rule tables in service_mate/constants.py
  - clock layout tweak  → service_mate/render.py
"""

import datetime as _dt
import json
import logging
import os
import re
import shutil
import socket
import sys
import difflib
import threading
import time
import webbrowser
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template

VERSION = "1.0.0"
APP_NAME = "ProPresenter Runsheet Builder"
DEFAULT_PORT = 5757
PORT_RANGE = 20  # try DEFAULT_PORT..DEFAULT_PORT+PORT_RANGE-1


# ── User data dir (settings, log) ─────────────────────────────────────────────
# Must work both as a script AND inside a frozen PyInstaller bundle (where
# writing next to the executable is not allowed on Mac signed bundles and is
# fragile on Windows).

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

# One-time migration: if the old in-tree settings file exists and the new one
# doesn't, copy it across so existing users don't lose their config.
if not getattr(sys, "frozen", False):
    try:
        old = Path(__file__).resolve().parent / "settings.json"
        if old.exists() and not SETTINGS_FILE.exists():
            SETTINGS_FILE.write_text(old.read_text())
    except Exception:
        pass


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("pp_runsheet")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


log = _setup_logging()


# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB cap on PDF upload


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "PDF too large (limit 25 MB)."}), 413


@app.errorhandler(Exception)
def _unhandled(e):
    log.exception("Unhandled exception in request")
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ── PDF extraction ────────────────────────────────────────────────────────────

def extract_pdf_text(path: str) -> str:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


# ── Cross-platform ProPresenter paths ─────────────────────────────────────────

def _pp_candidates():
    if sys.platform == "darwin":
        return [
            Path.home() / "Documents" / "ProPresenter",
            Path.home() / "ProPresenter",
            Path("/Users/Shared/ProPresenter"),
        ]
    return [
        Path.home() / "Documents" / "ProPresenter",
        Path("C:/Users/Public/Documents/ProPresenter"),
        Path.home() / "Documents" / "RenewedVision" / "ProPresenter",
    ]


def find_pp_root() -> str:
    for p in _pp_candidates():
        if p.exists():
            return str(p)
    return str(Path.home() / "Documents" / "ProPresenter")


def find_library_dirs(pp_root: str) -> list:
    lib = Path(pp_root) / "Libraries"
    if not lib.exists():
        return []
    return [str(d) for d in sorted(lib.iterdir()) if d.is_dir()]


def find_playlist_dir(pp_root: str):
    p = Path(pp_root) / "Playlists"
    return str(p) if p.exists() else None


# ── Library scan from disk ────────────────────────────────────────────────────

_UUID_RE = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _uuid_from_binary(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)  # UUID is in the header; no need to read whole file
        m = _UUID_RE.search(data)
        return m.group().decode() if m else ""
    except Exception:
        return ""


def scan_library(directory: str) -> list:
    items = []
    for i, pro in enumerate(sorted(Path(directory).rglob("*.pro"))):
        items.append({"name": pro.stem, "uuid": _uuid_from_binary(pro), "index": i})
    return items


# ── Fuzzy matching ────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", "", (s or "").lower().strip())


def fuzzy_match(name: str, items: list, threshold: float = 0.55):
    best_score, best = 0.0, None
    nn = _norm(name)
    for item in items:
        s = difflib.SequenceMatcher(None, nn, _norm(item.get("name", ""))).ratio()
        if s > best_score:
            best_score, best = s, item
    return (best, best_score) if best_score >= threshold else (None, best_score)


# ── AI prompt + per-type colour map ───────────────────────────────────────────

# The default prompt the AI receives. The {RUNSHEET} placeholder is replaced
# with the extracted PDF text before sending. Users can override this in the
# UI ("📝 Edit AI Prompt" button) — their version is persisted in settings.
DEFAULT_PROMPT = """\
You are analysing a church service runsheet (order of service).

## WHAT TO SKIP
Most runsheets have a "rostering" section at the very top that lists who
is doing each role (lines like "Pre Service Prayer: Grant, Rebekah",
"Worship Leader: Chitsaka, Pascar", "Speaker: Hind, Nick", "ML Open: ...").
This is just credits — IGNORE IT COMPLETELY.

The actual service begins at the FIRST item that has a specific time-of-day
(e.g. "9:24 AM"). Start extracting from THAT item onward.

Also IGNORE any footer sections that come AFTER the last service item —
typically things like "Rehearsal Times", "Songs", "Tech Notes", lists of
upcoming dates that aren't part of the service flow, etc.

## RETURN FORMAT — JSON object only, no markdown:

{
  "service_name": "<short name combining the service title and date,
                    e.g. 'Sunday Service — 3 May 2026'>",
  "items": [
    {"type":         "<see TYPES below>",
     "title":        "...",
     "notes":        "...",
     "duration_min": <integer minutes, or 0 if not specified>}
  ]
}

## DURATION_MIN
Most runsheets list a duration next to each item (e.g. "9:30 AM 20 Worship
and Ministry Time" — the 20 is duration in minutes; "10:14 AM 30 Preach Title"
— 30 minutes). Always extract this as an integer in `duration_min`.
Use 0 if there's no explicit duration. This field drives countdown-timer
creation in ProPresenter.

## TYPES — choose carefully

- song          ONLY actual sung worship songs the band/team performs.
                Examples: "Amazing Grace", "Alleluia", "The King Is In The
                Room". Often listed back-to-back with short or zero duration.
                ⚠ DO NOT use "song" for items that mention a person's name —
                those are MC moments, not songs.

- mc_on_stage   A person stepping on stage to lead a transition or open/land
                a section. ALMOST ALWAYS has a person's name with a dash.
                Examples: "Land Worship - Lauren", "Welcome - John",
                "Open Service - Mary", "Meeting Land and Recap - Matt".

- announcement  Speaker giving information to the congregation.
                Examples: "Junior Youth Out", "Upcoming Dates",
                "Welcome and Connection Cards", "Celebrations",
                "Whats Your Next Step Moment".

- sermon        The main preaching / message slot. Look for "Preach Title",
                "Message", or a minister's name with a sermon topic.

- prayer        Prayer time / altar call / ministry moment.

- scripture     A bible reading. The `title` MUST be the bible reference in
                a clean form: "Genesis 1:23-28", "John 3:16",
                "1 Corinthians 13:4-7". Detect references like "Bible
                Genesis 1:23-28", "Read John 3:16", "Scripture: Romans 8:28"
                — strip the leading word, just keep the reference.

- offering      Offering / tithe / giving moment.

- video         A pre-recorded video clip is being played.

- other         Section dividers (e.g. "Praise and Worship", "Culture Focus",
                "Land Service"), countdowns, music beds, anything that
                doesn't fit above.

## NOTES FIELD
Include any time-of-day (e.g. "9:30 AM") and speaker names in the notes
field. The duration goes in `duration_min`, NOT in notes.
Use empty string ("") if there is no extra info.

## EXAMPLE
{"service_name":"Sunday Service — 3 May 2026",
 "items":[
   {"type":"other","title":"Go live - online streaming","notes":"9:24 AM","duration_min":1},
   {"type":"other","title":"Countdown - Start 9:27am","notes":"9:25 AM","duration_min":5},
   {"type":"other","title":"Worship and Ministry Time","notes":"9:30 AM","duration_min":20},
   {"type":"song","title":"Alleluia","notes":"9:50 AM","duration_min":0},
   {"type":"song","title":"The King Is In The Room","notes":"","duration_min":0},
   {"type":"song","title":"Jesus Be The Name","notes":"","duration_min":0},
   {"type":"mc_on_stage","title":"Land Worship - Lauren","notes":"9:50 AM","duration_min":5},
   {"type":"scripture","title":"Genesis 1:23-28","notes":"9:55 AM","duration_min":2},
   {"type":"announcement","title":"Welcome and Connection Cards","notes":"9:55 AM","duration_min":5},
   {"type":"announcement","title":"Culture Moment - Generosity - Ps Melissa","notes":"10:00 AM","duration_min":10},
   {"type":"announcement","title":"Junior Youth Out","notes":"10:10 AM","duration_min":1},
   {"type":"sermon","title":"Preach: King Jesus - Ps Nick","notes":"10:14 AM","duration_min":30},
   {"type":"prayer","title":"Altar Call/Ministry Moment","notes":"10:44 AM","duration_min":5},
   {"type":"mc_on_stage","title":"Meeting Land and Recap - Matt","notes":"10:49 AM","duration_min":2},
   {"type":"announcement","title":"Upcoming Dates","notes":"10:53 AM","duration_min":5}
 ]}

RUNSHEET:
---
{RUNSHEET}
---
"""

# RGBA (0-1 floats) for ProPresenter playlist header items, by type.
# Mirrors the tag colours in the UI table so volunteers see the same colour
# in the builder and in PP.
TYPE_COLORS = {
    "song":         (0.06, 0.24, 0.55, 1.0),  # blue
    "mc_on_stage":  (0.05, 0.45, 0.50, 1.0),  # teal
    "announcement": (0.85, 0.50, 0.05, 1.0),  # amber
    "sermon":       (0.45, 0.20, 0.65, 1.0),  # purple
    "prayer":       (0.55, 0.35, 0.75, 1.0),  # lavender
    "scripture":    (0.15, 0.50, 0.30, 1.0),  # green
    "offering":     (0.70, 0.55, 0.10, 1.0),  # gold
    "video":        (0.55, 0.20, 0.20, 1.0),  # rust
    "other":        (0.30, 0.30, 0.40, 1.0),  # gray
}
ACTION_NEEDED_COLOR = (0.86, 0.15, 0.15, 1.0)  # bright red


def _color_for_type(t: str) -> dict:
    r, g, b, a = TYPE_COLORS.get(t or "other", TYPE_COLORS["other"])
    return {"red": r, "green": g, "blue": b, "alpha": a}


def _color_dict(rgba: tuple) -> dict:
    r, g, b, a = rgba
    return {"red": r, "green": g, "blue": b, "alpha": a}


# ── Time + duration parsing for runsheet → PP timers ─────────────────────────

# Matches "9:24 AM", "9:24am", "12:30 PM", etc. — the AM/PM marker is required
# so we don't accidentally match e.g. a chord "G2:4" or a note like "for 35:00".
# We still extract time-of-day for display purposes (it goes in the timer name
# so the operator can find the right timer at the right moment), but timers
# themselves are duration-based since the runsheet is uploaded days ahead.
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")

# Matches durations like "20 min", "20min", "20 minutes", "20m", "(20 min)".
_DURATION_RE = re.compile(r"(\d+)\s*(?:min(?:ute)?s?|m\b)", re.IGNORECASE)


def _extract_time_str(text: str) -> str:
    """Return the time-of-day as a display string (e.g. '9:30 AM') or ''."""
    if not text:
        return ""
    m = _TIME_RE.search(text)
    if not m:
        return ""
    h, mn, p = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if not (1 <= h <= 12 and 0 <= mn <= 59):
        return ""
    return f"{h}:{mn:02d} {p}"


def _extract_duration_min(parsed_item: dict) -> int:
    """Find the duration in whole minutes for a parsed runsheet item.

    Order of precedence:
      1. Explicit `duration_min` field returned by the AI (preferred).
      2. Regex match on the `notes` field ("20 min", "30 minutes", etc.).
      3. Regex match on the `title` field as a last resort.
    Returns 0 if no duration found / 0-duration item — caller should skip."""
    raw = parsed_item.get("duration_min")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    for field in ("notes", "title"):
        m = _DURATION_RE.search(parsed_item.get(field, "") or "")
        if m:
            n = int(m.group(1))
            if 0 < n < 24 * 60:  # sanity cap: under 24 h
                return n
    return 0


# All timers we create are prefixed with this marker so we can safely delete
# them on the next run without touching the user's own timers.
_RB_TIMER_PREFIX = "[RB] "


def _delete_existing_rb_timers(base: str) -> int:
    """Delete every timer in PP whose name starts with [RB] (i.e. created by
    a previous run of this app). Never touches user-created timers."""
    import requests as req
    deleted = 0
    try:
        r = req.get(f"{base}/v1/timers", timeout=6)
        if not r.ok:
            return 0
        for t in r.json():
            name = (t.get("id") or {}).get("name", "")
            uuid = (t.get("id") or {}).get("uuid")
            if not uuid or not name.startswith(_RB_TIMER_PREFIX):
                continue
            try:
                d = req.delete(f"{base}/v1/timer/{uuid}", timeout=4)
                if d.ok:
                    deleted += 1
                    log.info(f"Deleted old timer: {name}")
            except Exception:
                log.exception(f"Failed to delete timer {uuid}")
    except Exception:
        log.exception("Failed to list timers for cleanup")
    return deleted


def _create_pp_timers(base: str, playlist_name: str, matched: list) -> dict:
    """Cleanup previous [RB] timers, then create one *duration-based* countdown
    timer in PP for every matched item that has a duration.

    Duration-based (not count-down-to-time) because runsheets are typically
    uploaded days before the service — the time-of-day in the runsheet is the
    *planned* slot, not when the operator will actually start the timer.

    The runsheet's time-of-day is included in the timer name as a hint so the
    operator can quickly find the right timer at the right moment.

    Returns {created, deleted, no_duration, total_items, errors}."""
    import requests as req
    deleted = _delete_existing_rb_timers(base)

    created, no_duration, total_items, errors = 0, 0, 0, []
    # Map runsheet-item index (0-based) → exact timer name we created for it.
    # Used by the Service Mate auto-track to identify the running [RB] timer.
    timer_names: dict = {}
    for idx, mi in enumerate(matched, start=1):
        p = mi.get("parsed") or {}
        # Section dividers / songs / items the operator never times → skip.
        # Only items the worship/host actually needs to track time on.
        ptype = (p.get("type") or "").lower()
        if ptype in ("song", "scripture"):
            # Songs are presentations (PP shows song length naturally);
            # scripture is brief; skip both for timer creation.
            continue
        total_items += 1
        dur_min = _extract_duration_min(p)
        if dur_min <= 0:
            no_duration += 1
            continue
        time_hint = _extract_time_str(p.get("notes", "")) or \
                    _extract_time_str(p.get("title", ""))
        title = (p.get("title") or "").strip()
        # Order-preserving 2-digit sequence so PP's timer panel shows them
        # in runsheet order (PP sorts alphabetically within the panel).
        seq = f"{idx:02d}"
        time_part = f"{time_hint} — " if time_hint else ""
        timer_name = (f"{_RB_TIMER_PREFIX}{seq}. {time_part}{title} "
                      f"({dur_min} min)")[:120]
        payload = {
            "name":           timer_name,
            "allows_overrun": True,
            "countdown":      {"duration": dur_min * 60},
        }
        try:
            r = req.post(f"{base}/v1/timers", json=payload, timeout=6)
            if r.ok:
                created += 1
                # idx is 1-based above; record under 0-based item index.
                timer_names[idx - 1] = timer_name
                log.info(f"Created timer: {timer_name}")
            else:
                errors.append(f"{timer_name} → HTTP {r.status_code}")
                log.warning(f"Timer create failed: {timer_name} → "
                            f"{r.status_code} {r.text[:120]}")
        except Exception as e:
            errors.append(f"{timer_name} → {type(e).__name__}")
            log.exception(f"Timer create exception for {timer_name}")
    return {
        "created":      created,
        "deleted":      deleted,
        "no_duration":  no_duration,
        "total_items":  total_items,
        "errors":       errors,
        "timer_names":  timer_names,
    }


# ── Service Mate (GeekMagic clocks) ───────────────────────────────────────────
#
# All Service Mate code (rendering, GeekMagic HTTP client, ProPresenter
# auto-track, daemon loop, route handlers) moved to the propresenterrunsheet
# package in phase 2 of the refactor. The names are re-exported here so
# tests and the rest of this file can keep using `propresenter_app.<name>`.
#
# When you need to edit Service Mate behaviour, work in:
#   propresenterrunsheet/service_mate/  — render, push, auto-track, daemon
#   propresenterrunsheet/routes/        — /api/runsheet/* and /api/clocks/*

from propresenterrunsheet.service_mate import (
    # Constants
    ROLE_ACCENT, SCREEN_CUES, SOUND_CUES, LIGHTS_CUES, ROLE_CUE_TABLES,
    SM_FILENAME, SM_FONTS, SM_H, SM_JPEG_QUALITY, SM_LOOP_INTERVAL_S,
    SM_PP_POLL_EVERY_N_TICKS, SM_TESTCARD_FILENAME, SM_ULTRA_IMAGE_THEME,
    SM_VERBOSITIES, SM_VERBOSITY_DEFAULT, SM_W,
    # File paths (tests monkeypatch these on the underlying module)
    CLOCKS_CONFIG_FILE, RUNSHEET_STATE_FILE,
    # State helpers
    _cue_for, _default_clocks_config, _ensure_item_cues, _next_visible_item,
    _read_clocks_config, _read_runsheet_state, _write_clocks_config,
    _write_runsheet_state,
    # Render helpers
    _compute_remaining_seconds, _format_mmss, _render_cue,
    _render_cue_compact, _render_cue_detailed, _render_standby,
    _render_test_card, _resolve_current, _sm_font, _text_width,
    # GeekMagic HTTP client + module-level state
    _CLOCK_THEME_SET, _probe_clock, _push_to_clock, _set_clock_brightness,
    # ProPresenter auto-track
    _clean_header_name, _maybe_advance_from_pp, _parse_pp_time,
    _pp_active_section_index, _pp_get_playlist_items,
    # Daemon
    _CLOCKS_LOOP_LAST_PUSHED, _clocks_loop, _clocks_loop_tick,
    start_clocks_loop,
)
from propresenterrunsheet.routes import register_blueprints

# Wire Service Mate routes onto the Flask app — must run after `app` is
# defined (region 2 above). Routes live in propresenterrunsheet/routes/.
register_blueprints(app)


# ── Settings ──────────────────────────────────────────────────────────────────

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


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # The HTML / CSS / JS for the UI lives in templates/index.html and
    # static/app.{css,js} so editors give us syntax highlighting and the
    # browser can cache the static assets. PyInstaller bundles these via
    # --add-data flags in build_mac.sh / build_win.bat.
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "version": VERSION, "platform": sys.platform})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    s = load_settings()
    pp_root = find_pp_root()
    s["pp_root"] = pp_root
    s["library_dirs"] = find_library_dirs(pp_root)
    s["platform"] = sys.platform
    s["version"] = VERSION
    s["data_dir"] = str(DATA_DIR)
    return jsonify(s)


@app.route("/api/settings", methods=["POST"])
def post_settings():
    body = request.get_json(silent=True) or {}
    save_settings(body)
    return jsonify({"ok": True})


@app.route("/api/prompt", methods=["GET"])
def get_prompt():
    saved = (load_settings().get("ai_prompt") or "").strip()
    return jsonify({
        "prompt":     saved or DEFAULT_PROMPT,
        "is_default": not saved,
        "default":    DEFAULT_PROMPT,
    })


@app.route("/api/prompt", methods=["POST"])
def post_prompt():
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    # Empty string is meaningful → "revert to default".
    save_settings({"ai_prompt": prompt if isinstance(prompt, str) else ""})
    return jsonify({"ok": True})


@app.route("/api/library/scan", methods=["POST"])
def api_scan_library():
    body = request.get_json(silent=True) or {}
    d = (body.get("directory") or "").strip()
    if not d:
        return jsonify({"error": "Library folder not set."}), 400
    if not Path(d).exists():
        return jsonify({"error": f"Folder not found: {d}"}), 400
    items = scan_library(d)
    log.info(f"Library scan: {len(items)} items from {d}")
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/library/fetch", methods=["POST"])
def api_fetch_library():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = f"http://{host}:{port}"
    try:
        r = req.get(f"{base}/v1/libraries", timeout=6)
        r.raise_for_status()
        libs = r.json()
        if isinstance(libs, dict):
            lib_id = next(iter(libs))
            v = libs[lib_id]
            lib_id = v.get("uuid") or v.get("name") or lib_id
        else:
            lib_id = libs[0].get("uuid") or libs[0].get("name")
        r2 = req.get(f"{base}/v1/library/{lib_id}", timeout=12)
        r2.raise_for_status()
        items = r2.json().get("items", [])
        log.info(f"Library fetch via API: {len(items)} items")
        return jsonify({"items": items, "count": len(items)})
    except req.exceptions.ConnectionError:
        return jsonify({"error":
            f"Cannot connect to ProPresenter at {host}:{port}. "
            "Make sure ProPresenter is running and Network is enabled."}), 200
    except Exception as e:
        log.exception("Library fetch failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload_and_parse", methods=["POST"])
def api_upload_and_parse():
    import requests as req

    # 1. Validate request
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF uploaded"}), 400
    pdf_file = request.files["pdf"]
    if not pdf_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # 2. Save upload to a temp path (we delete it after extraction either way)
    tmp_path = UPLOAD_FOLDER / f"runsheet_{int(time.time()*1000)}.pdf"
    pdf_file.save(str(tmp_path))

    # 3. Resolve API key + model (form values override saved settings)
    settings = load_settings()
    or_key = (request.form.get("or_key") or settings.get("or_key") or "").strip()
    model = (request.form.get("or_model")
             or settings.get("or_model")
             or _default_settings()["or_model"]).strip()

    if not or_key:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error": "OpenRouter API key required."}), 400

    try:
        # 4. Extract text from the PDF (always clean up the temp file)
        try:
            raw = extract_pdf_text(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)

        if not raw.strip():
            return jsonify({"error":
                "Could not extract text from PDF. "
                "Make sure it is a text-based PDF (not a scanned image)."}), 400

        # 5. Assemble the prompt
        # User-customised prompt if they've set one, else built-in default.
        # {RUNSHEET} is replaced with the extracted PDF text; if the user
        # removed the placeholder, we append the runsheet at the end so the
        # model still sees it.
        prompt_template = (settings.get("ai_prompt") or "").strip() \
            or DEFAULT_PROMPT
        runsheet_text = raw[:7000]
        if "{RUNSHEET}" in prompt_template:
            prompt = prompt_template.replace("{RUNSHEET}", runsheet_text)
        else:
            prompt = (f"{prompt_template}\n\nRUNSHEET:\n---\n"
                      f"{runsheet_text}\n---")

        # 5b. Service Mate cue addendum — ask the model to also emit a per-role
        # one-liner per item so the GeekMagic clocks at Screen/Sound/Lights
        # have something specific to display. If the model ignores this, the
        # rule table fills in a generic fallback.
        prompt += (
            "\n\nADDITIONAL FIELD — `cues`:\n"
            "For EACH item, also include a `cues` object with three short "
            "imperative phrases (≤ 40 chars each) telling the operator at "
            "that station what to do when this item is current:\n"
            "  - cues.screen  — what the SCREEN/lyric op should cue next\n"
            "  - cues.sound   — what the SOUND op should do (which mics on/off)\n"
            "  - cues.lights  — what the LIGHTS op should do\n"
            "Use the title, speaker names, and notes for specificity. "
            "Examples:\n"
            "  cues.screen = \"Slide — Build My Life\"\n"
            "  cues.sound  = \"Mic on for Ps Nick\"\n"
            "  cues.lights = \"Spot — preacher\"\n"
            "If you can't tell, leave the field as an empty string."
        )

        # 6. Call OpenRouter
        # Specific 4xx responses become friendly JSON errors (HTTP 200 so the
        # JS reads the message); everything else falls through to raise_for_status
        # and surfaces as a generic 500.
        log.info(f"OpenRouter request: model={model}, raw_chars={len(raw)}")
        resp = req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization":  f"Bearer {or_key}",
                "HTTP-Referer":   "propresenter-runsheet-builder",
                "X-Title":        APP_NAME,
                "Content-Type":   "application/json",
            },
            json={
                "model":       model,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=90,
        )
        if resp.status_code == 401:
            return jsonify({"error":
                "OpenRouter rejected the API key (401). "
                "Check the key in the sidebar."}), 200
        if resp.status_code == 402:
            return jsonify({"error":
                "OpenRouter says this account has no credit / model is paid (402). "
                "Try a different model — a free one is in the sidebar by default."}), 200
        if resp.status_code == 404:
            return jsonify({"error":
                f"OpenRouter says model '{model}' not found (404). "
                "Check the model id at openrouter.ai/models."}), 200
        resp.raise_for_status()

        # 7. Parse the AI response — strip markdown fences, accept either
        # {service_name, items} (preferred) or a bare items array (older models).
        content = resp.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        m_obj = re.search(r"\{.*\}", content, re.DOTALL)
        m_arr = re.search(r"\[.*\]", content, re.DOTALL)
        if m_obj:
            data = json.loads(m_obj.group())
        elif m_arr:
            data = json.loads(m_arr.group())
        else:
            data = json.loads(content)

        if isinstance(data, list):
            items, service_name = data, ""
        elif isinstance(data, dict):
            items = data.get("items", [])
            service_name = (data.get("service_name") or "").strip()
        else:
            return jsonify({"error": "AI returned unexpected JSON shape."}), 500

        # 8. If the AI didn't supply a service name, derive one from the filename
        if not service_name and pdf_file.filename:
            stem = re.sub(r"\.pdf$", "", pdf_file.filename, flags=re.IGNORECASE)
            service_name = re.sub(r"[_]+", " ", stem).strip()

        # Fill any per-role cue gaps from the rule table so every item has
        # cues for the Service Mate clocks.
        for it in items:
            if isinstance(it, dict):
                _ensure_item_cues(it)

        # Also seed the Service Mate runsheet state on parse — so the user can
        # test the clock cue flow without going through Create Playlist (which
        # requires ProPresenter to be running). Create Playlist later overwrites
        # this with the timer-name-stamped version for auto-track.
        try:
            sm_state = {
                "service_name":       service_name or pdf_file.filename or "Runsheet",
                "items":              items,
                "current_index":      0,
                "current_started_at": _dt.datetime.now().isoformat(),
                "auto_track":         {"enabled": True},
            }
            _write_runsheet_state(sm_state)
            log.info(f"Service Mate state seeded from parse: {len(items)} items")
        except Exception:
            log.exception("Service Mate parse-time state write failed")

        log.info(f"AI parsed {len(items)} runsheet items, "
                 f"suggested name: {service_name!r}")
        return jsonify({
            "items":          items,
            "filename":       pdf_file.filename,
            "suggested_name": service_name,
        })

    except json.JSONDecodeError as e:
        log.exception("AI returned invalid JSON")
        return jsonify({"error": f"AI response was not valid JSON: {e}"}), 500
    except req.exceptions.Timeout:
        return jsonify({"error":
            "OpenRouter request timed out. Try again, or pick a faster model."}), 200
    except Exception as e:
        log.exception("Parse failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/match", methods=["POST"])
def api_match():
    body = request.get_json(silent=True) or {}
    parsed = body.get("parsed", [])
    library = body.get("library", [])
    threshold = float(body.get("threshold", 0.55))
    results = []
    for item in parsed:
        if item.get("type") == "song" and library:
            match, conf = fuzzy_match(item.get("title", ""), library, threshold)
        else:
            match, conf = None, 0.0
        results.append({"parsed": item, "match": match,
                        "confidence": round(conf, 3)})
    return jsonify({"items": results})


@app.route("/api/create_playlist", methods=["POST"])
def api_create_playlist():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = f"http://{host}:{port}"
    name = (body.get("name") or "").strip()
    matched = body.get("matched") or []
    before = time.time()

    if not name:
        return jsonify({"error": "Playlist name required."}), 200
    if not matched:
        return jsonify({"error": "No items to add to the playlist."}), 200

    try:
        # 1. Create the playlist
        r = req.post(f"{base}/v1/playlists",
                     json={"name": name, "type": "playlist"}, timeout=6)
        r.raise_for_status()
        pid = r.json().get("id", {})
        if isinstance(pid, dict):
            playlist_id = pid.get("uuid") or pid.get("name") or name
        else:
            playlist_id = str(pid) or name

        # 2. Build items list
        items = []
        for mi in matched:
            p = mi.get("parsed") or {}
            m = mi.get("match")
            if p.get("type") == "song" and m:
                pres_uuid = m.get("uuid", "")
                # is_hidden / is_pco are required by the PP API. We never
                # produce hidden items and don't integrate with Planning
                # Center Online — both stay False on every item we send.
                items.append({
                    "id":          {"uuid":  pres_uuid,
                                    "name":  m.get("name", ""),
                                    "index": m.get("index", 0)},
                    "type":        "presentation",
                    "target_uuid": pres_uuid,
                    "is_hidden":   False, "is_pco": False,
                })
            elif p.get("type") == "song":
                # Unmatched song → red ACTION NEEDED placeholder so the
                # volunteer notices and can manually add the song in PP.
                label = f"⚠ ACTION NEEDED — {p.get('title', '')}"
                if p.get("notes"):
                    label += f"  ({p['notes']})"
                items.append({
                    "id":           {"uuid": "", "name": label, "index": 0},
                    "type":         "header",
                    "target_uuid":  "",
                    "is_hidden":    False, "is_pco": False,
                    "header_color": _color_dict(ACTION_NEEDED_COLOR),
                })
            else:
                title = p.get("title", "") or ""
                # Make scripture items visually distinctive in PP — operator
                # uses the green colour + book emoji to spot them, then opens
                # PP's built-in Bible feature manually for the actual verses
                # (PP's REST API has no scripture endpoints in v7.21).
                if p.get("type") == "scripture":
                    title = f"📖 {title}"
                label = title
                if p.get("notes"):
                    label += f"  —  {p['notes']}"
                items.append({
                    "id":           {"uuid": "", "name": label, "index": 0},
                    "type":         "header",
                    "target_uuid":  "",
                    "is_hidden":    False, "is_pco": False,
                    "header_color": _color_for_type(p.get("type")),
                })

        # 3. Push items to playlist
        r2 = req.put(f"{base}/v1/playlist/{playlist_id}",
                     json=items, timeout=10)
        if r2.status_code == 404:
            return jsonify({"error":
                "ProPresenter rejected one of the song UUIDs (404). "
                "Re-scan / re-fetch your library so item UUIDs are current, "
                f"then try again. Server said: {r2.text[:200]}"}), 200
        if r2.status_code == 400:
            return jsonify({"error":
                "ProPresenter rejected the playlist contents (400). "
                f"Server said: {r2.text[:300]}"}), 200
        r2.raise_for_status()

        songs = sum(1 for mi in matched
                    if (mi.get("parsed") or {}).get("type") == "song"
                    and mi.get("match"))
        needs_action = sum(1 for mi in matched
                           if (mi.get("parsed") or {}).get("type") == "song"
                           and not mi.get("match"))
        headers = sum(1 for mi in matched
                      if (mi.get("parsed") or {}).get("type") != "song")

        # 4. Try to export the .playlist file
        export_path = None
        export_dir = (body.get("export_dir") or "").strip()
        if export_dir:
            pdir = find_playlist_dir(find_pp_root())
            if pdir:
                time.sleep(1.0)
                candidates = [f for f in Path(pdir).iterdir()
                              if f.is_file() and f.stat().st_mtime > before]
                if candidates:
                    newest = max(candidates, key=lambda f: f.stat().st_mtime)
                    Path(export_dir).mkdir(parents=True, exist_ok=True)
                    dest = Path(export_dir) / f"{name}.playlist"
                    shutil.copy2(newest, dest)
                    export_path = str(dest)

        # 5. Optional: create duration-based countdown timers
        timer_result = {"created": 0, "deleted": 0, "no_duration": 0,
                        "total_items": 0, "errors": [], "timer_names": {}}
        if body.get("create_timers"):
            timer_result = _create_pp_timers(base, name, matched)

        # 6. Persist Service Mate runsheet state — what the GeekMagic clocks
        # display on the LAN. We strip the "match" wrappers and keep only the
        # parsed items, plus stamp each item with the exact PP timer name we
        # created for it (so auto-track can match by name later).
        try:
            timer_names = (timer_result or {}).get("timer_names") or {}
            sm_items = []
            for i, mi in enumerate(matched):
                p = dict((mi.get("parsed") or {}))
                if i in timer_names:
                    p["pp_timer_name"] = timer_names[i]
                _ensure_item_cues(p)
                sm_items.append(p)
            sm_state = {
                "service_name":       name,
                "items":              sm_items,
                "current_index":      0,
                "current_started_at": _dt.datetime.now().isoformat(),
                "auto_track":         {"enabled": True},
            }
            _write_runsheet_state(sm_state)
            log.info(f"Service Mate state written: {len(sm_items)} items")
        except Exception:
            log.exception("Service Mate state write failed (non-fatal)")

        log.info(f"Playlist created: '{name}' → {songs} songs, {headers} headers, "
                 f"{needs_action} action-needed, {timer_result['created']} timers "
                 f"(deleted {timer_result['deleted']} old, "
                 f"{timer_result['no_duration']} skipped no-duration), "
                 f"export={export_path}")

        return jsonify({
            "ok":                  True,
            "songs":               songs,
            "headers":             headers,
            "needs_action":        needs_action,
            "timers_created":      timer_result["created"],
            "timers_deleted":      timer_result["deleted"],
            "timers_no_duration":  timer_result["no_duration"],
            "timers_total_items":  timer_result["total_items"],
            "timer_errors":        timer_result["errors"],
            "export_path":         export_path,
        })

    except req.exceptions.ConnectionError:
        return jsonify({"error":
            f"Cannot connect to ProPresenter at {host}:{port}. "
            "Make sure ProPresenter is running and Network is enabled in "
            "Preferences → Integrations → Network."}), 200
    except Exception as e:
        log.exception("Playlist create failed")
        return jsonify({"error": str(e)}), 200


@app.route("/api/test_connection", methods=["POST"])
def api_test_connection():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = f"http://{host}:{port}"
    try:
        r = req.get(f"{base}/v1/libraries", timeout=4)
        r.raise_for_status()
        libs = r.json()
        return jsonify({"ok": True,
                        "count": len(libs) if hasattr(libs, "__len__") else 0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/quit", methods=["POST"])
def api_quit():
    log.info("Quit requested via UI")
    def _bye():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True})



# ── Server bootstrap ──────────────────────────────────────────────────────────

def _find_free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + PORT_RANGE)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in {preferred}–{preferred + PORT_RANGE - 1}")


def _cleanup_old_uploads(max_age_hours: int = 24) -> None:
    cutoff = time.time() - max_age_hours * 3600
    try:
        for f in UPLOAD_FOLDER.glob("*.pdf"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _open_browser(port: int) -> None:
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        log.exception("Could not open browser")


def _show_startup_error(title: str, message: str) -> None:
    log.error(f"{title}: {message}")
    print(f"\n!!! {title}\n{message}\n", file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def _serve(port: int) -> None:
    try:
        from waitress import serve
        log.info(f"Serving with waitress on http://127.0.0.1:{port}")
        serve(app, host="127.0.0.1", port=port, threads=8, _quiet=True)
    except ImportError:
        log.warning("waitress not installed — using Flask dev server "
                    "(install waitress for production use)")
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def main() -> None:
    try:
        log.info(f"=== {APP_NAME} v{VERSION} ===")
        log.info(f"Platform: {sys.platform}  Frozen: {getattr(sys, 'frozen', False)}")
        log.info(f"Data dir: {DATA_DIR}")
        _cleanup_old_uploads()

        port = _find_free_port(DEFAULT_PORT)
        if port != DEFAULT_PORT:
            log.warning(f"Port {DEFAULT_PORT} taken — using {port} instead")

        platform_name = "Mac" if sys.platform == "darwin" else (
                        "Windows" if sys.platform == "win32" else sys.platform)
        banner = "=" * 56
        print(banner)
        print(f"  {APP_NAME} — v{VERSION} ({platform_name})")
        print(f"  http://localhost:{port}")
        print(f"  Logs:     {LOG_FILE}")
        print(f"  Settings: {SETTINGS_FILE}")
        print("  Press Ctrl+C to quit (or use the Quit button in the UI)")
        print(banner)

        threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
        # Service Mate daemon — pushes 240×240 PNGs to GeekMagic clocks on the
        # LAN. No-op if no clock IPs are configured.
        start_clocks_loop()
        _serve(port)
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    except Exception as e:
        _show_startup_error(
            f"{APP_NAME} failed to start",
            f"{type(e).__name__}: {e}\n\nLog file:\n{LOG_FILE}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
