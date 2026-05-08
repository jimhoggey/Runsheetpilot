"""Time-of-day + duration parsing for runsheet items.

Used both by the timer-creation step (which stamps PP timers with a
"9:30 AM — 20 min" hint in the name) and by the Service Mate countdown
(which falls back to `duration_min` when no PP timer is currently
running)."""

import re


# Matches "9:24 AM", "9:24am", "12:30 PM", etc. — the AM/PM marker is required
# so we don't accidentally match e.g. a chord "G2:4" or a note like "for 35:00".
# We still extract time-of-day for display purposes (it goes in the timer name
# so the operator can find the right timer at the right moment), but timers
# themselves are duration-based since the runsheet is uploaded days ahead.
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")

# Matches durations like "20 min", "20min", "20 minutes", "20m", "(20 min)".
_DURATION_RE = re.compile(r"(\d+)\s*(?:min(?:ute)?s?|m\b)", re.IGNORECASE)


def _extract_time_str(text: str) -> str:
    """Return the time-of-day as a display string (e.g. '9:30 AM') or ''."""
    if not text:
        return ""
    m = _TIME_RE.search(text)
    if not m:
        return ""
    h, mn, p = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if not (1 <= h <= 12 and 0 <= mn <= 59):
        return ""
    return f"{h}:{mn:02d} {p}"


def _extract_duration_min(parsed_item: dict) -> int:
    """Find the duration in whole minutes for a parsed runsheet item.

    Order of precedence:
      1. Explicit `duration_min` field returned by the AI (preferred).
      2. Regex match on the `notes` field ("20 min", "30 minutes", etc.).
      3. Regex match on the `title` field as a last resort.
    Returns 0 if no duration found / 0-duration item — caller should skip."""
    raw = parsed_item.get("duration_min")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    for field in ("notes", "title"):
        m = _DURATION_RE.search(parsed_item.get(field, "") or "")
        if m:
            n = int(m.group(1))
            if 0 < n < 24 * 60:  # sanity cap: under 24 h
                return n
    return 0
