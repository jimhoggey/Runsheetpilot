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
This is intentionally a single file so PyInstaller has one entry point.
Navigate by the "# ── ..." section dividers; inside the inline UI block,
JS sections use matching "// ── ..." dividers near the top of <script>.

Top-level regions, in order:
  1. Constants, user data dir, logging
  2. Flask app + global error handlers
  3. PDF extraction
  4. ProPresenter paths (cross-platform)
  5. Library scan from disk
  6. Fuzzy matching (song title → library)
  7. AI prompt template + per-type colour map
  8. Time/duration parsing + PP timer creation
  9. Settings load/save
 10. API routes (one block per /api/* endpoint)
 11. HTML / CSS / JS — the entire UI, inlined (~half the file)
 12. Server bootstrap (port, browser, waitress)

Common feature touch-points:
  - new API endpoint    → region 10  + JS caller in region 11
  - new UI panel        → HTML in region 11 + JS handler in region 11
  - new settings field  → _default_settings() in region 9 + UI in region 11
  - new runsheet type   → DEFAULT_PROMPT (region 7), TYPE_COLORS (region 7),
                          tagClass() and CSS .tag-* (region 11)
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

from flask import Flask, request, jsonify, Response

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
                log.info(f"Created timer: {timer_name}")
            else:
                errors.append(f"{timer_name} → HTTP {r.status_code}")
                log.warning(f"Timer create failed: {timer_name} → "
                            f"{r.status_code} {r.text[:120]}")
        except Exception as e:
            errors.append(f"{timer_name} → {type(e).__name__}")
            log.exception(f"Timer create exception for {timer_name}")
    return {
        "created":     created,
        "deleted":     deleted,
        "no_duration": no_duration,
        "total_items": total_items,
        "errors":      errors,
    }


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
    return Response(HTML, mimetype="text/html")


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
                        "total_items": 0, "errors": []}
        if body.get("create_timers"):
            timer_result = _create_pp_timers(base, name, matched)

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


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ProPresenter Runsheet Builder</title>
<style>
/* ─── tokens ───────────────────────────────────────────────────────────── */
:root {
  /* surface */
  --bg:        #0a0a14;
  --panel:     #14142a;
  --card:      #1c1c34;
  --card-2:    #252544;
  --border:    rgba(255,255,255,0.06);
  --border-2:  rgba(255,255,255,0.10);
  --border-3:  rgba(255,255,255,0.16);

  /* text */
  --text:      #ececf3;
  --muted:     #8b8b9c;
  --dim:       #5a5a6e;

  /* accent (kept blue, slightly refined) */
  --acc:       #3b82f6;
  --acc-2:     #60a5fa;
  --acc-soft:  rgba(59,130,246,0.16);
  --acc-glow:  rgba(59,130,246,0.32);

  --grn:       #22c55e;
  --grn-soft:  rgba(34,197,94,0.14);
  --org:       #f59e0b;
  --org-soft:  rgba(245,158,11,0.14);
  --red:       #ef4444;
  --red-soft:  rgba(239,68,68,0.14);

  /* spacing */
  --s-1:  4px;  --s-2:  8px;  --s-3: 12px;
  --s-4: 16px;  --s-5: 20px;  --s-6: 28px;

  /* radius */
  --r-1: 6px;  --r-2: 8px;  --r-3: 12px;  --r-4: 16px;

  /* shadow */
  --sh-1: 0 1px 2px rgba(0,0,0,0.25);
  --sh-2: 0 4px 14px rgba(0,0,0,0.30);
  --sh-3: 0 20px 60px rgba(0,0,0,0.55);
  --ring: 0 0 0 3px var(--acc-soft);

  /* fonts */
  --font:      -apple-system, BlinkMacSystemFont, 'Inter', 'SF Pro Text',
               'Segoe UI', Roboto, system-ui, sans-serif;
  --font-mono: 'SF Mono', 'JetBrains Mono', 'Menlo', 'Monaco', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html { color-scheme: dark; }
body {
  font-family: var(--font);
  background:
    radial-gradient(1200px 600px at 80% -10%, rgba(59,130,246,0.08), transparent 60%),
    radial-gradient(900px  600px at -10% 110%, rgba(99,102,241,0.06), transparent 60%),
    var(--bg);
  background-attachment: fixed;
  color: var(--text);
  min-height: 100vh;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

::selection { background: var(--acc-soft); }

/* scrollbars (subtle, modern) */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: rgba(255,255,255,0.08);
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background-color: rgba(255,255,255,0.18); }

/* ─── header ───────────────────────────────────────────────────────────── */
header {
  background:
    linear-gradient(135deg, rgba(29,78,216,0.95), rgba(59,130,246,0.85) 60%, rgba(96,165,250,0.85));
  padding: 13px 22px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 20;
  backdrop-filter: saturate(140%) blur(10px);
  -webkit-backdrop-filter: saturate(140%) blur(10px);
  border-bottom: 1px solid rgba(255,255,255,0.08);
  box-shadow: 0 1px 0 rgba(0,0,0,0.2);
}
header h1 {
  font-size: 0.95rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: #fff;
  display: flex; align-items: center; gap: 8px;
}
.header-right { display: flex; align-items: center; gap: 10px; }
header .badge {
  font-size: 0.68rem;
  font-weight: 600;
  color: rgba(255,255,255,0.85);
  background: rgba(255,255,255,0.12);
  padding: 4px 10px;
  border-radius: 999px;
  letter-spacing: 0.02em;
  border: 1px solid rgba(255,255,255,0.10);
}
.quit-btn {
  background: rgba(255,255,255,0.10);
  color: #fff;
  border: 1px solid rgba(255,255,255,0.15);
  padding: 6px 12px;
  border-radius: var(--r-1);
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  transition: background .15s, border-color .15s, transform .06s;
}
.quit-btn:hover { background: rgba(239,68,68,0.65); border-color: rgba(239,68,68,0.6); }
.quit-btn:active { transform: translateY(1px); }

/* ─── layout ───────────────────────────────────────────────────────────── */
.layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  align-items: start;
  min-height: calc(100vh - 52px);
}
aside {
  background: var(--panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 52px;
  align-self: start;
  height: calc(100vh - 52px);
  overflow-y: auto;
}
main {
  display: flex;
  flex-direction: column;
  padding: var(--s-5);
  gap: var(--s-4);
  min-width: 0;
}

/* ─── sidebar sections ─────────────────────────────────────────────────── */
.sidebar-section {
  padding: var(--s-4) var(--s-4) var(--s-2);
  border-bottom: 1px solid var(--border);
}
.sec-title {
  font-size: 0.66rem;
  font-weight: 700;
  color: var(--muted);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  margin-bottom: var(--s-3);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.save-dot {
  font-size: 0.65rem;
  color: var(--dim);
  font-weight: 600;
  letter-spacing: 0;
  text-transform: none;
  background: var(--card);
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  transition: color .15s, background .15s, border-color .15s;
}
.save-dot.saving { color: var(--org); background: var(--org-soft); border-color: rgba(245,158,11,0.25); }
.save-dot.saved  { color: var(--grn); background: var(--grn-soft); border-color: rgba(34,197,94,0.25); }

/* tabs (segmented) */
.sidebar-tabs {
  display: flex;
  gap: 3px;
  margin-bottom: var(--s-3);
  background: var(--card);
  padding: 3px;
  border-radius: var(--r-2);
  border: 1px solid var(--border);
}
.tab {
  flex: 1;
  padding: 6px 8px;
  text-align: center;
  border-radius: 6px;
  font-size: 0.78rem;
  font-weight: 600;
  cursor: pointer;
  background: transparent;
  color: var(--muted);
  border: none;
  font-family: inherit;
  transition: background .15s, color .15s;
}
.tab:hover { color: var(--text); }
.tab.active {
  background: var(--acc);
  color: #fff;
  box-shadow: 0 1px 2px rgba(0,0,0,0.2);
}
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ─── form ─────────────────────────────────────────────────────────────── */
label {
  display: block;
  font-size: 0.74rem;
  color: var(--muted);
  margin-bottom: 6px;
  font-weight: 500;
}
input, select, textarea {
  width: 100%;
  background: var(--card);
  color: var(--text);
  border: 1px solid var(--border-2);
  border-radius: var(--r-2);
  padding: 8px 10px;
  font-size: 0.85rem;
  font-family: inherit;
  outline: none;
  transition: border-color .15s, box-shadow .15s, background .15s;
  margin-bottom: var(--s-3);
}
input:hover, select:hover, textarea:hover { border-color: var(--border-3); }
input:focus, select:focus, textarea:focus {
  border-color: var(--acc);
  box-shadow: var(--ring);
}
input::placeholder, textarea::placeholder { color: var(--dim); }
.field { margin-bottom: 2px; }

/* range slider */
input[type="range"] {
  -webkit-appearance: none; appearance: none;
  background: transparent; padding: 0; margin: 0 0 var(--s-2);
  height: 18px; border: none; box-shadow: none;
}
input[type="range"]:focus { box-shadow: none; }
input[type="range"]::-webkit-slider-runnable-track {
  height: 4px; background: var(--card-2); border-radius: 2px;
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  height: 16px; width: 16px; border-radius: 50%;
  background: var(--acc); margin-top: -6px; cursor: pointer;
  border: 2px solid var(--bg);
  box-shadow: 0 1px 3px rgba(0,0,0,0.4), 0 0 0 0 var(--acc-glow);
  transition: box-shadow .15s, transform .06s;
}
input[type="range"]:hover::-webkit-slider-thumb { box-shadow: 0 1px 3px rgba(0,0,0,0.4), 0 0 0 4px var(--acc-glow); }
input[type="range"]:active::-webkit-slider-thumb { transform: scale(1.08); }

/* checkbox: keep simple, override form-element width */
input[type="checkbox"] {
  width: auto !important;
  margin: 0 !important;
  accent-color: var(--acc);
  cursor: pointer;
}

/* ─── buttons ──────────────────────────────────────────────────────────── */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 14px;
  border: none;
  border-radius: var(--r-2);
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
  color: #fff;
  font-family: inherit;
  white-space: nowrap;
  transition: filter .12s, transform .06s, box-shadow .15s, background .15s;
  box-shadow: var(--sh-1);
}
.btn:hover  { filter: brightness(1.08); }
.btn:active { transform: translateY(1px); filter: brightness(0.92); }
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  pointer-events: none;
  filter: grayscale(0.3);
}
.btn-acc { background: linear-gradient(180deg, var(--acc), #2563eb); }
.btn-grn { background: linear-gradient(180deg, var(--grn), #16a34a); }
.btn-dim {
  background: var(--card);
  color: var(--text);
  border: 1px solid var(--border-2);
  box-shadow: none;
}
.btn-dim:hover { background: var(--card-2); border-color: var(--border-3); }
.btn-full { width: 100%; }
.btn-sm   { padding: 6px 10px; font-size: 0.76rem; }

/* ─── cards ────────────────────────────────────────────────────────────── */
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--r-3);
  padding: var(--s-4);
  box-shadow: 0 1px 0 rgba(255,255,255,0.025) inset, var(--sh-1);
}
.card-title {
  font-weight: 700;
  font-size: 0.92rem;
  letter-spacing: -0.01em;
  margin-bottom: var(--s-3);
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ─── drop zone ────────────────────────────────────────────────────────── */
#drop-zone {
  border: 1.5px dashed var(--border-3);
  border-radius: var(--r-3);
  padding: 38px 20px;
  text-align: center;
  cursor: pointer;
  transition: border-color .18s, background .18s, transform .12s;
  background:
    radial-gradient(circle at 50% 30%, rgba(59,130,246,0.04), transparent 60%),
    var(--card);
}
#drop-zone:hover, #drop-zone.drag-over {
  border-color: var(--acc);
  background: var(--acc-soft);
  transform: translateY(-1px);
}
#drop-zone .icon { font-size: 2.3rem; margin-bottom: 8px; opacity: 0.9; }
#drop-zone .hint { color: var(--muted); font-size: 0.8rem; margin-top: 4px; }
#drop-zone.has-file {
  border-color: var(--grn);
  border-style: solid;
  background:
    radial-gradient(circle at 50% 30%, rgba(34,197,94,0.08), transparent 60%),
    var(--card);
}

/* ─── status bar ───────────────────────────────────────────────────────── */
#status-bar {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--r-2);
  padding: 10px var(--s-4);
  font-size: 0.82rem;
  color: var(--muted);
  min-height: 40px;
  display: flex;
  align-items: center;
  gap: 10px;
  line-height: 1.45;
}
.spinner {
  width: 14px; height: 14px;
  border: 2px solid var(--border-3);
  border-top-color: var(--acc);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
  flex-shrink: 0;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ─── results table ────────────────────────────────────────────────────── */
.results-wrap {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--r-3);
  overflow: hidden;
  box-shadow: var(--sh-1);
}
table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
th {
  text-align: left;
  padding: 11px 14px;
  color: var(--muted);
  font-size: 0.66rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  background: rgba(0,0,0,0.18);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
td {
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
tr { transition: background .12s; }
tr:hover td { background: rgba(255,255,255,0.025); }

/* tags (same colours, slight refinement) */
.tag {
  font-size: 0.68rem;
  padding: 3px 9px;
  border-radius: 999px;
  font-weight: 600;
  white-space: nowrap;
  display: inline-block;
  letter-spacing: 0.01em;
}
.tag-song   { background: #1e3a5f; color: #60a5fa; }
.tag-mc     { background: #0e3a3a; color: #5eead4; }
.tag-ann    { background: #3b2f1e; color: #fbbf24; }
.tag-serm   { background: #2d1b69; color: #a78bfa; }
.tag-prayer { background: #2a1e4a; color: #c4b5fd; }
.tag-script { background: #0e3a1a; color: #86efac; }
.tag-offer  { background: #3a2f10; color: #fcd34d; }
.tag-video  { background: #3a1a14; color: #fca5a5; }
.tag-oth    { background: rgba(255,255,255,0.06); color: var(--muted); }

.match-ok  { color: #4ade80; font-weight: 600; }
.match-bad { color: #f87171; }
.match-hdr { color: #93c5fd; }
.score     { font-size: 0.74rem; color: var(--muted); font-weight: 600; }

/* ─── notice ───────────────────────────────────────────────────────────── */
.notice {
  border-radius: var(--r-2);
  padding: 12px var(--s-4);
  font-size: 0.83rem;
  margin-top: 4px;
  line-height: 1.6;
  border: 1px solid;
}
.notice-ok  { background: var(--grn-soft); border-color: rgba(34,197,94,0.30);  color: #86efac; }
.notice-err { background: var(--red-soft); border-color: rgba(239,68,68,0.30); color: #fca5a5; }

code {
  font-family: var(--font-mono);
  font-size: 0.78rem;
  background: rgba(0,0,0,0.32);
  padding: 1px 6px;
  border-radius: 4px;
  color: #c7c7d6;
  border: 1px solid var(--border);
}

/* ─── library status row ───────────────────────────────────────────────── */
#lib-status {
  font-size: 0.74rem;
  color: var(--muted);
  padding: var(--s-2) 0 var(--s-1);
  min-height: 20px;
}
.stat-ok  { color: var(--grn) !important; }
.stat-err { color: var(--red) !important; }

/* ─── helpers ──────────────────────────────────────────────────────────── */
.row { display: flex; gap: 8px; align-items: flex-start; }
.row input, .row select { margin-bottom: 0; flex: 1; }

.footer-info {
  padding: var(--s-3) var(--s-4);
  font-size: 0.68rem;
  color: var(--dim);
  border-top: 1px solid var(--border);
  text-align: center;
}

/* ─── modal ────────────────────────────────────────────────────────────── */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.55);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  z-index: 50;
  display: none;
  align-items: center;
  justify-content: center;
  padding: var(--s-5);
  animation: fade-in .15s ease-out;
}
.modal-backdrop.active { display: flex; }
.modal {
  background: var(--panel);
  border: 1px solid var(--border-2);
  border-radius: var(--r-4);
  padding: var(--s-5);
  width: 100%;
  max-width: 920px;
  height: 85vh;
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  box-shadow: var(--sh-3);
  animation: scale-in .18s cubic-bezier(.2,.7,.3,1);
}
.modal-head { display: flex; justify-content: space-between; align-items: center; gap: var(--s-3); }
.modal-head h2 { font-size: 1rem; font-weight: 700; letter-spacing: -0.01em; }
.modal-hint { font-size: 0.77rem; color: var(--muted); line-height: 1.55; }
.modal textarea {
  flex: 1;
  width: 100%;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  line-height: 1.55;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border-2);
  border-radius: var(--r-2);
  padding: var(--s-3);
  resize: none;
  outline: none;
  margin: 0;
}
.modal textarea:focus { border-color: var(--acc); box-shadow: var(--ring); }
.modal-actions { display: flex; gap: 10px; align-items: center; justify-content: space-between; }

@keyframes fade-in  { from { opacity: 0; }                       to { opacity: 1; } }
@keyframes scale-in { from { opacity: 0; transform: translateY(6px) scale(.98); }
                        to { opacity: 1; transform: translateY(0)   scale(1);  } }

/* small helper: focus-visible accessibility */
button:focus-visible, .quit-btn:focus-visible {
  box-shadow: var(--ring);
  outline: none;
}
</style>
</head>
<body>

<header>
  <h1>🎛 ProPresenter Runsheet Builder</h1>
  <div class="header-right">
    <span class="badge" id="version-badge">v—</span>
    <button class="quit-btn" onclick="quitApp()" title="Stop the app and close the browser tab">Quit</button>
  </div>
</header>

<div class="layout">

<aside>

  <div class="sidebar-section">
    <div class="sec-title">
      <span>Library Source</span>
      <span class="save-dot" id="save-dot">auto-save</span>
    </div>
    <div class="sidebar-tabs">
      <button class="tab active" id="tab-btn-disk" onclick="switchTab('disk')">📁 From Disk</button>
      <button class="tab" id="tab-btn-api" onclick="switchTab('api')">🔌 Via API</button>
    </div>

    <div id="tab-disk" class="tab-panel active">
      <div class="field"><label>ProPresenter Library Folder</label>
        <input id="lib-dir" type="text" placeholder="Path to your Libraries folder…">
      </div>
      <button class="btn btn-grn btn-full" onclick="scanLibrary()">↓ Scan Library from Disk</button>
    </div>

    <div id="tab-api" class="tab-panel">
      <div class="field"><label>Host</label>
        <input id="pp-host" type="text" value="localhost">
      </div>
      <div class="field"><label>Port</label>
        <input id="pp-port" type="text" value="50001" style="width:90px">
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-dim btn-sm" onclick="testConnection()">Test</button>
        <button class="btn btn-grn" onclick="fetchLibraryApi()">↓ Fetch Library</button>
      </div>
    </div>

    <div id="lib-status">No library loaded</div>
  </div>

  <div class="sidebar-section">
    <div class="sec-title"><span>OpenRouter API</span></div>
    <div class="field"><label>API Key</label>
      <input id="or-key" type="password" placeholder="sk-or-…" autocomplete="off">
    </div>
    <div class="field"><label>Model — paste any OpenRouter model string</label>
      <input id="or-model" type="text"
             placeholder="e.g. google/gemini-2.0-flash-exp:free"
             title="Any model from openrouter.ai/models">
    </div>
    <div style="font-size:.72rem;color:var(--muted);margin-top:-6px;margin-bottom:8px">
      Find models at <code>openrouter.ai/models</code>
    </div>
    <button class="btn btn-dim btn-sm btn-full" onclick="openPromptModal()">
      📝 Show / edit AI prompt
    </button>
  </div>

  <!-- Second host/port section. The same values appear in the API tab above
       (#pp-host / #pp-port), but PP host/port is also relevant when *creating*
       the playlist, so we surface it again here. The two pairs are mirrored
       in JS (loadSettings binds input listeners both ways) so the user only
       ever sees one set of values. -->
  <div class="sidebar-section">
    <div class="sec-title"><span>ProPresenter Connection</span></div>
    <div class="row" style="margin-bottom:8px">
      <div class="field" style="flex:2;margin-bottom:0"><label>Host</label>
        <input id="pp-host2" type="text" value="localhost" style="margin-bottom:0">
      </div>
      <div class="field" style="flex:1;margin-bottom:0"><label>Port</label>
        <input id="pp-port2" type="text" value="50001" style="margin-bottom:0">
      </div>
    </div>
    <button class="btn btn-dim btn-sm" onclick="testConnection2()" style="margin-bottom:8px">⚡ Test Connection</button>
    <div style="font-size:.72rem;color:var(--muted)">
      Enable in ProPresenter → Preferences → Integrations → Network
    </div>
  </div>

  <div class="sidebar-section">
    <div class="sec-title"><span>Timers</span></div>
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;
                  font-size:.82rem;color:#e8e8f0;margin-bottom:8px">
      <input type="checkbox" id="create-timers" checked
             style="width:auto;margin:0;cursor:pointer">
      <span>Create countdown timers from runsheet times</span>
    </label>
    <div style="font-size:.72rem;color:var(--muted);line-height:1.5">
      For every section with a duration (e.g. <code>30 min</code> sermon),
      a countdown timer is added in PP's Timer panel. Operator starts each
      one when the section begins. Old <code>[RB]</code> timers are cleared
      first so re-runs don't pile up.
    </div>
  </div>

  <div class="sidebar-section">
    <div class="sec-title"><span>Match Sensitivity</span></div>
    <label>Threshold: <span id="thresh-val" style="color:var(--acc);font-weight:700">55%</span></label>
    <input type="range" id="threshold" min="30" max="90" value="55" style="margin-bottom:8px"
           oninput="document.getElementById('thresh-val').textContent=this.value+'%'">
  </div>

  <div class="sidebar-section">
    <div class="sec-title"><span>Export .playlist File To</span></div>
    <div class="field"><label>Folder (leave blank to skip export)</label>
      <input id="export-dir" type="text" placeholder="~/Desktop">
    </div>
  </div>

  <div class="footer-info" id="footer-info">—</div>

</aside>

<main>

  <div class="card">
    <div class="card-title">📄 Upload Runsheet PDF</div>
    <div id="drop-zone" onclick="document.getElementById('pdf-input').click()"
         ondragover="handleDragOver(event)" ondrop="handleDrop(event)"
         ondragleave="this.classList.remove('drag-over')">
      <div class="icon">☁️</div>
      <div style="font-weight:600;margin-bottom:4px">Drop your PDF here or click to browse</div>
      <div class="hint">Runsheet · Order of Service · any PDF</div>
    </div>
    <input id="pdf-input" type="file" accept=".pdf" style="display:none"
           onchange="handleFileSelect(this.files[0])">
    <div style="margin-top:10px">
      <button class="btn btn-acc btn-full" id="parse-btn" onclick="parseRunsheet()">
        🔍 Parse Runsheet with AI
      </button>
    </div>
  </div>

  <div id="status-bar">
    <span>Ready — load your library, then upload a PDF.</span>
  </div>

  <div class="card">
    <div class="card-title">🎵 Create Playlist in ProPresenter</div>
    <div class="row" style="align-items:stretch">
      <div style="flex:1">
        <label>Playlist Name</label>
        <input id="playlist-name" type="text" placeholder="Sunday Service · 4 May 2026" style="margin-bottom:0">
      </div>
      <div style="display:flex;align-items:flex-end">
        <button class="btn btn-grn" id="create-btn" style="padding:10px 18px" onclick="createPlaylist()">
          ✓ Create Playlist + Export File
        </button>
      </div>
    </div>
  </div>

  <div class="results-wrap">
    <table>
      <thead>
        <tr>
          <th style="width:32px">#</th>
          <th style="width:100px">Type</th>
          <th>Title / Description</th>
          <th>Matched Library Item</th>
          <th style="width:56px">Score</th>
        </tr>
      </thead>
      <tbody id="results-body">
        <tr><td colspan="5" style="color:var(--muted);padding:24px;text-align:center">
          Parse a runsheet to see items here.
        </td></tr>
      </tbody>
    </table>
  </div>

  <div id="result-notice"></div>

</main>
</div>

<!-- ═══════════ Edit AI Prompt Modal ═══════════ -->
<div id="prompt-modal" class="modal-backdrop"
     onclick="if(event.target===this)closePromptModal()">
  <div class="modal">
    <div class="modal-head">
      <h2>📝 AI Prompt</h2>
      <button class="btn btn-dim btn-sm" onclick="closePromptModal()">✕ Close</button>
    </div>
    <div class="modal-hint">
      The runsheet text replaces the <code>{RUNSHEET}</code> placeholder
      (or is appended if you remove it). Changes auto-save.
      Empty text reverts to the built-in default.
    </div>
    <textarea id="prompt-textarea" spellcheck="false"></textarea>
    <div class="modal-actions">
      <button class="btn btn-dim btn-sm" onclick="resetPrompt()">↺ Reset to default</button>
      <span id="prompt-save-dot" class="save-dot">auto-save</span>
    </div>
  </div>
</div>

<script>
/* ─────────────────────────────────────────────────────────────────────────
   UI script. Sections below match the regions in the Python module map:
     1. Globals + UI helpers
     2. Settings: load + auto-save
     3. PDF upload + drag-and-drop
     4. Library load (disk + API)
     5. Connection test
     6. Parse + render results table
     7. Create playlist in ProPresenter
     8. AI prompt modal
     9. Quit + boot
   ───────────────────────────────────────────────────────────────────────── */

// ─── 1. Globals + UI helpers ──────────────────────────────────────────────
let libraryItems = [];
let matchedItems = [];
let uploadedFile = null;
let saveTimer = null;
let suppressAutoSave = true; // suppress during initial loadSettings()

const AUTOSAVE_FIELDS = [
  'or-key', 'or-model', 'lib-dir', 'export-dir',
  'pp-host', 'pp-port', 'pp-host2', 'pp-port2', 'threshold',
  'create-timers'
];

function setSaveDot(state) {
  const dot = document.getElementById('save-dot');
  dot.className = 'save-dot ' + (state || '');
  dot.textContent = state === 'saving' ? 'saving…'
                 : state === 'saved'  ? 'saved ✓'
                 : 'auto-save';
}

function switchTab(t) {
  document.getElementById('tab-btn-disk').classList.toggle('active', t === 'disk');
  document.getElementById('tab-btn-api').classList.toggle('active', t === 'api');
  document.getElementById('tab-disk').classList.toggle('active', t === 'disk');
  document.getElementById('tab-api').classList.toggle('active', t === 'api');
}

function setStatus(html, color) {
  const bar = document.getElementById('status-bar');
  bar.innerHTML = html;
  bar.style.color = color || 'var(--muted)';
}
function setLoading(msg) {
  setStatus(`<div class="spinner"></div>${msg}`);
}

// ─── 2. Settings: load + auto-save ────────────────────────────────────────
async function loadSettings() {
  const s = await fetch('/api/settings').then(r => r.json());
  document.getElementById('pp-host').value    = s.pp_host  || 'localhost';
  document.getElementById('pp-port').value    = s.pp_port  || '50001';
  document.getElementById('pp-host2').value   = s.pp_host  || 'localhost';
  document.getElementById('pp-port2').value   = s.pp_port  || '50001';
  document.getElementById('or-key').value     = s.or_key   || '';
  document.getElementById('or-model').value   = s.or_model || 'google/gemini-2.0-flash-exp:free';
  document.getElementById('lib-dir').value    = s.library_dir || '';
  document.getElementById('export-dir').value = s.export_dir  || '';
  document.getElementById('threshold').value  = Math.round((s.threshold || .55) * 100);
  document.getElementById('thresh-val').textContent = document.getElementById('threshold').value + '%';
  document.getElementById('create-timers').checked = s.create_timers !== false;

  const today = new Date().toLocaleDateString('en-AU',
      {day:'2-digit', month:'short', year:'numeric'});
  document.getElementById('playlist-name').value = 'Service ' + today;

  document.getElementById('version-badge').textContent = 'v' + (s.version || '?');
  document.getElementById('footer-info').innerHTML =
    `Settings: <code>${s.data_dir || ''}</code>`;

  const isMac = s.platform === 'darwin';
  document.getElementById('lib-dir').placeholder = isMac
    ? '~/Documents/ProPresenter/Libraries/Default'
    : 'C:\\Users\\…\\Documents\\ProPresenter\\Libraries\\Default';
  document.getElementById('export-dir').placeholder = isMac
    ? '~/Desktop'
    : 'C:\\Users\\…\\Desktop';

  // Wire up auto-save AFTER initial values are set, so setting them doesn't trigger a save
  AUTOSAVE_FIELDS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('input', autoSaveDebounced);
    el.addEventListener('change', autoSaveDebounced);
  });
  // Mirror host/port between the two sections
  document.getElementById('pp-host').addEventListener('input', e =>
    document.getElementById('pp-host2').value = e.target.value);
  document.getElementById('pp-port').addEventListener('input', e =>
    document.getElementById('pp-port2').value = e.target.value);
  document.getElementById('pp-host2').addEventListener('input', e =>
    document.getElementById('pp-host').value = e.target.value);
  document.getElementById('pp-port2').addEventListener('input', e =>
    document.getElementById('pp-port').value = e.target.value);

  suppressAutoSave = false;
  setSaveDot('');
}

function autoSaveDebounced() {
  if (suppressAutoSave) return;
  setSaveDot('saving');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveSettings, 500);
}

async function saveSettings() {
  const data = {
    pp_host:       document.getElementById('pp-host2').value,
    pp_port:       document.getElementById('pp-port2').value,
    or_key:        document.getElementById('or-key').value,
    or_model:      document.getElementById('or-model').value,
    library_dir:   document.getElementById('lib-dir').value,
    export_dir:    document.getElementById('export-dir').value,
    threshold:     parseInt(document.getElementById('threshold').value) / 100,
    create_timers: document.getElementById('create-timers').checked,
  };
  try {
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    setSaveDot('saved');
    setTimeout(() => setSaveDot(''), 1500);
  } catch (e) {
    setSaveDot('');
    setStatus('Could not save settings: ' + e, 'var(--red)');
  }
}

// ─── 3. PDF upload + drag-and-drop ────────────────────────────────────────
function handleDragOver(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.add('drag-over');
}
function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file && file.name.toLowerCase().endsWith('.pdf')) handleFileSelect(file);
  else setStatus('Only .pdf files are supported.', 'var(--red)');
}
function handleFileSelect(file) {
  if (!file) return;
  uploadedFile = file;
  const dz = document.getElementById('drop-zone');
  dz.classList.add('has-file');
  dz.innerHTML = `
    <div style="font-size:1.8rem;margin-bottom:6px">✅</div>
    <div style="font-weight:700;color:var(--grn)">${file.name}</div>
    <div class="hint">${(file.size/1024).toFixed(0)} KB — click to change</div>`;
  dz.onclick = () => document.getElementById('pdf-input').click();
  setStatus(`PDF loaded: ${file.name} — click Parse to send to AI.`);
}

// ─── 4. Library load (disk + API) ─────────────────────────────────────────
function setLibStatus(msg, cls) {
  const el = document.getElementById('lib-status');
  el.textContent = msg;
  el.className = cls || '';
}

async function scanLibrary() {
  const dir = document.getElementById('lib-dir').value.trim();
  if (!dir) { setStatus('Enter the library folder path first.', 'var(--red)'); return; }
  setLoading('Scanning library from disk…');
  try {
    const res = await fetch('/api/library/scan', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({directory: dir})
    }).then(r => r.json());
    if (res.error) { setStatus('Error: ' + res.error, 'var(--red)'); setLibStatus('Scan failed', 'stat-err'); return; }
    libraryItems = res.items;
    setLibStatus(`✓  ${res.count} items loaded from disk`, 'stat-ok');
    setStatus(`Library scanned — ${res.count} presentations ready for matching.`, 'var(--grn)');
  } catch (e) {
    setStatus('Scan failed: ' + e, 'var(--red)');
  }
}

async function fetchLibraryApi() {
  setLoading('Fetching library from ProPresenter…');
  try {
    const res = await fetch('/api/library/fetch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        host: document.getElementById('pp-host').value,
        port: document.getElementById('pp-port').value,
      })
    }).then(r => r.json());
    if (res.error) { setStatus('Error: ' + res.error, 'var(--red)'); setLibStatus('Fetch failed', 'stat-err'); return; }
    libraryItems = res.items;
    setLibStatus(`✓  ${res.count} items via API`, 'stat-ok');
    setStatus(`Library loaded — ${res.count} presentations.`, 'var(--grn)');
  } catch (e) {
    setStatus('Fetch failed: ' + e, 'var(--red)');
  }
}

// ─── 5. Connection test ───────────────────────────────────────────────────
// Two near-identical test functions because the host/port fields appear in
// two sidebar sections (Library API tab + ProPresenter Connection). Each one
// reads the values from its own section's inputs so the user can verify
// either form before relying on it.
async function _runTest(host, port) {
  return await fetch('/api/test_connection', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({host, port})
  }).then(r => r.json());
}

async function testConnection() {
  const res = await _runTest(
    document.getElementById('pp-host').value,
    document.getElementById('pp-port').value);
  alert(res.ok
    ? `✅ Connected — ${res.count} library/libraries found.`
    : `❌ Failed:\n${res.error}\n\nEnable Network in ProPresenter → Preferences → Integrations.`);
}
async function testConnection2() {
  const res = await _runTest(
    document.getElementById('pp-host2').value,
    document.getElementById('pp-port2').value);
  alert(res.ok
    ? `✅ Connected — ProPresenter is ready.`
    : `❌ Failed:\n${res.error}\n\nMake sure ProPresenter is running and Network is enabled.`);
}

// ─── 6. Parse runsheet + render results table ─────────────────────────────
async function parseRunsheet() {
  if (!uploadedFile) { setStatus('Upload a PDF first.', 'var(--red)'); return; }
  if (!document.getElementById('or-key').value.trim()) {
    setStatus('Enter your OpenRouter API key in the sidebar.', 'var(--red)'); return; }

  const btn = document.getElementById('parse-btn');
  btn.disabled = true;
  setLoading('Uploading PDF and sending to AI…');

  const form = new FormData();
  form.append('pdf',      uploadedFile);
  form.append('or_key',   document.getElementById('or-key').value.trim());
  form.append('or_model', document.getElementById('or-model').value.trim());

  try {
    const res = await fetch('/api/upload_and_parse', {method:'POST', body: form})
      .then(r => r.json());
    if (res.error) { setStatus('❌ ' + res.error, 'var(--red)'); return; }

    // Auto-populate the playlist name from the AI-extracted service name
    // (or filename fallback). User can still edit it before creating.
    if (res.suggested_name) {
      document.getElementById('playlist-name').value = res.suggested_name;
    }

    setLoading(`AI found ${res.items.length} items — matching to library…`);

    const threshold = parseInt(document.getElementById('threshold').value) / 100;
    const matchRes = await fetch('/api/match', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({parsed: res.items, library: libraryItems, threshold})
    }).then(r => r.json());

    matchedItems = matchRes.items;
    renderResults();
  } catch (e) {
    setStatus('❌ ' + e, 'var(--red)');
  } finally {
    btn.disabled = false;
  }
}

function tagClass(type) {
  const m = {
    song:         'tag-song',
    mc_on_stage:  'tag-mc',
    announcement: 'tag-ann',
    sermon:       'tag-serm',
    prayer:       'tag-prayer',
    scripture:    'tag-script',
    offering:     'tag-offer',
    video:        'tag-video',
  };
  return m[type] || 'tag-oth';
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function renderResults() {
  const tbody = document.getElementById('results-body');
  tbody.innerHTML = '';
  let matched = 0, total = 0;

  matchedItems.forEach((mi, i) => {
    const p = mi.parsed, m = mi.match, conf = mi.confidence;
    if (p.type === 'song') total++;

    let matchCell, scoreCell;
    if (p.type === 'song') {
      if (m) {
        matchCell  = `<span class="match-ok">${escapeHtml(m.name)}</span>`;
        scoreCell  = `<span class="score">${Math.round(conf*100)}%</span>`;
        matched++;
      } else {
        matchCell = `<span class="match-bad">⚠ ACTION NEEDED placeholder</span>
          <button class="btn btn-dim btn-sm" style="margin-left:6px"
                  onclick="pickManual(${i})">Pick</button>`;
        scoreCell = `<span class="score match-bad">${Math.round(conf*100)}%</span>`;
      }
    } else {
      matchCell = `<span class="match-hdr">→ section header</span>`;
      scoreCell = '';
    }

    const notes = p.notes
      ? `<span style="color:var(--muted);font-size:.78rem"> [${escapeHtml(p.notes)}]</span>` : '';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:var(--muted)">${i+1}</td>
      <td><span class="tag ${tagClass(p.type)}">${escapeHtml(p.type)}</span></td>
      <td>${escapeHtml(p.title)}${notes}</td>
      <td>${matchCell}</td>
      <td>${scoreCell}</td>`;
    tbody.appendChild(tr);
  });

  const unmatched = total - matched;
  let msg, color;
  if (total === 0) {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; no songs in this runsheet &nbsp;·&nbsp; Click <strong>Create Playlist</strong> when ready.`;
    color = 'var(--grn)';
  } else if (libraryItems.length === 0) {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; <span style="color:var(--org)"><strong>No ProPresenter songs loaded yet</strong> — all ${total} song${total!==1?'s':''} will become red <strong>ACTION NEEDED</strong> placeholders. To match automatically, click <strong>↓ Scan Library from Disk</strong> (or <strong>↓ Fetch Library</strong>) at the top of the sidebar, then re-parse.</span>`;
    color = 'var(--org)';
  } else if (unmatched) {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; ${matched}/${total} songs matched &nbsp;·&nbsp; <span style="color:var(--org)">${unmatched} unmatched — click <strong>Pick</strong> to choose manually, or leave for an <strong>ACTION NEEDED</strong> placeholder in the playlist.</span>`;
    color = 'var(--org)';
  } else {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; ✓ all ${total} song${total!==1?'s':''} matched &nbsp;·&nbsp; Click <strong>Create Playlist</strong> when ready.`;
    color = 'var(--grn)';
  }
  setStatus(msg, color);
}

function pickManual(idx) {
  const q = prompt('Search for song name (or leave blank to see all):') || '';
  const hits = libraryItems.filter(x =>
    !q || (x.name || '').toLowerCase().includes(q.toLowerCase()));
  if (!hits.length) { alert('No matches found.'); return; }
  const list = hits.slice(0, 20).map((m, i) => `${i+1}. ${m.name}`).join('\n');
  const pick = parseInt(prompt(`Choose a number:\n${list}`));
  if (isNaN(pick) || pick < 1 || pick > hits.length) return;
  matchedItems[idx].match      = hits[pick-1];
  matchedItems[idx].confidence = 1.0;
  renderResults();
}

// ─── 7. Create playlist in ProPresenter ───────────────────────────────────
async function createPlaylist() {
  if (!matchedItems.length) { setStatus('Parse a runsheet first.', 'var(--red)'); return; }
  const name = document.getElementById('playlist-name').value.trim();
  if (!name) { setStatus('Enter a playlist name.', 'var(--red)'); return; }

  const btn = document.getElementById('create-btn');
  btn.disabled = true;
  setLoading('Creating playlist in ProPresenter…');

  try {
    const res = await fetch('/api/create_playlist', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        host:          document.getElementById('pp-host2').value,
        port:          document.getElementById('pp-port2').value,
        name,
        matched:       matchedItems,
        export_dir:    document.getElementById('export-dir').value,
        create_timers: document.getElementById('create-timers').checked,
      })
    }).then(r => r.json());

    const notice = document.getElementById('result-notice');
    if (res.error) {
      notice.innerHTML = `<div class="notice notice-err">❌ ${escapeHtml(res.error)}</div>`;
      setStatus('Error creating playlist.', 'var(--red)');
      return;
    }

    let html = `<div class="notice notice-ok">
      ✅ <strong>Playlist "${escapeHtml(name)}" created in ProPresenter!</strong><br>
      ${res.songs} song${res.songs!==1?'s':''} added &nbsp;·&nbsp;
      ${res.headers} section header${res.headers!==1?'s':''}`;
    if (res.needs_action) html += ` &nbsp;·&nbsp; <span style="color:#fca5a5">⚠ ${res.needs_action} <strong>ACTION NEEDED</strong> placeholder${res.needs_action!==1?'s':''}</span> — open the playlist in ProPresenter and add the song${res.needs_action!==1?'s':''} manually.`;
    if (res.timers_deleted) {
      html += `<br>🧹 Cleared ${res.timers_deleted} previous <code>[RB]</code> timer${res.timers_deleted!==1?'s':''} from PP.`;
    }
    if (res.timers_created) {
      html += `<br>⏱ <strong>${res.timers_created} duration countdown timer${res.timers_created!==1?'s':''}</strong> created in PP (named with <code>[RB]</code> prefix, prefixed by sequence number for runsheet order) — open the Timer panel and start each one when its section begins.`;
    } else if (res.timers_total_items > 0 && res.timers_no_duration === res.timers_total_items) {
      html += `<br>⏱ <span style="color:#fbbf24">No timers created — none of the timeable items had a recognisable duration.</span> Make sure your runsheet shows minutes (e.g. <code>9:30 AM 20 Worship and Ministry Time</code> — the 20 is the duration).`;
    } else if (res.timers_no_duration > 0) {
      html += `<br>⏱ ${res.timers_no_duration} item${res.timers_no_duration!==1?'s':''} skipped (no duration in runsheet).`;
    }
    if (res.timer_errors && res.timer_errors.length) {
      html += `<br><span style="color:#fbbf24">⚠ ${res.timer_errors.length} timer${res.timer_errors.length!==1?'s':''} failed:</span> <code style="font-size:.72rem">${escapeHtml(res.timer_errors.join('; '))}</code>`;
    }
    if (res.export_path) {
      html += `<br><br>📁 <strong>.playlist file exported to:</strong><br>
        <code>${escapeHtml(res.export_path)}</code>`;
    } else if (document.getElementById('export-dir').value) {
      html += `<br><br>⚠️ Could not find the exported file automatically.
        Check your ProPresenter/Playlists/ folder.`;
    }
    html += '</div>';
    notice.innerHTML = html;
    const extra = res.needs_action ? `, ${res.needs_action} ACTION NEEDED` : '';
    setStatus(`✅ Playlist "${name}" created — ${res.songs} songs, ${res.headers} headers${extra}.`,
              res.needs_action ? 'var(--org)' : 'var(--grn)');
  } catch (e) {
    setStatus('❌ ' + e, 'var(--red)');
  } finally {
    btn.disabled = false;
  }
}

// ─── 8. AI prompt modal ───────────────────────────────────────────────────
let promptSaveTimer = null;

async function openPromptModal() {
  const ta = document.getElementById('prompt-textarea');
  ta.value = '';
  document.getElementById('prompt-modal').classList.add('active');
  setPromptSaveDot('');
  try {
    const res = await fetch('/api/prompt').then(r => r.json());
    ta.value = res.prompt || '';
    if (res.is_default) setPromptSaveDot('default');
  } catch (e) {
    ta.value = '(could not load prompt: ' + e + ')';
  }
}

function closePromptModal() {
  document.getElementById('prompt-modal').classList.remove('active');
}

function setPromptSaveDot(state) {
  const dot = document.getElementById('prompt-save-dot');
  dot.className = 'save-dot ' + (state === 'saving' || state === 'saved' ? state : '');
  dot.textContent = state === 'saving' ? 'saving…'
                  : state === 'saved'  ? 'saved ✓'
                  : state === 'default' ? 'using default'
                  : 'auto-save';
}

function autoSavePromptDebounced() {
  setPromptSaveDot('saving');
  clearTimeout(promptSaveTimer);
  promptSaveTimer = setTimeout(savePrompt, 600);
}

async function savePrompt() {
  const value = document.getElementById('prompt-textarea').value;
  try {
    await fetch('/api/prompt', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: value})
    });
    setPromptSaveDot(value.trim() ? 'saved' : 'default');
    if (value.trim()) setTimeout(() => setPromptSaveDot(''), 1500);
  } catch (e) {
    setPromptSaveDot('');
    alert('Could not save prompt: ' + e);
  }
}

async function resetPrompt() {
  if (!confirm('Discard your customisations and revert to the built-in default prompt?')) return;
  try {
    await fetch('/api/prompt', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: ''})
    });
    const res = await fetch('/api/prompt').then(r => r.json());
    document.getElementById('prompt-textarea').value = res.prompt || '';
    setPromptSaveDot('default');
  } catch (e) {
    alert('Could not reset: ' + e);
  }
}

// ─── 9. Quit + boot ───────────────────────────────────────────────────────
async function quitApp() {
  if (!confirm('Quit the ProPresenter Runsheet Builder?\n\nYou can reopen it from your Applications folder.')) return;
  try { await fetch('/api/quit', {method:'POST'}); } catch (_) {}
  document.body.innerHTML = '<div style="padding:40px;text-align:center;font-family:sans-serif">'
    + '<h2>👋 Server stopped.</h2><p style="color:#888;margin-top:10px">You can close this tab.</p></div>';
}

loadSettings();

// Wire up the prompt-modal textarea + ESC-to-close (script runs after DOM
// is parsed, so the elements exist).
document.getElementById('prompt-textarea')
        .addEventListener('input', autoSavePromptDebounced);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closePromptModal();
});
</script>
</body>
</html>
"""


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
