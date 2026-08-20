"""Does the countdown this runsheet asks for actually exist yet?

Service Visuals (the sibling app) renders countdown timers and clocks
into `~/Documents/Service Visuals` with a filename that carries its own
meaning — set in that app's render/encoder.py:

    timer_5m00s_ring_20260819-125231.mp4   -> 5m00s countdown, ring style
    clock_1900-00_600s_ring_2026….mp4      -> 7pm clock, 600s runtime

So when a runsheet row asks for a countdown, the length it needs can be
compared against what has actually been rendered.

The question that makes this useful rather than noisy is *which* of four
states an item is in, because only one of them needs the operator:

  in_template   the item already matched the ProPresenter template — a
                Sunday service reuses the same Countdown every week, so
                there is nothing to make and nothing to say.
  in_pp         a timer of that length is in ProPresenter's Media bin.
  rendered      Service Visuals has made one, but ProPresenter hasn't
                got it — the drag the media panel already handles.
  missing       nobody has made it. THIS is the one worth surfacing,
                and it is worth surfacing on Wednesday rather than at
                6:25pm on Friday.

Deliberately keyword-triggered, not every timed row. Runsheet Pilot
already creates a ProPresenter countdown timer for every item with a
duration; suggesting a rendered video for all of them would bury the
one row that actually said it needed one.
"""

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("pp_runsheet")

# The row has to ASK. "Countdown", "5 min countdown needed", "clock
# timer" — not merely "this item has a duration".
_ASKS_FOR_TIMER = re.compile(
    r"\b(count\s?down|timer|clock)\b", re.IGNORECASE)

# "5 minute", "5min", "5m", "5 mins" — an explicit length in the words,
# which beats the duration column when the two disagree, because someone
# wrote it on purpose.
# One unambiguous class for the gap, not `\s*(?:-|\s)?\s*` — that gives
# the engine three ways to match a single space and backtracks
# polynomially on a run of them (CodeQL py/polynomial-redos). Longest
# alternative first so "minutes" isn't consumed as "min".
_EXPLICIT_MINUTES = re.compile(
    r"(\d{1,3})[\s-]{0,4}(?:minutes|minute|mins|min|m)\b", re.IGNORECASE)

# Service Visuals' own output convention.
_TIMER_FILE = re.compile(
    r"^timer_(\d{1,3})m(\d{2})s_([A-Za-z0-9\-]+?)(_ms)?_\d{8}-\d{6}",
    re.IGNORECASE)


def exports_dir() -> str:
    """Where Service Visuals puts finished renders.

    Mirrors that app's `_default_exports_dir`, including its
    SERVICE_VISUALS_EXPORTS override, so the two stay in step instead of
    this one hardcoding a path that moves.
    """
    override = os.environ.get("SERVICE_VISUALS_EXPORTS")
    if override:
        return os.path.abspath(override)
    return str(Path.home() / "Documents" / "Service Visuals")


def wants_timer(item) -> bool:
    """True when the row explicitly asks for a countdown or clock."""
    if not isinstance(item, dict):
        return False
    text = f"{item.get('title') or ''} {item.get('notes') or ''}"
    return bool(_ASKS_FOR_TIMER.search(text))


def wanted_minutes(item):
    """How long the countdown should be, or None if the row never says.

    Words win over the duration column: a row reading "Countdown — 5 min
    countdown needed" with 10 in the duration column is describing a
    5-minute countdown inside a 10-minute slot.
    """
    if not isinstance(item, dict):
        return None
    text = f"{item.get('title') or ''} {item.get('notes') or ''}"
    m = _EXPLICIT_MINUTES.search(text)
    if m:
        minutes = int(m.group(1))
        return minutes if 0 < minutes <= 240 else None
    try:
        minutes = int(item.get("duration_min") or 0)
    except (TypeError, ValueError):
        return None
    return minutes if 0 < minutes <= 240 else None


def scan_timers(directory=None) -> list:
    """Rendered countdowns, as `[{"name","path","minutes","seconds","style"}]`.

    Only files matching Service Visuals' timer convention; anything else
    in that folder (spinners, scoreboards, backgrounds) is ignored.
    """
    root = Path(directory or exports_dir())
    out = []
    try:
        entries = list(root.iterdir())
    except Exception:
        return out                      # folder absent: nothing rendered
    for p in entries:
        try:
            if not p.is_file():
                continue
            m = _TIMER_FILE.match(p.name)
            if not m:
                continue
            out.append({"name": p.name, "path": str(p),
                        "minutes": int(m.group(1)), "seconds": int(m.group(2)),
                        "style": m.group(3).lower()})
        except Exception:
            continue
    out.sort(key=lambda t: (t["minutes"], t["seconds"]))
    return out


def timer_status(item, timers=None, bin_names=None) -> dict:
    """What, if anything, this row needs before it can show a countdown.

    Returns None when the row didn't ask for one. Otherwise a dict with
    `state` in {in_template, in_pp, rendered, missing} and `minutes`.

    Exact match only. A 3-minute timer in a 5-minute slot is a wrong
    answer that looks like a right one, so a near miss is reported
    separately as `nearest` and never as a match.
    """
    if not wants_timer(item):
        return None
    minutes = wanted_minutes(item)
    if minutes is None:
        return None

    result = {"minutes": minutes, "title": item.get("title") or "",
              "state": "missing", "path": "", "name": "", "nearest": None}

    # 1. The template already covers it — the Sunday case. The item is
    #    linked to the operator's reusable Countdown, so it is going into
    #    the playlist whatever this module thinks.
    if item.get("library_match"):
        result["state"] = "in_template"
        return result

    exact = [t for t in (timers or [])
             if t["minutes"] == minutes and t["seconds"] == 0]

    # 2. ProPresenter already has a timer of that length in its Media
    #    bin (matched on the stem, which is how PP names bin items).
    have = {str(n).strip().casefold() for n in (bin_names or [])}
    for t in exact:
        if Path(t["name"]).stem.strip().casefold() in have:
            result.update(state="in_pp", name=t["name"], path=t["path"])
            return result

    # 3. Rendered but not imported — the drag the media panel handles.
    if exact:
        result.update(state="rendered", name=exact[0]["name"],
                      path=exact[0]["path"])
        return result

    # 4. Nothing of that length. Mention the closest so the operator can
    #    judge, but never call it a match.
    if timers:
        near = min(timers, key=lambda t: abs(t["minutes"] * 60 + t["seconds"]
                                             - minutes * 60))
        result["nearest"] = {"name": near["name"], "minutes": near["minutes"],
                             "seconds": near["seconds"]}
    return result


def review(items, timers=None, bin_names=None, directory=None) -> list:
    """Every row that asked for a countdown and hasn't got one.

    `in_template` and `in_pp` rows are dropped: they are already handled,
    and a panel that lists solved problems trains people to ignore it.
    """
    timers = scan_timers(directory) if timers is None else timers
    out = []
    for item in items or []:
        status = timer_status(item, timers, bin_names)
        if status and status["state"] in ("rendered", "missing"):
            out.append(status)
    return out
