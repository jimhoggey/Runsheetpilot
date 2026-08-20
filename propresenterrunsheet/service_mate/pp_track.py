"""ProPresenter auto-tracking for Service Mate.

Polls ProPresenter every couple of seconds and updates the runsheet state's
`current_index` / `pp_remaining_seconds` so the clocks follow whatever the
operator is actually doing in PP. Three signals, in priority order:

  1. Active playlist section (primary). Operators commonly insert media
     items between our header rows — clicking one of those should still
     advance the Service Mate to the section that media belongs to. So we
     walk backward through the playlist from the active item to find the
     most recent header, strip its decorations, and fuzzy-match against
     the runsheet titles.

     Two robustness measures here, both tuned for the click-once-but-it's-
     wrong bug:
       * UUID-first resolution. PP can briefly return a stale or off-by-one
         `playlist_item.id.index` right after an operator clicks; the
         `playlist_item.id.uuid` it returns alongside is more reliable, so
         we look up the active item's position in the cached playlist by
         UUID and only fall back to the numeric index when UUID matching
         fails (empty UUID, or test fixtures that don't carry UUIDs).
       * Stickiness. Even with UUID matching we require the same target
         section to come back from two consecutive polls before we commit
         to advancing — that way a single bad poll right after a click
         can't drag the clocks to the wrong section. Adds up to one
         poll-interval (~2 s) of lag, which is acceptable for service ops.
  2. Running [RB] timer. Provides accurate remaining time when one of our
     timers is actually running.
  3. Active presentation name match. Fallback for non-playlist usage —
     SUPPRESSED whenever PP has an active playlist (signal 1 owns that
     case, and falling through to name-fuzzy here was a major source of
     "advanced to the wrong section" bugs).

Manual cue clicks set a 10-second override window during which all three
are suppressed so the operator's input isn't fought."""

import datetime as _dt
import difflib
import logging
import re
import time

from ..propresenter.library import _norm


log = logging.getLogger("pp_runsheet")


# Cached playlist contents — refreshed when the active playlist UUID changes
# or when the cache is older than this many seconds.
_PP_PLAYLIST_CACHE = {"uuid": None, "items": [], "fetched_at": 0.0}
_PP_PLAYLIST_CACHE_TTL_S = 60

# Stickiness for signal 1: require this many consecutive polls returning the
# same target section before we actually advance `current_index`. Defends
# against single bad polls right after a click (PP can briefly report stale
# or off-by-one indices before its internal state settles).
SECTION_ADVANCE_MIN_POLLS = 2
_PENDING_SECTION_TARGET: dict = {"index": None, "count": 0}

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


def _resolve_active_position(plist: list, active_uuid: str, active_index) -> int:
    """Find the position of the currently-active playlist item in the cached
    items list. Prefers UUID match — more robust than the numeric index, which
    PP has been observed to briefly report stale or off-by-one right after an
    operator clicks. Falls back to the numeric index when UUID matching fails
    (empty UUID, or items that don't carry UUIDs in some test setups).
    Returns -1 when neither matches."""
    if active_uuid:
        for i, item in enumerate(plist):
            if (item.get("id") or {}).get("uuid") == active_uuid:
                return i
    if isinstance(active_index, int) and 0 <= active_index < len(plist):
        return active_index
    return -1


def _pp_active_section_probe(state: dict, base: str) -> dict:
    """Probe PP for the currently-active playlist item and resolve it back to
    a runsheet section index. Returns a dict with:

        section_idx: int | None
            Best-matching runsheet index. None when no confident match.
        has_active_playlist: bool
            Whether PP currently has an active playlist at all. The caller
            uses this to suppress signal 3 (the name-fuzzy fallback) — if
            a playlist is active, signal 1 owns that case and we should
            not fall through, even when section_idx itself is None."""
    import requests as req
    result = {"section_idx": None, "has_active_playlist": False}
    try:
        r = req.get(f"{base}/v1/playlist/active", timeout=2)
        if not r.ok:
            return result
        data = r.json() or {}
        pres = data.get("presentation")
        if not isinstance(pres, dict):
            return result
        playlist = pres.get("playlist") or {}
        playlist_item = pres.get("playlist_item") or {}
        playlist_uuid = playlist.get("uuid")
        if not playlist_uuid:
            return result
        # We have an active playlist — record that even if we end up unable
        # to resolve a confident section match below.
        result["has_active_playlist"] = True
        playlist_item_id = playlist_item.get("id") or {}
        active_uuid = playlist_item_id.get("uuid") or ""
        active_index = playlist_item_id.get("index")
        if not active_uuid and active_index is None:
            return result
        plist = _pp_get_playlist_items(base, playlist_uuid)
        pos = _resolve_active_position(plist, active_uuid, active_index)
        if pos < 0:
            # Cache might be stale (operator just rearranged items in PP) —
            # force a refresh and retry once.
            _PP_PLAYLIST_CACHE["uuid"] = None
            plist = _pp_get_playlist_items(base, playlist_uuid)
            pos = _resolve_active_position(plist, active_uuid, active_index)
            if pos < 0:
                return result
        # Find the section header for the active item: itself if it's a
        # header, else walk backward.
        header_name = ""
        if plist[pos].get("type") == "header":
            header_name = (plist[pos].get("id") or {}).get("name", "")
        else:
            for i in range(pos - 1, -1, -1):
                if plist[i].get("type") == "header":
                    header_name = (plist[i].get("id") or {}).get("name", "")
                    break
        clean = _clean_header_name(header_name)
        if not clean:
            return result
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
            result["section_idx"] = best_i
    except Exception:
        log.debug("PP /v1/playlist/active fetch failed", exc_info=True)
    return result


def _pp_active_section_index(state: dict, base: str):
    """Map the currently-active PP playlist item back to a runsheet item
    index. Thin wrapper around `_pp_active_section_probe` — preserved as a
    standalone callable for the test suite. Returns the section index, or
    None when no confident match (or when PP isn't reachable / has no
    active playlist)."""
    return _pp_active_section_probe(state, base)["section_idx"]


def _maybe_advance_from_pp(state: dict) -> dict:
    """Auto-track ProPresenter so the Service Mate follows whatever the
    operator is doing. See the module docstring for the three-signal scheme.
    Manual cue clicks set a 10-second override window during which all three
    signals are suppressed so the operator's input isn't fought."""
    import requests as req
    from ..propresenter.timers import _RB_TIMER_PREFIX
    from ..settings import load_settings

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
    try:
        from ..propresenter.net import pp_base
        base = pp_base(host, port)
    except Exception:
        # Bad saved host: auto-track just has nothing to track this
        # tick. The daemon must never die over a settings value.
        return state
    items = state.get("items") or []
    if not items:
        return state

    # 1) Active playlist section (primary signal) — gated by stickiness so a
    # single bad poll right after a click can't drag us to the wrong section.
    probe = _pp_active_section_probe(state, base)
    section_idx = probe["section_idx"]
    has_active_playlist = probe["has_active_playlist"]

    if section_idx is not None:
        if section_idx == state.get("current_index"):
            # Already there — clear any pending target.
            _PENDING_SECTION_TARGET["index"] = None
            _PENDING_SECTION_TARGET["count"] = 0
        elif _PENDING_SECTION_TARGET["index"] == section_idx:
            _PENDING_SECTION_TARGET["count"] += 1
            if _PENDING_SECTION_TARGET["count"] >= SECTION_ADVANCE_MIN_POLLS:
                state["current_index"] = section_idx
                state["current_started_at"] = _dt.datetime.now().isoformat()
                state["pp_source"] = "section"
                _PENDING_SECTION_TARGET["index"] = None
                _PENDING_SECTION_TARGET["count"] = 0
        else:
            # New target — start counting; don't advance yet.
            _PENDING_SECTION_TARGET["index"] = section_idx
            _PENDING_SECTION_TARGET["count"] = 1

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

    # Suppress signal 3 (name-fuzzy fallback) whenever PP has an active
    # playlist. Signal 1 owns the playlist case — letting signal 3 fire here
    # was a major source of "advanced to the wrong section" bugs (it would
    # match the active presentation's name against a runsheet item below the
    # current section while signal 1 was briefly returning None).
    if section_idx is not None or timer_running or has_active_playlist:
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
