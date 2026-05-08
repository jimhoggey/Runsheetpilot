"""Duration-based countdown timers in ProPresenter.

Every timer we create has the `[RB] ` prefix so we can safely delete only
our own timers on the next run without touching the user's. Timers are
duration-based (not count-down-to-time) because runsheets are typically
uploaded days before the service — the time-of-day in the runsheet is
the *planned* slot, not when the operator will actually start the timer.

The runsheet's time-of-day is included in the timer name as a hint so
the operator can quickly find the right timer at the right moment in
PP's timer panel."""

import logging


log = logging.getLogger("pp_runsheet")


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

    See the module docstring for why timers are duration-based.

    Returns {created, deleted, no_duration, total_items, errors, timer_names}.
    `timer_names` maps runsheet item index → exact timer name created, used
    by the Service Mate auto-track to identify the running [RB] timer."""
    import requests as req
    # Lazy import: phase 4 of the refactor will move these into the package
    # (as parsing/duration.py); for now they live in propresenter_app.py and
    # importing at module top would cycle through the package's __init__.
    from propresenter_app import _extract_duration_min, _extract_time_str

    deleted = _delete_existing_rb_timers(base)

    created, no_duration, total_items, errors = 0, 0, 0, []
    # Map runsheet-item index (0-based) → exact timer name we created for it.
    # Used by the Service Mate auto-track to identify the running [RB] timer.
    timer_names: dict = {}
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
                # idx is 1-based above; record under 0-based item index.
                timer_names[idx - 1] = timer_name
                log.info(f"Created timer: {timer_name}")
            else:
                errors.append(f"{timer_name} → HTTP {r.status_code}")
                log.warning(f"Timer create failed: {timer_name} → "
                            f"{r.status_code} {r.text[:120]}")
        except Exception as e:
            errors.append(f"{timer_name} → {type(e).__name__}")
            log.exception(f"Timer create exception for {timer_name}")
    return {
        "created":      created,
        "deleted":      deleted,
        "no_duration":  no_duration,
        "total_items":  total_items,
        "errors":       errors,
        "timer_names":  timer_names,
    }
