"""Service Mate background loop.

Runs forever in a daemon thread; ticks every SM_LOOP_INTERVAL_S, renders the
current cue (or standby image) for each configured clock, and pushes it over
HTTP. SHA-1 dedup of the rendered bytes means we only actually push when
something changes (typically once a second when the countdown ticks).

Kept separate from the route handlers so the loop can run independently of
any HTTP request — boot the server, the daemon starts and keeps the clocks
fresh."""

import datetime as _dt
import hashlib
import json
import logging
import threading
import time

from .constants import (
    SM_LOOP_INTERVAL_S, SM_PP_POLL_EVERY_N_TICKS, SM_VERBOSITIES,
    SM_VERBOSITY_DEFAULT,
)
from .geekmagic import _probe_custom, _push_state, _push_to_clock
from .protocol import EndsAtHolder, build_state_payload
from .pp_track import _maybe_advance_from_pp
from .render import _render_cue, _render_standby
from .state import _read_clocks_config, _read_runsheet_state, _write_runsheet_state


log = logging.getLogger("pp_runsheet")


# Module-level so other endpoints (e.g. /api/clocks/<id>/test, /standby) can
# invalidate a clock's "last pushed" entry, forcing the loop to re-push on the
# next tick. Tests reach in to inspect/clear it.
_CLOCKS_LOOP_LAST_PUSHED: dict = {}

# ONE holder for the whole estate, deliberately not one per clock.
#
# `ends_at` is a property of the runsheet item, not of a display. Every clock
# must receive the identical deadline: that is what makes them tick together,
# because each device schedules its repaint at the instant the displayed
# integer changes, derived from that shared value. Per-clock holders would
# resolve at slightly different moments and hand out deadlines a few hundred
# milliseconds apart — and no amount of clock-offset accuracy recovers from
# that.
_ENDS_AT = EndsAtHolder()

# Re-push at least this often even when nothing changed, so a clock that was
# rebooted or dropped off the network rejoins the service without waiting for
# the next cue. Cheap: a small JSON body, versus the ~30KB JPEG the stock path
# sends twice a second.
_HEARTBEAT_S = 10.0


def _state_fingerprint(payload: dict) -> str:
    """Dedup key for a state payload, with `now` EXCLUDED.

    `now` is re-stamped for every push, so including it would make every
    payload unique and defeat dedup entirely — turning a push-on-change
    protocol into 120 pushes a minute.
    """
    body = {k: v for k, v in payload.items() if k != "now"}
    return hashlib.sha1(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


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
    # Resolved ONCE per tick, before the per-clock loop, so every clock in this
    # pass is given the same deadline.
    ends_at = None if standby else _ENDS_AT.resolve(state, _dt.datetime.now())
    if standby:
        _ENDS_AT.reset()

    for clock in cfg["clocks"]:
        ip = (clock.get("ip") or "").strip()
        role = clock.get("role") or clock.get("id") or "screen"
        cid = clock.get("id") or role
        verbosity = (clock.get("verbosity") or SM_VERBOSITY_DEFAULT).lower()
        if verbosity not in SM_VERBOSITIES:
            verbosity = SM_VERBOSITY_DEFAULT
        if not ip:
            continue

        if _probe_custom(ip):
            # Custom firmware: send state, never pictures. The device owns the
            # timer and repaints locally, which is the entire point of the
            # rewrite — so no Pillow render happens for this clock at all.
            layout = "standby" if standby else verbosity
            try:
                payload = build_state_payload(
                    role, layout, state, ends_at,
                    # Stamped HERE, immediately before this clock's POST, and
                    # deliberately not once above the loop. A single `now`
                    # reused across sequential pushes gives the last clock an
                    # offset hundreds of milliseconds worse than the first,
                    # baked in until the next push.
                    _dt.datetime.now(),
                    brightness=cfg.get("brightness"))
            except Exception:
                log.exception(f"payload build failed for role={role}")
                continue

            h = _state_fingerprint(payload)
            prev = _CLOCKS_LOOP_LAST_PUSHED.get(cid) or ("", 0.0)
            if prev[0] == h and (time.time() - prev[1]) < _HEARTBEAT_S:
                continue
            if _push_state(ip, payload):
                _CLOCKS_LOOP_LAST_PUSHED[cid] = (h, time.time())
            continue

        # Stock firmware: unchanged image push. This branch must keep working —
        # it is what every clock not yet reflashed depends on.
        try:
            jpg = (_render_standby(role) if standby
                   else _render_cue(role, state, verbosity=verbosity))
        except Exception:
            log.exception(f"render failed for role={role}")
            continue
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
