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
  2. Running [RB] timer. Provides accurate remaining time when one of our
     timers is actually running.
  3. Active presentation name match. Fallback for non-playlist usage.

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
    operator is doing. See the module docstring for the three-signal scheme.
    Manual cue clicks set a 10-second override window during which all three
    signals are suppressed so the operator's input isn't fought."""
    import requests as req
    # Lazy import: load_settings still lives in propresenter_app (phase 4 of
    # the refactor moves it). Importing it at module top would cycle through
    # the package's __init__.py.
    from propresenter_app import load_settings
    from ..propresenter.timers import _RB_TIMER_PREFIX

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
