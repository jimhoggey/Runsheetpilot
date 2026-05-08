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
  8b. Service Mate — GeekMagic clock rendering, push, auto-track loop
  9. Settings load/save
 10. API routes (one block per /api/* endpoint, incl. /api/runsheet/*
     and /api/clocks/* for the Service Mate)
 11. Server bootstrap (port, browser, waitress, clocks loop)

Common feature touch-points:
  - new API endpoint    → region 10  + JS caller in static/app.js
  - new UI panel        → templates/index.html + JS handler in static/app.js
  - new settings field  → _default_settings() in region 9 + UI in templates
  - new runsheet type   → DEFAULT_PROMPT (region 7), TYPE_COLORS (region 7),
                          tagClass() in static/app.js, .tag-* in static/app.css,
                          *_CUES rule tables (region 8b)
  - clock layout tweak  → _render_cue() in region 8b
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


# ── Service Mate (GeekMagic SmallTV-Ultra clocks) ────────────────────────────
#
# Each "Service Mate" is a GeekMagic SmallTV-Ultra running stock firmware on the
# LAN. It has no app-level "show this text" endpoint — only image upload + a
# "display this image" toggle. So we render a 240×240 PNG with the current
# runsheet item, countdown, and a role-aware cue, then push it.
#
# Validated against adrienbrault/geekmagic-hacs (the Home Assistant integration).
# Stock firmware HTTP surface used here:
#   POST /doUpload?dir=/image/  multipart, field "file"  → save image
#   GET  /set?theme=3                                    → custom-image mode
#   GET  /set?img=/image/<filename>                      → display
#   GET  /set?brt=<1-100>                                → brightness
#   GET  /app.json                                       → health
#
# Quirk: the Ultra firmware returns malformed HTTP on POST (duplicate
# Content-Length header). `requests` will raise ChunkedEncodingError /
# ProtocolError even though the upload succeeded — _push_to_clock catches it.

RUNSHEET_STATE_FILE = DATA_DIR / "runsheet_state.json"
CLOCKS_CONFIG_FILE  = DATA_DIR / "clocks.json"

# Display constants — Ultra is 240×240 RGB.
# Firmware (v9.0.39 confirmed) only renders JPG/GIF in Photo Album mode — PNG
# uploads succeed but the device won't display them. So we encode as JPEG.
SM_W, SM_H = 240, 240
SM_FILENAME = "rb_cue.jpg"           # uploaded under /image/ on the device
SM_TESTCARD_FILENAME = "rb_test.jpg"
SM_JPEG_QUALITY = 90
SM_ULTRA_IMAGE_THEME = 3   # Theme 3 = "Photo Album" (custom image full-screen)

# Daemon loop cadence — render every TICK; only POLL ProPresenter every Nth
# tick. 500 ms render lets the on-screen countdown step every 1 s instead of
# every 2 s; PP polling stays at 2 s so we don't hammer ProPresenter's API.
SM_LOOP_INTERVAL_S = 0.5
SM_PP_POLL_EVERY_N_TICKS = 4

# Per-verbosity font sizes — tweak here, layouts in _render_cue_compact /
# _render_cue_detailed pick from these.
SM_FONTS = {
    "compact": {
        "label":   14,   # top role/type strip
        "title":   22,   # current item title
        "clock":   56,   # countdown
        "next":    13,   # "NEXT — TYPE" label
        "cue":     15,   # bottom cue band
    },
    "detailed": {
        "label":   12,
        "title":   18,
        "notes":   12,
        "clock":   42,
        "next":    12,
        "next_t":  14,   # next-item title (rendered, unlike compact)
        "then":    12,   # "then: <next-cue>" hint line
        "cue":     14,
    },
}
SM_VERBOSITY_DEFAULT = "compact"
SM_VERBOSITIES = ("compact", "detailed")

# Role accent colours used in the rendered cue images. Hex tuples (RGB).
ROLE_ACCENT = {
    "screen": (59, 130, 246),   # blue
    "sound":  (34, 197, 94),    # green
    "lights": (245, 158, 11),   # amber
}

# Fallback rule table for per-role cue text when the LLM doesn't supply one.
# Key = item type (matches the runsheet "type" field). Value = short imperative.
SCREEN_CUES = {
    "song":         "Cue song slides",
    "mc_on_stage":  "MC slide / lower-thirds",
    "sermon":       "Sermon slides",
    "scripture":    "Scripture slides",
    "announcement": "Announcement loop",
    "prayer":       "Prayer slide",
    "offering":     "Offering slide",
    "video":        "Video — full screen",
    "other":        "Stand by",
}
SOUND_CUES = {
    "song":         "Band mics live · MC mute",
    "mc_on_stage":  "MC mic ON · band mute",
    "sermon":       "Speaker mic ON",
    "scripture":    "Reader mic ON",
    "announcement": "MC mic ON",
    "prayer":       "Prayer mic ON",
    "offering":     "MC mic ON",
    "video":        "Video audio ON",
    "other":        "Stand by",
}
LIGHTS_CUES = {
    "song":         "Stage wash — band",
    "mc_on_stage":  "Spot — MC",
    "sermon":       "Spot — preacher",
    "scripture":    "Soft warm wash",
    "announcement": "House lights up",
    "prayer":       "Soft warm wash",
    "offering":     "House lights up",
    "video":        "Stage dim · screen up",
    "other":        "Stand by",
}
ROLE_CUE_TABLES = {
    "screen": SCREEN_CUES, "sound": SOUND_CUES, "lights": LIGHTS_CUES,
}


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


def _format_mmss(seconds) -> str:
    if seconds is None:
        return "--:--"
    sign = "-" if seconds < 0 else ""
    s = abs(int(seconds))
    return f"{sign}{s // 60:02d}:{s % 60:02d}"


def _compute_remaining_seconds(state: dict):
    """Best-effort countdown: prefer pp_timer remaining if known, else compute
    from current_started_at + duration_min."""
    pp_remaining = state.get("pp_remaining_seconds")
    if isinstance(pp_remaining, (int, float)):
        return int(pp_remaining)
    items = state.get("items") or []
    idx = int(state.get("current_index") or 0)
    if not (0 <= idx < len(items)):
        return None
    item = items[idx]
    dur_min = _extract_duration_min(item)
    if dur_min <= 0:
        return None
    started_at = state.get("current_started_at")
    if not started_at:
        return None
    try:
        started = _dt.datetime.fromisoformat(started_at)
    except Exception:
        return None
    elapsed = (_dt.datetime.now() - started).total_seconds()
    return int(dur_min * 60 - elapsed)


def _sm_font(size: int):
    """Pick the first available proportional font on this OS, else default."""
    from PIL import ImageFont
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_role_strip(draw, role: str, accent: tuple, type_label: str,
                     height: int, font):
    """Top role-coloured strip: ROLE on the left, item TYPE on the right."""
    draw.rectangle([(0, 0), (SM_W, height)], fill=accent)
    text_y = max(0, (height - font.size) // 2 - 1)
    draw.text((8, text_y), role.upper(), fill=(255, 255, 255), font=font)
    tw = draw.textlength(type_label, font=font)
    draw.text((SM_W - 8 - tw, text_y), type_label, fill=(255, 255, 255), font=font)


def _draw_cue_band(draw, accent: tuple, cue_text: str, font):
    """Bottom accent-tinted band with the cue line for this role."""
    if not cue_text:
        return
    band_color = tuple(min(255, int(c * 0.35)) for c in accent)
    band_h = font.size + 11
    draw.rectangle([(0, SM_H - band_h), (SM_W, SM_H)], fill=band_color)
    ct = cue_text
    while draw.textlength(ct, font=font) > SM_W - 16 and len(ct) > 4:
        ct = ct[:-2]
    if ct != cue_text:
        ct = ct[:-1] + "…"
    draw.text((8, SM_H - band_h + 5), ct, fill=(255, 255, 255), font=font)


def _new_canvas():
    from PIL import Image
    return Image.new("RGB", (SM_W, SM_H), (16, 16, 28))


def _save_jpeg(img) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=SM_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _resolve_current(state: dict):
    """Return (cur_item, next_item, remaining_seconds) for the active state."""
    items = state.get("items") or []
    idx = int(state.get("current_index") or 0)
    if not items:
        return None, None, None
    idx = max(0, min(idx, len(items) - 1))
    return items[idx], _next_visible_item(items, idx), \
        _compute_remaining_seconds(state)


def _render_cue(role: str, state: dict, verbosity: str = SM_VERBOSITY_DEFAULT) -> bytes:
    """Render a 240×240 JPEG for a given role + verbosity. JPEG (not PNG)
    because the v9.0.39 firmware only displays JPG/GIF in Photo Album mode.
    Verbosity dispatches to the compact (less words, large fonts) or detailed
    (more words, smaller fonts) layout."""
    if verbosity not in SM_VERBOSITIES:
        verbosity = SM_VERBOSITY_DEFAULT
    if verbosity == "detailed":
        return _render_cue_detailed(role, state)
    return _render_cue_compact(role, state)


def _render_cue_compact(role: str, state: dict) -> bytes:
    """Glance-from-across-the-room layout. Big title, huge countdown, just
    the next item TYPE (no full title), single cue line at the bottom."""
    from PIL import ImageDraw
    f = SM_FONTS["compact"]
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    cur, nxt, remaining = _resolve_current(state)

    img = _new_canvas()
    draw = ImageDraw.Draw(img)

    f_label = _sm_font(f["label"])
    f_title = _sm_font(f["title"])
    f_clock = _sm_font(f["clock"])
    f_next  = _sm_font(f["next"])
    f_cue   = _sm_font(f["cue"])

    type_label = (cur.get("type") if cur else "").upper().replace("_", " ") or "—"
    _draw_role_strip(draw, role, accent, type_label, height=28, font=f_label)

    cur_title = (cur.get("title") if cur else "(no runsheet)") or "(empty)"
    _draw_wrapped(draw, cur_title, (12, 36), SM_W - 24, f_title,
                  (236, 236, 243), max_lines=2)

    mmss = _format_mmss(remaining) if remaining is not None else "--:--"
    tw = draw.textlength(mmss, font=f_clock)
    cd_color = (239, 68, 68) if (remaining is not None and remaining < 30) \
        else (255, 255, 255)
    draw.text(((SM_W - tw) / 2, 92), mmss, fill=cd_color, font=f_clock)

    draw.line([(12, 168), (SM_W - 12, 168)], fill=(60, 60, 80), width=1)

    if nxt:
        nxt_type = (nxt.get("type") or "").upper().replace("_", " ")
        draw.text((12, 174), f"NEXT — {nxt_type}", fill=(140, 140, 170),
                  font=f_next)
        nxt_title = nxt.get("title") or ""
        _draw_wrapped(draw, nxt_title, (12, 190), SM_W - 24, f_next,
                      (220, 220, 235), max_lines=1)
    else:
        draw.text((12, 174), "END OF SERVICE", fill=(140, 140, 170), font=f_next)

    _draw_cue_band(draw, accent, _cue_for(role, cur) if cur else "", f_cue)
    return _save_jpeg(img)


def _render_cue_detailed(role: str, state: dict) -> bytes:
    """Standing-right-next-to-the-screen layout. Smaller fonts, includes notes,
    full next-item title, and a 'then:' hint with the next role cue."""
    from PIL import ImageDraw
    f = SM_FONTS["detailed"]
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    cur, nxt, remaining = _resolve_current(state)

    img = _new_canvas()
    draw = ImageDraw.Draw(img)

    f_label = _sm_font(f["label"])
    f_title = _sm_font(f["title"])
    f_notes = _sm_font(f["notes"])
    f_clock = _sm_font(f["clock"])
    f_next  = _sm_font(f["next"])
    f_next_t = _sm_font(f["next_t"])
    f_then  = _sm_font(f["then"])
    f_cue   = _sm_font(f["cue"])

    type_label = (cur.get("type") if cur else "").upper().replace("_", " ") or "—"
    _draw_role_strip(draw, role, accent, type_label, height=22, font=f_label)

    # Title + notes — leave 12 px each side so glyphs don't bleed past the
    # canvas edge (PIL's textlength under-reports the right side bearing for
    # the last character, which can let a string render wider than the value
    # we measured).
    cur_title = (cur.get("title") if cur else "(no runsheet)") or "(empty)"
    _draw_wrapped(draw, cur_title, (12, 28), SM_W - 24, f_title,
                  (236, 236, 243), max_lines=2)
    notes = (cur.get("notes") if cur else "") or ""
    if notes:
        _draw_wrapped(draw, notes, (12, 28 + (f["title"] + 4) * 2 + 2),
                      SM_W - 24, f_notes, (140, 140, 170), max_lines=1)

    # Countdown — slightly smaller, centred
    mmss = _format_mmss(remaining) if remaining is not None else "--:--"
    tw = draw.textlength(mmss, font=f_clock)
    cd_color = (239, 68, 68) if (remaining is not None and remaining < 30) \
        else (255, 255, 255)
    draw.text(((SM_W - tw) / 2, 96), mmss, fill=cd_color, font=f_clock)

    # NEXT — full title + a 'then:' hint pulled from the next item's role cue
    next_y = 148
    if nxt:
        nxt_type = (nxt.get("type") or "").upper().replace("_", " ")
        draw.text((12, next_y), f"NEXT — {nxt_type}", fill=(140, 140, 170),
                  font=f_next)
        _draw_wrapped(draw, nxt.get("title") or "",
                      (12, next_y + f["next"] + 3), SM_W - 24, f_next_t,
                      (220, 220, 235), max_lines=1)
        then_cue = _cue_for(role, nxt)
        if then_cue:
            then_y = next_y + f["next"] + 3 + f["next_t"] + 4
            then_text = f"then: {then_cue}"
            tt = then_text
            while draw.textlength(tt, font=f_then) > SM_W - 24 and len(tt) > 8:
                tt = tt[:-2]
            if tt != then_text:
                tt = tt[:-1] + "…"
            draw.text((12, then_y), tt, fill=(120, 120, 150), font=f_then)
    else:
        draw.text((12, next_y), "END OF SERVICE", fill=(140, 140, 170), font=f_next)

    _draw_cue_band(draw, accent, _cue_for(role, cur) if cur else "", f_cue)
    return _save_jpeg(img)


def _text_width(font, text: str) -> int:
    """Truer width than draw.textlength — uses the glyph bounding box so the
    rightmost character's side-bearing is counted. Without this, long titles
    can bleed past the canvas edge because Pillow's textlength only reports
    the advance width."""
    if not text:
        return 0
    try:
        l, _, r, _ = font.getbbox(text)
        return max(int(r - l), 0)
    except Exception:
        # ImageFont.load_default() doesn't have getbbox in older Pillow
        return int(font.getlength(text)) if hasattr(font, "getlength") else 0


def _draw_wrapped(draw, text, xy, max_w, font, fill, max_lines: int = 2):
    """Greedy word-wrap to fit max_w; ellipsizes only when words got dropped.
    Uses _text_width (bbox-based) so bearings are accounted for."""
    if not text:
        return
    words = str(text).split()
    lines: list = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if _text_width(font, test) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            if len(lines) >= max_lines:
                break
            cur = w
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # Only ellipsize when we actually dropped words. Otherwise the previous
    # bug fired: every wrapped block was speculatively trimmed to fit
    # last + "…", chopping legitimate trailing characters off perfectly-fitting
    # text.
    rendered_word_count = sum(len(line.split()) for line in lines)
    if lines and rendered_word_count < len(words):
        last = lines[-1]
        while _text_width(font, last + "…") > max_w and len(last) > 2:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"

    y = xy[1]
    line_h = font.size + 4
    for line in lines:
        draw.text((xy[0], y), line, fill=fill, font=font)
        y += line_h


def _render_test_card(role: str, ip: str = "") -> bytes:
    """A simple coloured card with the role label — used by the Test button."""
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    img = Image.new("RGB", (SM_W, SM_H), accent)
    draw = ImageDraw.Draw(img)
    try:
        f_big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
        f_sm  = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except OSError:
        f_big = ImageFont.load_default()
        f_sm  = ImageFont.load_default()
    label = role.upper()
    tw = draw.textlength(label, font=f_big)
    draw.text(((SM_W - tw) / 2, 60), label, fill=(255, 255, 255), font=f_big)
    sub = "Service Mate test"
    tw2 = draw.textlength(sub, font=f_sm)
    draw.text(((SM_W - tw2) / 2, 130), sub, fill=(240, 240, 255), font=f_sm)
    if ip:
        tw3 = draw.textlength(ip, font=f_sm)
        draw.text(((SM_W - tw3) / 2, 156), ip, fill=(220, 220, 235), font=f_sm)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=SM_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _render_standby(role: str) -> bytes:
    """Pre-service waiting page — shown when the operator has reset the clocks
    or when no runsheet is loaded. Same role-coloured strip as the live cue
    so the device looks visually consistent. Shows "STANDBY", a friendly
    sub-line, and the current wall-clock time so anyone glancing at the screen
    knows the device is alive and waiting."""
    from PIL import ImageDraw
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    img = _new_canvas()
    draw = ImageDraw.Draw(img)

    f_label = _sm_font(14)
    f_big   = _sm_font(40)
    f_sub   = _sm_font(14)
    f_clock = _sm_font(28)

    _draw_role_strip(draw, role, accent, "STANDBY", height=28, font=f_label)

    big = "STANDBY"
    tw = _text_width(f_big, big)
    draw.text(((SM_W - tw) / 2, 70), big, fill=(236, 236, 243), font=f_big)

    sub = "Awaiting service start"
    tw2 = _text_width(f_sub, sub)
    draw.text(((SM_W - tw2) / 2, 122), sub, fill=(140, 140, 170), font=f_sub)

    now = _dt.datetime.now().strftime("%H:%M")
    tw3 = _text_width(f_clock, now)
    draw.text(((SM_W - tw3) / 2, 158), now, fill=(200, 200, 220), font=f_clock)

    band_color = tuple(min(255, int(c * 0.35)) for c in accent)
    band_h = f_sub.size + 11
    draw.rectangle([(0, SM_H - band_h), (SM_W, SM_H)], fill=band_color)
    return _save_jpeg(img)


# Track which IPs we've already set theme=3 on this process — saves an HTTP
# call per push. Cleared on restart.
_CLOCK_THEME_SET: set = set()


def _push_to_clock(ip: str, image_bytes: bytes,
                   filename: str = SM_FILENAME) -> bool:
    """Upload an image to a GeekMagic Ultra and switch its display to it.
    Returns True on success. Treats the firmware's malformed-HTTP-after-POST
    quirk as success (HACS does the same).

    `image_bytes` should be JPEG or GIF — v9.0.39 firmware does not display
    PNG in Photo Album mode (uploads succeed silently but never render)."""
    import requests as req
    if not ip:
        return False
    base = f"http://{ip}"
    fl = (filename or SM_FILENAME).lower()
    if fl.endswith(".gif"):
        ctype = "image/gif"
    elif fl.endswith(".png"):
        ctype = "image/png"
    else:
        ctype = "image/jpeg"
    files = {"file": (filename, image_bytes, ctype)}
    try:
        try:
            r = req.post(f"{base}/doUpload", params={"dir": "/image/"},
                         files=files, timeout=8)
            if not r.ok:
                log.warning(f"Clock {ip} upload returned {r.status_code}")
        except (req.exceptions.ChunkedEncodingError,
                req.exceptions.InvalidHeader,
                req.exceptions.ContentDecodingError,
                req.exceptions.ConnectionError) as e:
            # Ultra firmware (v9.0.39) sends malformed HTTP on POST —
            # specifically, it returns a response with two unmatching
            # Content-Length headers (e.g. "3888, 11"). urllib3 / requests
            # raise InvalidHeader (newer) or ChunkedEncodingError (older)
            # even though the upload itself succeeded. Verified by checking
            # that GET /image/<filename> returns 200 after such errors.
            log.debug(f"Clock {ip} POST raised {type(e).__name__} (ignored — "
                      "Ultra firmware quirk)")
        # Switch to custom-image mode (only first time per process)
        if ip not in _CLOCK_THEME_SET:
            try:
                r2 = req.get(f"{base}/set", params={"theme": SM_ULTRA_IMAGE_THEME},
                             timeout=4)
                r2.raise_for_status()
                _CLOCK_THEME_SET.add(ip)
            except Exception:
                log.exception(f"Clock {ip} theme set failed")
                return False
        # Display the image
        r3 = req.get(f"{base}/set", params={"img": f"/image/{filename}"}, timeout=4)
        r3.raise_for_status()
        return True
    except Exception:
        log.exception(f"Clock {ip} push failed")
        return False


def _set_clock_brightness(ip: str, brt: int) -> bool:
    import requests as req
    try:
        r = req.get(f"http://{ip}/set", params={"brt": int(brt)}, timeout=4)
        r.raise_for_status()
        return True
    except Exception:
        log.exception(f"Clock {ip} brightness failed")
        return False


def _probe_clock(ip: str) -> dict:
    import requests as req
    try:
        r = req.get(f"http://{ip}/app.json", timeout=4)
        r.raise_for_status()
        # Some firmware returns text/plain — accept any.
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:200]}
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Cached playlist contents — refreshed when the active playlist UUID changes
# or when the cache is older than this many seconds.
_PP_PLAYLIST_CACHE = {"uuid": None, "items": [], "fetched_at": 0.0}
_PP_PLAYLIST_CACHE_TTL_S = 60

# Strip header decorations the create-playlist code adds, so we can match the
# header name back to the original runsheet item title.
_HDR_ACTION_RE   = re.compile(r"^\s*⚠\s*ACTION NEEDED\s*—\s*", re.IGNORECASE)
_HDR_TIME_TAIL_RE = re.compile(
    r"\s+—\s+\d{1,2}:\d{2}\s*[AaPp][Mm]\s*$"
)
_HDR_PAREN_TIME_RE = re.compile(
    r"\s*\(\s*\d{1,2}:\d{2}\s*[AaPp][Mm]\s*\)\s*$"
)
_HDR_BOOK_RE = re.compile(r"^📖\s*")


def _clean_header_name(name: str) -> str:
    """Reverse the decorations create_playlist adds to a header so the result
    matches the original runsheet title."""
    if not name:
        return ""
    s = name
    s = _HDR_ACTION_RE.sub("", s)
    s = _HDR_BOOK_RE.sub("", s)
    s = _HDR_TIME_TAIL_RE.sub("", s)
    s = _HDR_PAREN_TIME_RE.sub("", s)
    return s.strip()


def _pp_get_playlist_items(base: str, playlist_uuid: str) -> list:
    """Cached fetch of /v1/playlist/{uuid} items. Refreshes when the active
    playlist UUID changes or every _PP_PLAYLIST_CACHE_TTL_S seconds."""
    import requests as req
    now = time.time()
    cache = _PP_PLAYLIST_CACHE
    if (cache["uuid"] == playlist_uuid
            and (now - cache["fetched_at"]) < _PP_PLAYLIST_CACHE_TTL_S
            and cache["items"]):
        return cache["items"]
    try:
        r = req.get(f"{base}/v1/playlist/{playlist_uuid}", timeout=3)
        if r.ok:
            data = r.json() or {}
            cache["uuid"] = playlist_uuid
            cache["items"] = data.get("items") or []
            cache["fetched_at"] = now
            return cache["items"]
    except Exception:
        log.debug("playlist fetch failed", exc_info=True)
    return cache["items"] if cache["uuid"] == playlist_uuid else []


def _pp_active_section_index(state: dict, base: str):
    """Map the currently-active PP playlist item back to a runsheet item index.

    Operators commonly insert media items between our header rows; clicking
    one of those should still advance the Service Mate to the section that
    media belongs to. So we walk backward through the playlist from the
    active item to find the most recent header, strip its decorations, and
    fuzzy-match against the runsheet titles.

    Returns a runsheet index, or None if no confident match."""
    import requests as req
    try:
        r = req.get(f"{base}/v1/playlist/active", timeout=2)
        if not r.ok:
            return None
        data = r.json() or {}
        pres = data.get("presentation")
        if not isinstance(pres, dict):
            return None
        playlist = pres.get("playlist") or {}
        playlist_item = pres.get("playlist_item") or {}
        playlist_uuid = playlist.get("uuid")
        active_index = (playlist_item.get("id") or {}).get("index")
        if not playlist_uuid or active_index is None:
            return None
        plist = _pp_get_playlist_items(base, playlist_uuid)
        if active_index >= len(plist):
            # Cache might be stale — force refresh and retry once.
            _PP_PLAYLIST_CACHE["uuid"] = None
            plist = _pp_get_playlist_items(base, playlist_uuid)
            if active_index >= len(plist):
                return None
        # Find the section header for the active item: itself if it's a
        # header, else walk backward.
        header_name = ""
        if plist[active_index].get("type") == "header":
            header_name = (plist[active_index].get("id") or {}).get("name", "")
        else:
            for i in range(active_index - 1, -1, -1):
                if plist[i].get("type") == "header":
                    header_name = (plist[i].get("id") or {}).get("name", "")
                    break
        clean = _clean_header_name(header_name)
        if not clean:
            return None
        items = state.get("items") or []
        best_i, best_score = -1, 0.0
        nn = _norm(clean)
        for i, it in enumerate(items):
            score = difflib.SequenceMatcher(
                None, nn, _norm(it.get("title", ""))
            ).ratio()
            if score > best_score:
                best_score, best_i = score, i
        if best_i >= 0 and best_score >= 0.6:
            return best_i
    except Exception:
        log.debug("PP /v1/playlist/active fetch failed", exc_info=True)
    return None


def _maybe_advance_from_pp(state: dict) -> dict:
    """Auto-track ProPresenter so the Service Mate follows whatever the
    operator is doing. Three sources, used in order:
      1. Active playlist section — primary. Reads /v1/playlist/active and
         walks back to the parent header. Detects clicks on media inserted
         between our headers.
      2. Running [RB] timer — secondary. Provides accurate countdown time
         when a timer is actually running.
      3. Active presentation name match — fallback for non-playlist usage.
    Manual cue clicks set a 10-second override window during which all three
    are suppressed so the operator's input isn't fought."""
    import requests as req
    auto = (state.get("auto_track") or {})
    if not auto.get("enabled", True):
        return state
    until = state.get("manual_override_until")
    if until:
        try:
            if _dt.datetime.fromisoformat(until) > _dt.datetime.now():
                return state
        except Exception:
            state.pop("manual_override_until", None)
    settings = load_settings()
    host = settings.get("pp_host") or "localhost"
    port = settings.get("pp_port") or "50001"
    base = f"http://{host}:{port}"
    items = state.get("items") or []
    if not items:
        return state

    # 1) Active playlist section (primary signal)
    section_idx = _pp_active_section_index(state, base)
    if section_idx is not None and section_idx != state.get("current_index"):
        state["current_index"] = section_idx
        state["current_started_at"] = _dt.datetime.now().isoformat()
        state["pp_source"] = "section"

    # 2) Running [RB] timer — overlays accurate remaining time on top of (1).
    timer_running = False
    try:
        r = req.get(f"{base}/v1/timers/current", timeout=2)
        if r.ok:
            running = r.json() or []
            for t in running if isinstance(running, list) else []:
                tname = ((t.get("id") or {}).get("name") or "")
                tstate = (t.get("state") or "").lower()
                if not tname.startswith(_RB_TIMER_PREFIX):
                    continue
                if tstate not in ("running", "started", "active"):
                    continue
                for i, it in enumerate(items):
                    if (it.get("pp_timer_name") or "") == tname:
                        if state.get("current_index") != i:
                            state["current_index"] = i
                            state["current_started_at"] = _dt.datetime.now().isoformat()
                            state["pp_source"] = "timer"
                        rem = _parse_pp_time(t.get("time"))
                        if rem is not None:
                            state["pp_remaining_seconds"] = rem
                        timer_running = True
                        break
                if timer_running:
                    break
            if not timer_running:
                state.pop("pp_remaining_seconds", None)
    except Exception:
        log.debug("PP /v1/timers/current poll failed", exc_info=True)

    if section_idx is not None or timer_running:
        return state

    # 3) Active presentation name match (fallback for non-playlist mode)
    try:
        r = req.get(f"{base}/v1/presentation/active", timeout=2)
        if r.ok:
            data = r.json() or {}
            pres = (data.get("presentation") or data) if isinstance(data, dict) else {}
            active_name = ""
            if isinstance(pres, dict):
                active_name = ((pres.get("id") or {}).get("name")
                               or pres.get("name") or "")
            if active_name:
                cur_idx = int(state.get("current_index") or 0)
                best_i, best_score = -1, 0.0
                for i, it in enumerate(items):
                    if i < cur_idx:
                        continue
                    score = difflib.SequenceMatcher(
                        None, _norm(active_name), _norm(it.get("title", ""))
                    ).ratio()
                    if score > best_score:
                        best_score, best_i = score, i
                if best_i >= 0 and best_score >= 0.78 and best_i != cur_idx:
                    state["current_index"] = best_i
                    state["current_started_at"] = _dt.datetime.now().isoformat()
                    state["pp_source"] = "presentation"
                    state.pop("pp_remaining_seconds", None)
    except Exception:
        log.debug("PP /v1/presentation/active poll failed", exc_info=True)

    return state


def _parse_pp_time(s):
    """ProPresenter's timer endpoint returns time as 'HH:MM:SS' string.
    Returns total seconds, or None."""
    if isinstance(s, (int, float)):
        return int(s)
    if not isinstance(s, str):
        return None
    parts = s.strip().split(":")
    try:
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 3600 + m * 60 + sec
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1:
            return int(parts[0])
    except Exception:
        pass
    return None


# Module-level so other endpoints (e.g. /api/clocks/<id>/test) can invalidate
# a clock's "last pushed" entry, forcing the loop to re-push on the next tick.
_CLOCKS_LOOP_LAST_PUSHED: dict = {}


def _clocks_loop_tick(tick: int) -> None:
    """One pass of the background loop. `tick` increments each call; we use it
    to throttle ProPresenter polling so the loop can render at 500 ms while PP
    only gets hit every SM_PP_POLL_EVERY_N_TICKS ticks."""
    state = _read_runsheet_state() or {}
    cfg = _read_clocks_config()
    if not cfg.get("enabled") or not cfg.get("clocks"):
        return
    # Standby = explicit operator reset, OR no runsheet has ever been loaded.
    # In both cases we want the clocks showing a clean waiting page rather
    # than a stale cue or going dark.
    standby = bool(state.get("standby")) or not state.get("items")
    if not standby and tick % SM_PP_POLL_EVERY_N_TICKS == 0:
        state = _maybe_advance_from_pp(state)
        try:
            _write_runsheet_state(state)
        except Exception:
            log.exception("Failed to persist runsheet state mid-loop")
    for clock in cfg["clocks"]:
        ip = (clock.get("ip") or "").strip()
        role = clock.get("role") or clock.get("id") or "screen"
        cid = clock.get("id") or role
        verbosity = (clock.get("verbosity") or SM_VERBOSITY_DEFAULT).lower()
        if verbosity not in SM_VERBOSITIES:
            verbosity = SM_VERBOSITY_DEFAULT
        if not ip:
            continue
        try:
            jpg = (_render_standby(role) if standby
                   else _render_cue(role, state, verbosity=verbosity))
        except Exception:
            log.exception(f"render failed for role={role}")
            continue
        import hashlib
        h = hashlib.sha1(jpg).hexdigest()
        # Re-push every ~40 s even if unchanged, so the device recovers if it
        # was rebooted or the image was cleared.
        prev = _CLOCKS_LOOP_LAST_PUSHED.get(cid) or ("", 0.0)
        if prev[0] == h and (time.time() - prev[1]) < 40:
            continue
        if _push_to_clock(ip, jpg):
            _CLOCKS_LOOP_LAST_PUSHED[cid] = (h, time.time())


def _clocks_loop() -> None:
    log.info(f"Service Mate loop started "
             f"(tick={SM_LOOP_INTERVAL_S}s, pp-poll every {SM_PP_POLL_EVERY_N_TICKS} ticks)")
    tick = 0
    while True:
        try:
            _clocks_loop_tick(tick)
        except Exception:
            log.exception("clocks_loop tick failed")
        tick = (tick + 1) % 1_000_000
        time.sleep(SM_LOOP_INTERVAL_S)


def start_clocks_loop() -> None:
    """Start the background daemon thread that pushes images to the Service
    Mates. Idempotent — only starts once per process."""
    if getattr(start_clocks_loop, "_started", False):
        return
    start_clocks_loop._started = True   # type: ignore[attr-defined]
    threading.Thread(target=_clocks_loop, daemon=True,
                     name="service-mate").start()


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


# ── Service Mate routes ──────────────────────────────────────────────────────
#
# All endpoints under /api/runsheet/* and /api/clocks/* are for the GeekMagic
# clock integration (Service Mate). They never call OpenRouter — they only
# move the cue index, persist state, render PNGs, and push to clocks.

@app.route("/api/runsheet/state", methods=["GET"])
def api_runsheet_state_get():
    state = _read_runsheet_state()
    return jsonify(state or {})


@app.route("/api/runsheet/state", methods=["POST"])
def api_runsheet_state_post():
    """Replace the runsheet state on disk. Body shape:
       { service_name, items, current_index?, auto_track?: {enabled} }
       items is a list of dicts with type/title/notes/duration_min and
       optional `cues: {screen,sound,lights}` and `pp_timer_name`."""
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    # Fill any missing per-role cues from the rule table.
    for it in items:
        if isinstance(it, dict):
            _ensure_item_cues(it)
    state = {
        "service_name":      (body.get("service_name") or "").strip(),
        "items":             items,
        "current_index":     int(body.get("current_index") or 0),
        "current_started_at": _dt.datetime.now().isoformat(),
        "auto_track":        body.get("auto_track")
                             or {"enabled": True},
    }
    _write_runsheet_state(state)
    return jsonify({"ok": True, "state": state})


@app.route("/api/runsheet/cue", methods=["POST"])
def api_runsheet_cue():
    """Move the current-item index. Body: {"delta": +1|-1} or {"index": N}.
    Sets a 10-second manual override window during which auto-track is paused
    so the operator's click isn't immediately overridden."""
    body = request.get_json(silent=True) or {}
    state = _read_runsheet_state()
    if not state or not state.get("items"):
        return jsonify({"error": "no runsheet loaded"}), 400
    items = state["items"]
    cur = int(state.get("current_index") or 0)
    if "index" in body and body.get("index") is not None:
        new = int(body["index"])
    elif "delta" in body:
        new = cur + int(body["delta"])
    else:
        return jsonify({"error": "delta or index required"}), 400
    new = max(0, min(new, len(items) - 1))
    state["current_index"] = new
    state["current_started_at"] = _dt.datetime.now().isoformat()
    state["manual_override_until"] = (
        _dt.datetime.now() + _dt.timedelta(seconds=10)).isoformat()
    state.pop("pp_remaining_seconds", None)
    _write_runsheet_state(state)
    return jsonify({"ok": True, "current_index": new})


@app.route("/api/runsheet/state", methods=["DELETE"])
def api_runsheet_state_delete():
    try:
        if RUNSHEET_STATE_FILE.exists():
            RUNSHEET_STATE_FILE.unlink()
    except Exception:
        log.exception("Failed to delete runsheet_state.json")
    return jsonify({"ok": True})


@app.route("/api/clocks", methods=["GET"])
def api_clocks_get():
    return jsonify(_read_clocks_config())


@app.route("/api/clocks", methods=["POST"])
def api_clocks_post():
    body = request.get_json(silent=True) or {}
    cfg = _read_clocks_config()
    if "clocks" in body and isinstance(body["clocks"], list):
        cleaned = []
        for c in body["clocks"]:
            if not isinstance(c, dict):
                continue
            verbosity = (c.get("verbosity") or SM_VERBOSITY_DEFAULT).strip().lower()
            if verbosity not in SM_VERBOSITIES:
                verbosity = SM_VERBOSITY_DEFAULT
            cleaned.append({
                "id":        (c.get("id") or c.get("role") or "").strip().lower(),
                "ip":        (c.get("ip") or "").strip(),
                "role":      (c.get("role") or c.get("id") or "screen").strip().lower(),
                "name":      (c.get("name") or "").strip(),
                "verbosity": verbosity,
            })
        cfg["clocks"] = cleaned
    if "brightness" in body:
        try:
            cfg["brightness"] = max(1, min(100, int(body["brightness"])))
        except Exception:
            pass
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    _write_clocks_config(cfg)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/clocks/<clock_id>/probe", methods=["POST"])
def api_clock_probe(clock_id: str):
    cfg = _read_clocks_config()
    clock = next((c for c in cfg.get("clocks", [])
                  if c.get("id") == clock_id), None)
    if not clock:
        return jsonify({"error": "unknown clock"}), 404
    ip = (clock.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "no IP set"}), 200
    return jsonify(_probe_clock(ip))


@app.route("/api/clocks/<clock_id>/test", methods=["POST"])
def api_clock_test(clock_id: str):
    cfg = _read_clocks_config()
    clock = next((c for c in cfg.get("clocks", [])
                  if c.get("id") == clock_id), None)
    if not clock:
        return jsonify({"error": "unknown clock"}), 404
    ip = (clock.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "no IP set"}), 200
    role = clock.get("role") or "screen"
    jpg = _render_test_card(role, ip)
    if cfg.get("brightness"):
        _set_clock_brightness(ip, int(cfg["brightness"]))
    ok = _push_to_clock(ip, jpg, filename=SM_TESTCARD_FILENAME)
    # Test card and live cue are different files on the device — clearing the
    # last-pushed hash for this clock means the next loop tick re-pushes the
    # cue image, returning the device to the live view within ~1 s. Otherwise
    # the test card would stay until the cue content next changed.
    _CLOCKS_LOOP_LAST_PUSHED.pop(clock_id, None)
    return jsonify({"ok": ok})


@app.route("/api/clocks/standby", methods=["POST"])
def api_clocks_standby():
    """Reset all clocks to the pre-service waiting page. Persists `standby:true`
    in the runsheet state so the daemon keeps pushing the standby image; the
    flag is cleared automatically on the next runsheet load (parse / create
    playlist / explicit POST /api/runsheet/state with items)."""
    state = {
        "standby": True,
        "items": [],
        "current_index": 0,
        "current_started_at": _dt.datetime.now().isoformat(),
    }
    _write_runsheet_state(state)
    # Force every clock to re-push on the next loop tick (~500 ms) instead of
    # waiting for the next content change. Without this, clocks that are
    # already showing the standby image (e.g. after a server restart) wouldn't
    # tick refresh until the 40 s anti-bitrot push.
    _CLOCKS_LOOP_LAST_PUSHED.clear()
    return jsonify({"ok": True})


@app.route("/api/clocks/preview", methods=["GET"])
def api_clocks_preview():
    """Return the rendered JPEG for a given role + verbosity — used by the UI
    for an inline preview without the device, and for development."""
    role = (request.args.get("role") or "screen").lower()
    if role not in ROLE_ACCENT:
        role = "screen"
    verbosity = (request.args.get("verbosity") or SM_VERBOSITY_DEFAULT).lower()
    if verbosity not in SM_VERBOSITIES:
        verbosity = SM_VERBOSITY_DEFAULT
    state = _read_runsheet_state() or {}
    # If the operator hit Standby (or saved an explicit standby flag), preview
    # mirrors what the device is actually showing right now.
    if state.get("standby"):
        return Response(_render_standby(role), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    # No runsheet at all → fall through to demo data so first-time users can
    # see what a live cue will look like before loading a PDF.
    if not state.get("items"):
        state = {
            "items": [
                {"type": "song", "title": "Build My Life", "duration_min": 5,
                 "notes": "9:30 AM",
                 "cues": {"screen": "Cue song slides",
                          "sound":  "Band mics live · MC mute",
                          "lights": "Stage wash — band"}},
                {"type": "sermon", "title": "King Jesus — Ps Nick", "duration_min": 30,
                 "notes": "10:14 AM",
                 "cues": {"screen": "Sermon slides",
                          "sound":  "Mic on for Ps Nick",
                          "lights": "Spot — preacher"}}
            ],
            "current_index": 0,
            "current_started_at": _dt.datetime.now().isoformat(),
        }
    jpg = _render_cue(role, state, verbosity=verbosity)
    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


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
