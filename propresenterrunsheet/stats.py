"""Anonymous usage counts + crash reporting via Aptabase.

Ported from the Service Visuals implementation (see
docs/aptabase-learnings.md in that repo). Design rules, all load-bearing:

  * **Fire and forget.** `track()` puts a dict on a bounded queue and
    returns. A daemon thread POSTs it. Every failure is swallowed, a full
    queue drops events. Nothing in a request path ever waits on the
    network — a church wifi outage must not slow down a parse.
  * **The EVENTS tuple below is the entire privacy surface.** `track()`
    ignores any name not in it, so the complete list of what this app can
    ever report fits on one screen and anyone can audit it.
  * **Names and numbers, never content.** No runsheet text, song titles,
    media filenames, paths, or anything the operator typed. Counts and
    durations only. Runsheets are a church's internal documents and the
    media names carry event branding ("C3 SUMMIT 2025 …") — those stay on
    the machine, in app.log, where the operator can read them.
  * **Off in dev.** No-op unless the app is frozen, or
    RUNSHEET_PILOT_STATS=1 forces it on. RUNSHEET_PILOT_STATS=0 forces it
    off (CI sets this).
  * **Operator can turn it off** — Settings toggle, `stats_enabled`.

The app key is write-only; it is safe in a public repo.
"""

import json
import os
import platform
import queue
import random
import re
import sys
import threading
import time
import urllib.request

from .config import DATA_DIR, VERSION

APP_KEY = "A-US-8568533212"
_REGION = APP_KEY.split("-")[1] if APP_KEY.count("-") >= 2 else "US"
HOST = {"US": "https://us.aptabase.com",
        "EU": "https://eu.aptabase.com"}.get(_REGION, "https://us.aptabase.com")
SDK_VERSION = "runsheet-pilot-stdlib@1.0.0"

# ── the entire privacy surface ───────────────────────────────────────────
EVENTS = (
    # lifecycle
    "app_started",
    "app_quit",
    "startup_failed",
    "whats_new_shown",
    # upload / extraction
    "runsheet_uploaded",
    "ocr_used",
    "extract_failed",
    # the AI phase
    "parse_completed",
    "parse_failed",
    "rows_rescued",
    # matching
    "match_completed",
    "rematch_used",
    "matching_disabled",
    "library_loaded",
    "template_used",
    # creating the playlist in ProPresenter
    "playlist_created",
    "playlist_failed",
    "media_unlinked",
    # the downloaded-media assist panel
    "media_assist_shown",
    "media_assist_reveal",
    "port_discovered",
    # config + updates
    "settings_saved",
    "update_available",
    "update_applied",
    "service_mate_toggled",
    # failures
    "crash",
)

# ── settings_saved: WHICH setting changed ───────────────────────────────────
#
# The event used to carry keys=len(body) and nothing else. That was always
# 15, because the UI posts the whole settings object on every autosave, so
# the number said "a save happened" and not one thing more.
#
# Field NAMES are our own constants and are always safe to send. VALUES are
# emphatically not: this one dict holds the OpenRouter API key, the Service
# Mate licence key, a hostname and two filesystem paths, any of which
# identifies the operator or is an outright secret. And note that track()
# does NOT scrub prop values — scrub() is wired only to crash messages — so
# a value passed here goes out verbatim.
#
# Hence deny-by-default: a value is transmitted ONLY if its field is listed
# below. Anything new is name-only until a human decides otherwise, which is
# the right way round for a list whose failure mode is leaking a key.
SETTINGS_VALUE_SAFE = frozenset({
    # Booleans — the new state of a toggle IS the useful signal.
    "stats_enabled", "auto_port", "media_assist", "create_timers", "sm_hide",
    "lib_source",    # 'auto' | 'api' | 'disk'
    "threshold",     # 0.0-1.0
    "or_model",      # model id, already sent alongside parse_completed
    "pp_port",       # a port number is not personal, and the install that
                     # ran on 55416 instead of 50001 is exactly the kind of
                     # thing worth seeing across the fleet
})

# Prop names this function owns. A settings field sharing either name would
# be silently overwritten; tests/test_stats.py asserts none ever does.
_SETTINGS_META_PROPS = ("changed", "n_changed")


def settings_change_props(before: dict, after: dict) -> dict:
    """Describe what a settings save actually changed, safely.

    `after` is the POST body, which may be a partial update — the UI posts
    all 15 fields, but the parse-timing writer posts one. Only keys present
    in `after` are considered, and each is compared against what was on disk.

    A no-op save reports n_changed=0 rather than being dropped: redundant
    saves are worth being able to see and count, since a loop that rewrites
    identical settings is a bug that would otherwise be invisible.
    """
    changed = sorted(k for k, v in after.items() if before.get(k) != v)
    props: dict = {"changed": ",".join(changed)[:200], "n_changed": len(changed)}
    for k in changed:
        if k in SETTINGS_VALUE_SAFE and k not in _SETTINGS_META_PROPS:
            v = after[k]
            if isinstance(v, (str, int, float, bool)):
                props[k] = v
    return props


# A crash loop is one bug, not 500 events.
_MAX_ERRORS_PER_RUN = 20

_q: "queue.Queue" = queue.Queue(maxsize=128)
_state = {"sid": None, "sid_at": 0.0, "started": False,
          "errors": 0, "enabled": None, "boot": 0.0}
_lock = threading.Lock()


# ── enablement ───────────────────────────────────────────────────────────

def _forced():
    """Explicit env override, or None to decide normally."""
    raw = (os.environ.get("RUNSHEET_PILOT_STATS") or "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return None


def enabled() -> bool:
    """True when events may be sent right now.

    Frozen builds only (so dev runs and the test suite never report),
    unless the env var forces it, and never when the operator has turned
    the Settings toggle off.
    """
    if _state["enabled"] is not None:
        return _state["enabled"]
    forced = _forced()
    if forced is False:
        return False
    if forced is None and not getattr(sys, "frozen", False):
        return False
    try:                                    # local import: avoid a cycle
        from .settings import load_settings
        if load_settings().get("stats_enabled") is False:
            return False
    except Exception:
        pass
    return True


def set_enabled(on: bool) -> None:
    """Apply the Settings toggle immediately (also cached for this run)."""
    _state["enabled"] = bool(on) if not _forced() == False else False


# ── session ──────────────────────────────────────────────────────────────

def _session_id() -> str:
    """Anonymous, in-memory only, rotated after an hour idle.

    Never persisted — persisting it would make two separate runs
    linkable, which is the whole thing this design avoids.
    """
    now = time.time()
    with _lock:
        if _state["sid"] is None or now - _state["sid_at"] > 3600:
            _state["sid"] = str(int(now) * 100000000
                                + random.randint(0, 99999999))
        _state["sid_at"] = now
        return _state["sid"]


# ── transport ────────────────────────────────────────────────────────────

def _ssl_context():
    """A context with real CA certs.

    A frozen Python ships no CA bundle, so HTTPS silently fails in the
    packaged app while working perfectly from source — exactly the bug
    that is impossible to notice in dev. requests already vendors
    certifi, so it is present in every build.
    """
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _post(payload: dict) -> None:
    req = urllib.request.Request(
        HOST + "/api/v0/event",
        data=json.dumps(payload).encode("utf-8"),
        headers={"App-Key": APP_KEY, "Content-Type": "application/json"},
        method="POST")
    ctx = _ssl_context()
    if ctx is not None:
        urllib.request.urlopen(req, timeout=5, context=ctx).read()
    else:
        urllib.request.urlopen(req, timeout=5).read()


def _worker() -> None:
    while True:
        payload = _q.get()
        try:
            _post(payload)
        except Exception:
            pass                            # analytics never surface errors
        finally:
            _q.task_done()


def _os_name() -> str:
    return {"darwin": "macOS", "win32": "Windows"}.get(
        sys.platform, platform.system() or "Unknown")


def _os_version() -> str:
    try:
        if sys.platform == "darwin":
            return platform.mac_ver()[0] or platform.release()
        return platform.release()
    except Exception:
        return ""


def track(name: str, **props) -> None:
    """Queue one event. Silent no-op when disabled or not whitelisted."""
    if name not in EVENTS or not enabled():
        return
    try:
        clean = {}
        for k, v in props.items():
            if not isinstance(v, (str, int, float, bool)) or v is None:
                continue
            k = str(k)[:40]
            clean[k] = v[:200] if isinstance(v, str) else v
        _q.put_nowait({
            "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                         time.gmtime()),
            "sessionId":   _session_id(),
            "eventName":   name,
            "systemProps": {
                "isDebug":    not getattr(sys, "frozen", False),
                "osName":     _os_name(),
                "osVersion":  _os_version(),
                "appVersion": VERSION,
                "sdkVersion": SDK_VERSION,
            },
            "props": clean,
        })
    except queue.Full:
        pass
    except Exception:
        pass


def flush(timeout: float = 3.0) -> None:
    """Wait (briefly) for queued events — only on the way down."""
    if not _state["started"]:
        return
    end = time.time() + timeout
    while not _q.empty() and time.time() < end:
        time.sleep(0.05)


# ── crash reporting ──────────────────────────────────────────────────────

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# A path may contain SPACES — "\\server\share\C3 SUMMIT 2025.mp4",
# "/Users/x/Youth Runsheet.pdf". Tokenising on whitespace (the obvious
# first attempt) scrubs only the first fragment and leaves the event
# name bare, which is exactly the content this must never send. So:
# start at a drive letter, UNC prefix or slash and run lazily to the
# file extension; fall back to the token rule for extension-less paths.
_PATHY = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\n\"']*?\.[A-Za-z0-9]{1,5}\b"
    r"|\S*[/\\]\S*")
# A bare filename, also possibly spaced. Bounded to a few words so it
# cannot swallow a whole sentence.
_FILEY = re.compile(r"(?:[\w\-]+[ ]){0,6}[\w\-]+\.[A-Za-z0-9]{1,5}\b")


def scrub(msg: str, limit: int = 160) -> str:
    """Strip anything identifying out of an exception message.

    Order matters (learned the hard way): quoted reprs first — they hold
    user values like KeyError('Team name') — then anything with a path
    separator, then bare filenames, then the OS username.
    """
    text = str(msg or "")
    text = _QUOTED.sub("'…'", text)
    text = _PATHY.sub("<path>", text)
    text = _FILEY.sub("<file>", text)
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if user and len(user) > 2:
        text = text.replace(user, "<user>")
    return text[:limit]


def _where(tb, frames: int = 4) -> str:
    """The innermost frames as 'file.py:12 func', our own files preferred."""
    import traceback
    try:
        stack = traceback.extract_tb(tb)
    except Exception:
        return ""
    ours = [f for f in stack if "propresenterrunsheet" in (f.filename or "")
            or "propresenter_app" in (f.filename or "")]
    picked = (ours or stack)[-frames:]
    return " < ".join(
        f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
        for f in reversed(picked))


def report_error(exc, where_kind: str = "", **props) -> None:
    """Send the SHAPE of an exception; the full traceback stays local."""
    if _state["errors"] >= _MAX_ERRORS_PER_RUN:
        return
    _state["errors"] += 1
    try:
        track("crash",
              error=type(exc).__name__,
              where=_where(getattr(exc, "__traceback__", None)),
              msg=scrub(exc),
              where_kind=where_kind or "unknown",
              **props)
    except Exception:
        pass


def _install_hooks() -> None:
    """Process- and thread-level excepthooks, chained to the originals."""
    prior = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            if not isinstance(exc, KeyboardInterrupt):
                exc.__traceback__ = tb
                report_error(exc, where_kind="uncaught")
                flush(2.0)
        except Exception:
            pass
        prior(exc_type, exc, tb)

    sys.excepthook = hook

    try:
        prior_thread = threading.excepthook

        def thook(args):
            try:
                if args.exc_value is not None:
                    args.exc_value.__traceback__ = args.exc_traceback
                    report_error(args.exc_value, where_kind="thread")
            except Exception:
                pass
            prior_thread(args)

        threading.excepthook = thook
    except Exception:
        pass


# ── boot marker: catching "the app won't open" ───────────────────────────
# Written as the launcher's first act, deleted once the UI has actually
# talked to the backend. Still present on the NEXT launch means the last
# run died before it could report anything itself.

_BOOT_MARKER = DATA_DIR / "boot-pending"


def _check_boot_marker() -> None:
    try:
        if _BOOT_MARKER.exists():
            prior = ""
            try:
                prior = (_BOOT_MARKER.read_text() or "").strip()[:40]
            except Exception:
                pass
            track("startup_failed", prior_version=prior or "unknown")
    except Exception:
        pass


def mark_boot_pending() -> None:
    try:
        _BOOT_MARKER.write_text(VERSION)
    except Exception:
        pass


def boot_ok() -> None:
    """The UI reached the backend — clear the marker. Called once."""
    try:
        _BOOT_MARKER.unlink(missing_ok=True)
    except Exception:
        pass


# ── start ────────────────────────────────────────────────────────────────

def start() -> None:
    """Idempotent. Safe to call from the launcher AND the app module."""
    with _lock:
        if _state["started"]:
            return
        _state["started"] = True
    _state["boot"] = time.time()
    if not enabled():
        return
    threading.Thread(target=_worker, name="stats", daemon=True).start()
    _install_hooks()
    _check_boot_marker()
    mark_boot_pending()
    track("app_started", frozen=bool(getattr(sys, "frozen", False)))


def session_seconds() -> int:
    return int(time.time() - (_state["boot"] or time.time()))
