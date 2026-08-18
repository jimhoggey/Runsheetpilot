#!/usr/bin/env python3
"""Runsheet Pilot — entry point.

The actual app lives in the `propresenterrunsheet/` package. This file
stays as the entry point so the launchers (launch_mac.sh / run.bat) and
the PyInstaller specs in build_mac.sh / build_win.bat don't need
updating; it also keeps the Flask app object next to the
templates/ + static/ folders so Flask finds them automatically (both
in dev and inside a frozen .app/.exe).

Where the code lives now (after the four-phase refactor):
  templates/                          — index.html
  static/                             — app.css, app.js
  propresenterrunsheet/
    config.py                         — VERSION, APP_NAME, DATA_DIR, paths
    logging_setup.py                  — setup_logging(), `log`
    settings.py                       — load_settings, save_settings
    server.py                         — main(), _serve, port discovery
    parsing/                          — pdf, ai prompt, time/duration regex
    propresenter/                     — PP filesystem + REST API + payload
    service_mate/                     — GeekMagic clock subsystem
    routes/                           — Flask blueprints, one per topic

Common feature touch-points:
  - new API endpoint    → propresenterrunsheet/routes/<topic>.py
                          + JS caller in static/app.js
  - new UI panel        → templates/index.html + static/app.js
  - new settings field  → propresenterrunsheet/settings.py
                          + UI in templates/index.html
  - new runsheet type   → DEFAULT_PROMPT in parsing/ai.py,
                          TYPE_COLORS in propresenter/playlist.py,
                          tagClass()/CSS in static/app.{js,css},
                          *_CUES rule tables in service_mate/constants.py
  - clock layout tweak  → service_mate/render.py
"""

import os
import sys

# When invoked as a script (`python3 propresenter_app.py`), Python registers
# this file as `__main__` instead of `propresenter_app`. Some submodule
# imports — and tests via the conftest fixtures — reference
# `propresenter_app.<X>`, so without this alias Python would load this file
# a second time and trip a circular-import error during package init.
if __name__ == "__main__" and "propresenter_app" not in sys.modules:
    sys.modules["propresenter_app"] = sys.modules["__main__"]

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

# Side-effect imports: `propresenterrunsheet/__init__.py` configures
# logging and loads the propresenter / service_mate / parsing sub-packages
# before we touch them, so anything below this line can safely use them.
import propresenterrunsheet  # noqa: F401

from propresenterrunsheet.logging_setup import log
from propresenterrunsheet.routes import register_blueprints


# ── Flask app ─────────────────────────────────────────────────────────────────
# In a normal (source) run, Flask auto-discovers templates/ + static/ next to
# this file. In the FROZEN bundle we point Flask explicitly at the folders
# PyInstaller extracts into sys._MEIPASS. Flask's auto-detection (via
# __main__.__file__) is unreliable in a --onefile --windowed exe — especially
# when the process is launched by the self-updater rather than by double-click
# — which surfaced as "TemplateNotFound: index.html" right after an update.
# Explicit _MEIPASS paths are the documented, bulletproof fix.
if getattr(sys, "frozen", False):
    _bundle = sys._MEIPASS  # PyInstaller sets this on the frozen bundle
    app = Flask(__name__,
                template_folder=os.path.join(_bundle, "templates"),
                static_folder=os.path.join(_bundle, "static"))
else:
    app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB cap on PDF upload
# Auto-reload templates when their files change on disk. Flask defaults
# this to app.debug (False here), so without this any markup edit would
# only show up after restarting the whole app. The mtime check per
# render adds microseconds — meaningless for a local app.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Report any unhandled exception raised inside a request. Flask turns
# these into a 500 for the browser; without this hook they would never
# be seen by anyone. The handler only ever sends the exception TYPE,
# scrubbed message and frame basenames — see propresenterrunsheet/stats.py.
try:
    from flask import got_request_exception as _got_request_exception
    from propresenterrunsheet import stats as _stats

    def _report_request_exception(sender, exception, **_extra):
        try:
            from flask import request as _rq
            _stats.report_error(exception, where_kind="request",
                                route=(_rq.endpoint or "unknown"))
        except Exception:
            pass

    _got_request_exception.connect(_report_request_exception, app)
except Exception:
    pass


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "PDF too large (limit 25 MB)."}), 413


@app.errorhandler(Exception)
def _unhandled(e):
    # Flask routes werkzeug HTTPExceptions (404, 405, …) through this
    # catch-all too, because it's registered on the base Exception class.
    # Let those keep their own status code instead of being logged as a
    # crash and masked as a 500 — otherwise the browser's automatic
    # /favicon.ico probe fills the log with scary tracebacks on every
    # launch and every missing URL "fails" as a 500.
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled exception in request")
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# Wire every route on the package onto the app
register_blueprints(app)


# ── Re-export shim for tests + legacy callers ─────────────────────────────────
# `tests/conftest.py` and `tests/test_*.py` poke a couple of dozen helpers as
# `app_module.<name>`. Re-exporting them here lets the tests stay unchanged
# even though the source of truth has moved into the package.

from propresenterrunsheet.config import (  # noqa: E402, F401
    APP_NAME, DATA_DIR, DEFAULT_PORT, LOG_FILE, PORT_RANGE,
    SETTINGS_FILE, UPLOAD_FOLDER, VERSION,
)
from propresenterrunsheet.parsing import (  # noqa: E402, F401
    DEFAULT_PROMPT, _DURATION_RE, _TIME_RE,
    _extract_duration_min, _extract_time_str, extract_pdf_text,
)
from propresenterrunsheet.propresenter import (  # noqa: E402, F401
    ACTION_NEEDED_COLOR, TYPE_COLORS,
    _RB_TIMER_PREFIX, _UUID_RE,
    _color_dict, _color_for_type, _create_pp_timers,
    _delete_existing_rb_timers, _norm, _pp_candidates, _uuid_from_binary,
    auto_detect_template_uuid, build_playlist_payload, fetch_pp_playlist_items,
    fetch_pp_playlists, find_library_dirs, find_playlist_dir, find_pp_root,
    fuzzy_match, playlist_to_sections, resolve_library_name, resolve_section,
    scan_library,
)
from propresenterrunsheet.service_mate import (  # noqa: E402, F401
    CLOCKS_CONFIG_FILE, LIGHTS_CUES, ROLE_ACCENT, ROLE_CUE_TABLES,
    RUNSHEET_STATE_FILE, SCREEN_CUES, SM_FILENAME, SM_FONTS, SM_H,
    SM_JPEG_QUALITY, SM_LOOP_INTERVAL_S, SM_PP_POLL_EVERY_N_TICKS,
    SM_TESTCARD_FILENAME, SM_ULTRA_IMAGE_THEME, SM_VERBOSITIES,
    SM_VERBOSITY_DEFAULT, SM_W, SOUND_CUES,
    _CLOCKS_LOOP_LAST_PUSHED, _CLOCK_THEME_SET,
    _clean_header_name, _clocks_loop, _clocks_loop_tick,
    _compute_remaining_seconds, _cue_for, _default_clocks_config,
    _ensure_item_cues, _format_mmss, _maybe_advance_from_pp,
    _next_visible_item, _parse_pp_time, _pp_active_section_index,
    _pp_get_playlist_items, _probe_clock, _push_to_clock,
    _read_clocks_config, _read_runsheet_state, _render_cue,
    _render_cue_compact, _render_cue_detailed, _render_standby,
    _render_test_card, _resolve_current, _set_clock_brightness,
    _sm_font, _text_width, _write_clocks_config, _write_runsheet_state,
    start_clocks_loop,
)
from propresenterrunsheet.settings import (  # noqa: E402, F401
    _default_settings, load_settings, save_settings,
)
from propresenterrunsheet.server import main  # noqa: E402


if __name__ == "__main__":
    # Pass `app` explicitly so `main()` doesn't have to re-import this
    # module via `from propresenter_app import app`. In a PyInstaller
    # bundle that re-import has occasionally surfaced as "routes 404
    # even though they registered" — Python ends up holding two copies
    # of the propresenter_app module (one as __main__ from the bootloader,
    # one freshly loaded via the import). Threading the live `app` object
    # straight through closes that whole class of bug.
    main(app)
