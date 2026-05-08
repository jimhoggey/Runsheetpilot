"""Service Mate — GeekMagic SmallTV-Ultra clock integration.

Each "Service Mate" is a GeekMagic clock running stock firmware on the LAN.
It has no app-level "show this text" endpoint — only image upload + a
"display this image" toggle. So we render a 240×240 JPEG with the current
runsheet item, countdown, and a role-aware cue, then push it.

Submodule responsibilities:
  constants.py   — Display dimensions, font sizes, role accents, rule tables
  state.py       — runsheet_state.json + clocks.json I/O, item cue helpers
  render.py      — Pillow-based renderers (cue compact/detailed, test card,
                   standby) and drawing helpers
  geekmagic.py   — HTTP client for the device (upload, theme, brightness,
                   probe), with workarounds for v9.0.39 firmware quirks
  pp_track.py    — ProPresenter auto-tracking — playlist section / running
                   timer / active presentation, in priority order
  daemon.py      — Background thread that ticks every 500 ms, renders the
                   right image for each clock, and pushes via geekmagic
"""

# Re-export the symbols the rest of the app + the tests depend on. Every
# entry here is referenced as `propresenter_app.<name>` from somewhere
# (test_helpers.py, test_render.py, test_routes.py, conftest.py, or
# propresenter_app.py's own bootstrap).
from .constants import (
    ROLE_ACCENT,
    SCREEN_CUES,
    SOUND_CUES,
    LIGHTS_CUES,
    ROLE_CUE_TABLES,
    SM_FILENAME,
    SM_FONTS,
    SM_H,
    SM_JPEG_QUALITY,
    SM_LOOP_INTERVAL_S,
    SM_PP_POLL_EVERY_N_TICKS,
    SM_TESTCARD_FILENAME,
    SM_ULTRA_IMAGE_THEME,
    SM_VERBOSITIES,
    SM_VERBOSITY_DEFAULT,
    SM_W,
)
from .daemon import (
    _CLOCKS_LOOP_LAST_PUSHED,
    _clocks_loop,
    _clocks_loop_tick,
    start_clocks_loop,
)
from .geekmagic import (
    _CLOCK_THEME_SET,
    _probe_clock,
    _push_to_clock,
    _set_clock_brightness,
)
from .pp_track import (
    _clean_header_name,
    _maybe_advance_from_pp,
    _parse_pp_time,
    _pp_active_section_index,
    _pp_get_playlist_items,
)
from .render import (
    _compute_remaining_seconds,
    _format_mmss,
    _render_cue,
    _render_cue_compact,
    _render_cue_detailed,
    _render_standby,
    _render_test_card,
    _resolve_current,
    _sm_font,
    _text_width,
)
from .state import (
    CLOCKS_CONFIG_FILE,
    RUNSHEET_STATE_FILE,
    _cue_for,
    _default_clocks_config,
    _ensure_item_cues,
    _next_visible_item,
    _read_clocks_config,
    _read_runsheet_state,
    _write_clocks_config,
    _write_runsheet_state,
)
