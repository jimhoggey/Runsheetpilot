"""Service Mate JSON state protocol — the payload Runsheet Pilot pushes to
clocks running the custom ESP32 firmware.

This replaces the JPEG push for those clocks. The device owns the timer and
counts down locally; we send state, not pictures.

The wire contract is implemented twice — the C++ parser lives in the firmware
repo (`servicemate-firmware-esp32`, `src/net/api_server.cpp`) and the builder
lives here. Nothing in either build catches a disagreement: a renamed field
just goes quiet and a clock shows the wrong thing mid-service. Change one side
and you must change the other.

Two rules in here are not stylistic. Both exist because breaking them
reintroduces the countdown stutter that motivated the whole rewrite:

1. `now` is stamped by the CALLER, immediately before each individual POST.
   Reusing one timestamp across three sequential pushes gives the third clock
   an offset ~300ms worse than the first, baked in until the next push.
2. `ends_at` is HELD per item by EndsAtHolder, not recomputed per push. See
   the class docstring.
"""

import datetime as _dt

from .constants import SM_VERBOSITY_DEFAULT
from .state import _cue_for, _next_visible_item


# How far a freshly computed deadline must move before we believe it is a real
# timer change rather than sampling phase. pp_remaining_seconds is a whole
# second sampled every 2s, so it can legitimately imply a deadline up to ~1s
# either side of the held one; an operator resetting a timer moves it by far
# more than 2s.
ENDS_AT_TOLERANCE_S = 2.0


def _iso(t):
    """Naive local ISO-8601 with milliseconds and no timezone suffix.

    The firmware parses exactly this shape. It carries no timezone and needs
    none: `now` and `ends_at` are only ever subtracted from each other, so any
    single consistent frame works and no DST handling belongs anywhere in the
    path. A tz-aware value here would append an offset the parser rejects.
    """
    if t is None:
        return None
    if t.tzinfo is not None:
        t = t.replace(tzinfo=None)
    return t.isoformat(timespec="milliseconds")


def _duration_seconds(item):
    """Whole seconds for an item, or None when it has no usable duration."""
    if not item:
        return None
    from ..parsing.duration import _extract_duration_min
    mins = _extract_duration_min(item)
    return int(mins) * 60 if mins and mins > 0 else None


class EndsAtHolder:
    """Keeps `ends_at` stable for the life of a runsheet item.

    `_compute_remaining_seconds` prefers `state["pp_remaining_seconds"]`, which
    `pp_track` sets from ProPresenter's own timer display — a WHOLE-SECOND
    value, sampled every 2s. Computing `ends_at = now + remaining` on each push
    therefore lands on a different absolute instant every time, wobbling by up
    to a second as the sample phase drifts.

    The firmware cannot absorb that: its offset smoothing smooths `now`, not
    `ends_at`. A moving deadline makes every clock jump backward and forward —
    exactly the stutter this project exists to remove, reintroduced from the
    Python side.

    So the deadline is computed once when the item becomes current and then
    held, re-derived only when the item changes or when a fresh value differs
    by more than ENDS_AT_TOLERANCE_S. A real change is applied at once rather
    than smoothed: if the operator resets a ProPresenter timer, the clocks
    should obey immediately.
    """

    def __init__(self):
        self._key = None
        self._ends_at = None

    @staticmethod
    def _item_key(state):
        # current_started_at changes whenever pp_track advances the item, so it
        # distinguishes "same index, new run" from "same item still running".
        return (state.get("current_index"), state.get("current_started_at"))

    def reset(self):
        self._key = None
        self._ends_at = None

    def resolve(self, state, now):
        """The deadline to send, or None when there is no countdown."""
        from .render import _compute_remaining_seconds
        remaining = _compute_remaining_seconds(state)
        if remaining is None:
            self._key = None
            self._ends_at = None
            return None

        candidate = now + _dt.timedelta(seconds=int(remaining))
        key = self._item_key(state)

        if key != self._key or self._ends_at is None:
            self._key = key
            self._ends_at = candidate
        elif abs((candidate - self._ends_at).total_seconds()) > ENDS_AT_TOLERANCE_S:
            # Real timer change, not sampling noise. Obey it at once.
            self._ends_at = candidate
        return self._ends_at


def build_state_payload(role, verbosity, state, ends_at, now):
    """Build the JSON body for POST /api/state.

    `now` must be stamped by the caller immediately before the POST for THIS
    clock — see the module docstring. `ends_at` comes from an EndsAtHolder.

    Optional fields (`starts_at`, `next_duration_s`) are OMITTED rather than
    sent as null when unknown. The firmware treats absent as "hide the
    element"; null on a field it expects to be a string is a parse failure.
    """
    items = state.get("items") or []
    idx = int(state.get("current_index") or 0)
    idx = max(0, min(idx, len(items) - 1)) if items else 0
    cur = items[idx] if items else {}
    nxt = _next_visible_item(items, idx) if items else None

    layout = (verbosity or SM_VERBOSITY_DEFAULT).lower()

    payload = {
        "now":        _iso(now),
        "ends_at":    _iso(ends_at),
        "role":       role,
        "layout":     layout,
        "title":      (cur.get("title") or "").strip(),
        "cue":        _cue_for(role, cur),
        "type":       (cur.get("type") or "other").lower(),
        "next_title": (nxt.get("title") or "").strip() if nxt else "",
        "next_type":  (nxt.get("type") or "other").lower() if nxt else "",
        "next_cue":   _cue_for(role, nxt) if nxt else "",
        "notes":      (cur.get("notes") or "").strip(),
    }

    started_at = state.get("current_started_at")
    if started_at:
        try:
            payload["starts_at"] = _iso(_dt.datetime.fromisoformat(started_at))
        except (TypeError, ValueError):
            pass          # unparseable: omit, so the clock hides the bar

    next_secs = _duration_seconds(nxt)
    if next_secs:
        payload["next_duration_s"] = next_secs

    return payload
