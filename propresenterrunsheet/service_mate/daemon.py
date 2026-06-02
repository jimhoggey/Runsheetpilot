"""Service Mate background loop.

Runs forever in a daemon thread; ticks every SM_LOOP_INTERVAL_S, renders the
current cue (or standby image) for each configured clock, and pushes it over
HTTP. SHA-1 dedup of the rendered bytes means we only actually push when
something changes (typically once a second when the countdown ticks).

Kept separate from the route handlers so the loop can run independently of
any HTTP request — boot the server, the daemon starts and keeps the clocks
fresh."""

import logging
import threading
import time

from .constants import (
    SM_LOOP_INTERVAL_S, SM_PP_POLL_EVERY_N_TICKS, SM_VERBOSITIES,
    SM_VERBOSITY_DEFAULT,
)
from .geekmagic import _push_to_clock
from .pp_track import _maybe_advance_from_pp
from .render import _render_cue, _render_standby
from .state import _read_clocks_config, _read_runsheet_state, _write_runsheet_state


log = logging.getLogger("pp_runsheet")


# Module-level so other endpoints (e.g. /api/clocks/<id>/test, /standby) can
# invalidate a clock's "last pushed" entry, forcing the loop to re-push on the
# next tick. Tests reach in to inspect/clear it.
_CLOCKS_LOOP_LAST_PUSHED: dict = {}


def _clocks_loop_tick(tick: int) -> None:
    """One pass of the background loop. `tick` increments each call; we use it
    to throttle ProPresenter polling so the loop can render at 500 ms while PP
    only gets hit every SM_PP_POLL_EVERY_N_TICKS ticks."""
    state = _read_runsheet_state() or {}
    cfg = _read_clocks_config()
    if not cfg.get("enabled") or not cfg.get("clocks"):
        return
    # Paid add-on gate. Stamp the trial on first active tick (covers existing
    # users who already had the switch on before this feature shipped — they
    # get a fresh 14-day trial from first run), then stop pushing once the
    # trial has expired and no licence is present.
    from ..licensing import service_mate_allowed, start_trial_if_needed
    start_trial_if_needed()
    if not service_mate_allowed():
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
