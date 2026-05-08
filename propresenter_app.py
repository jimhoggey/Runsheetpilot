#!/usr/bin/env python3
"""ProPresenter Runsheet Builder — entry point.

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

import sys

# When invoked as a script (`python3 propresenter_app.py`), Python registers
# this file as `__main__` instead of `propresenter_app`. Some submodule
# imports — and tests via the conftest fixtures — reference
# `propresenter_app.<X>`, so without this alias Python would load this file
# a second time and trip a circular-import error during package init.
if __name__ == "__main__" and "propresenter_app" not in sys.modules:
    sys.modules["propresenter_app"] = sys.modules["__main__"]

from flask import Flask, jsonify

# Side-effect imports: `propresenterrunsheet/__init__.py` configures
# logging and loads the propresenter / service_mate / parsing sub-packages
# before we touch them, so anything below this line can safely use them.
import propresenterrunsheet  # noqa: F401

from propresenterrunsheet.logging_setup import log
from propresenterrunsheet.routes import register_blueprints


# ── Flask app ─────────────────────────────────────────────────────────────────
# The app object lives at the project root (next to templates/ + static/) so
# Flask auto-discovers them. PyInstaller's --add-data flags in
# build_mac.sh / build_win.bat ship those folders into the frozen bundle.

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB cap on PDF upload


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "PDF too large (limit 25 MB)."}), 413


@app.errorhandler(Exception)
def _unhandled(e):
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
    build_playlist_payload, find_library_dirs, find_playlist_dir,
    find_pp_root, fuzzy_match, scan_library,
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
    main()
