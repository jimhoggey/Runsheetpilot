"""The timed-row guard: no timed row can be silently dropped.

On the 14 Aug 2026 youth runsheet the model dropped the three
pre-service rows (Team Setup, Team prayer, Youth Arrival + Hangout) —
the untimed volunteer-roster block under Youth Arrival looks like the
rostering credits the prompt says to ignore, and the model generalised
the skip to the timed rows around it. The prompt already forbade exactly
this, so a stronger prompt is not a fix: with auto-selected free models,
adherence will always wobble.

The guarantee lives here instead, in code. `extract_timed_rows` finds
every "TIME [DURATION] TITLE" line in the raw runsheet text; the parse
route calls `rescue_missing_rows` to synthesize any row the model
dropped and slot it back in time order. Rescue runs BEFORE template
matching, so a rescued "Youth Arrival + Hangout" still links to the
operator's welcome loop like any other item.

The guard is one-directional by design: it only ADDS rows the raw text
proves exist. It never removes or reorders the model's items — splitting
songs out of a section's notes legitimately creates items with no raw
row of their own.
"""

import re

# A line that STARTS with a time of day: "5:00 PM 30 Team Setup & Band
# practice", "9:30 AM Worship", "19:40 10 Message". Anchored to line
# start: times elsewhere ("doors open at 6:00 PM") are prose, not rows.
#
# The time and the rest are parsed in TWO steps deliberately. A single
# regex with a required title backtracks on a bare "6:00 PM" header
# line — the engine gives the meridiem up to the title group and emits
# time="6:00", title="PM", a phantom row. Matching the time first and
# parsing the remainder separately makes that split impossible.
_ROW_TIME = re.compile(
    r"^\s*(?P<time>\d{1,2}[:.]\d{2}(?:\s*[AaPp]\.?[Mm]\.?)?)")
# Remainder: optional bare duration column, then the title. Greedy to
# end-of-line with the title stripped in code — a lazy `\S.*?` against a
# trailing `\s*$` backtracks polynomially on space-heavy lines (CodeQL
# py/polynomial-redos).
_ROW_REST = re.compile(r"^\s*(?:(?P<dur>\d{1,3})\s+)?(?P<title>\S.*)$")


def _norm_time(t: str) -> str:
    """'6:25  pm.' / '6.25PM' → '6:25pm' — one shape for comparisons.

    str methods, not regex: this runs on operator-supplied text, and
    `:+$`-style patterns backtrack polynomially on strings of colons
    (CodeQL py/polynomial-redos).
    """
    t = (t or "").strip().lower().replace(".", ":")
    t = "".join(c for c in t if c.isdigit() or c in ":apm")
    t = t.rstrip(":")
    # '6:25:pm' (from 'PM.' → ':') back to '6:25pm'
    return t.replace(":a", "a").replace(":p", "p")


def _title_tokens(title: str) -> set:
    return {w for w in re.split(r"[^\w]+", (title or "").casefold()) if w}


def extract_timed_rows(raw) -> list:
    """Every timed row in the raw runsheet text, in document order.

    Returns `[{"start_time", "title", "duration_min"}, ...]`. A time with
    no title (the page header's bare service-start time) is not a row.
    """
    rows = []
    for line in (raw or "").splitlines():
        m = _ROW_TIME.match(line)
        if not m:
            continue
        rest = _ROW_REST.match(line[m.end():])
        if not rest or not rest.group("title"):
            continue      # bare service-start time in the page header
        rows.append({
            "start_time":   " ".join(m.group("time").split()).strip(" ."),
            "title":        rest.group("title").strip(),
            "duration_min": int(rest.group("dur") or 0),
        })
    return rows


def _covered(row, items) -> bool:
    """Is this raw-text row represented among the model's items?

    Primarily by time — the model may legitimately reword the title
    ("Games Fun Month Amos & Ethan" → "Games Fun Month: Amos & Ethan").
    Title-overlap is the fallback for items without a start_time (older
    prompt versions, or a model that leaves the field blank): covered if
    at least half the row's title words appear in some item's title.
    """
    want = _norm_time(row["start_time"])
    row_tokens = _title_tokens(row["title"])
    for it in items:
        if not isinstance(it, dict):
            continue
        if want and _norm_time(it.get("start_time") or "") == want:
            return True
        if row_tokens:
            got = _title_tokens(it.get("title") or "")
            if len(row_tokens & got) * 2 >= len(row_tokens):
                return True
    return False


def _minutes(t: str):
    """'6:25pm' → 1105; None when unparseable (sorts nowhere)."""
    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)?$", _norm_time(t))
    if not m:
        return None
    h, mins = int(m.group(1)), int(m.group(2))
    if m.group(3) == "pm" and h != 12:
        h += 12
    elif m.group(3) == "am" and h == 12:
        h = 0
    return h * 60 + mins


def rescue_missing_rows(items, raw):
    """Return `(items, rescued_count)` with every dropped row restored.

    Synthesized rows are typed "other" — the honest default; the type
    clamp and template matching downstream treat them like any parsed
    item, so a rescued row can still pick up its template media. Each is
    inserted before the first existing item that starts later, keeping
    the runsheet's own order.
    """
    items = list(items or [])
    rescued = 0
    for row in extract_timed_rows(raw):
        if _covered(row, items):
            continue
        new = {"type": "other", "title": row["title"],
               "start_time": row["start_time"],
               "duration_min": row["duration_min"], "notes": ""}
        at = len(items)
        row_min = _minutes(row["start_time"])
        if row_min is not None:
            for i, it in enumerate(items):
                have = _minutes((it or {}).get("start_time") or "") \
                    if isinstance(it, dict) else None
                if have is not None and have > row_min:
                    at = i
                    break
        items.insert(at, new)
        rescued += 1
    return items, rescued
