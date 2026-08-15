"""Tests for the timed-row guard — the deterministic "no dropped rows" net.

On the 14 Aug 2026 youth runsheet the model silently dropped the three
pre-service rows:

    5:00 PM 30 Team Setup & Band practice
    5:30 PM 30 Team prayer + Meeting
    6:00 PM 25 Youth Arrival + Hangout

The prompt already forbade exactly this ("Do NOT skip a row because…
its notes list volunteer names") — the untimed roster block under Youth
Arrival looks like the rostering credits the prompt says to ignore, and
the model generalised the skip to the timed rows around it. A rule the
model already breaks cannot be the fix, and the free-model roulette
means adherence will always wobble.

So the guarantee moves out of the prompt and into code: every timed row
in the raw text must be represented in the parsed items, and any row the
model dropped is synthesized back in — title, time, duration, typed
"other" — in time order. Rescue runs BEFORE template matching, so a
rescued "Youth Arrival + Hangout" still links to the operator's welcome
loop like any other item.
"""
from propresenterrunsheet.parsing.timed_rows import (
    extract_timed_rows, rescue_missing_rows,
)


RAW = """Friday, 14 August, 2026
6:00 PM
Youth Service : Community Night
5:00 PM 30 Team Setup & Band practice
5:30 PM 30 Team prayer + Meeting
6:00 PM 25 Youth Arrival + Hangout
Welcome & Carpark: Rachel & Sarah Chani
Help Set up Food Talk to Drew, snack bar inside
6:25 PM 5 MC Welcome: Ollie & Elliot
- Invite Night Coming up
6:30 PM 40 Games Fun Month Amos & Ethan
7:10 PM 5 Culture Moment: Ollie & Elliot
7:15 PM 45 Connect Groups
8:00 PM 0 Close & Hangout
"""


def _item(title, start="", dur=0):
    return {"type": "other", "title": title, "start_time": start,
            "duration_min": dur, "notes": ""}


# ── extract_timed_rows ───────────────────────────────────────────────────

def test_every_timed_row_is_found():
    rows = extract_timed_rows(RAW)
    assert [(r["start_time"], r["title"]) for r in rows] == [
        ("5:00 PM", "Team Setup & Band practice"),
        ("5:30 PM", "Team prayer + Meeting"),
        ("6:00 PM", "Youth Arrival + Hangout"),
        ("6:25 PM", "MC Welcome: Ollie & Elliot"),
        ("6:30 PM", "Games Fun Month Amos & Ethan"),
        ("7:10 PM", "Culture Moment: Ollie & Elliot"),
        ("7:15 PM", "Connect Groups"),
        ("8:00 PM", "Close & Hangout"),
    ]


def test_durations_are_extracted():
    rows = extract_timed_rows(RAW)
    assert rows[0]["duration_min"] == 30
    assert rows[-1]["duration_min"] == 0


def test_a_bare_time_line_is_not_a_row():
    """The page header carries the service start ('6:00 PM' alone).
    Time with no title must not become a phantom item."""
    titles = [r["title"] for r in extract_timed_rows(RAW)]
    assert "" not in titles
    # …and the real 6:00 PM row is still found exactly once.
    assert sum(1 for r in extract_timed_rows(RAW)
               if r["start_time"] == "6:00 PM") == 1


def test_a_time_with_title_but_no_duration_is_a_row():
    rows = extract_timed_rows("9:30 AM Worship and Ministry\n")
    assert rows == [{"start_time": "9:30 AM",
                     "title": "Worship and Ministry", "duration_min": 0}]


def test_untimed_note_lines_are_never_rows():
    rows = extract_timed_rows(
        "- Invite Night Coming up\nFill Prizes into the golden Esky\n")
    assert rows == []


def test_24h_times_are_recognised():
    rows = extract_timed_rows("19:40 10 Message\n")
    assert rows == [{"start_time": "19:40", "title": "Message",
                     "duration_min": 10}]


def test_empty_text_gives_no_rows():
    assert extract_timed_rows("") == []
    assert extract_timed_rows(None) == []


# ── rescue_missing_rows ──────────────────────────────────────────────────

def test_the_actual_14_aug_failure_is_rescued():
    """The model returned only the five later rows; the three
    pre-service rows come back, in time order, at the front."""
    model_items = [
        _item("MC Welcome: Ollie & Elliot", "6:25 PM", 5),
        _item("Games Fun Month Amos & Ethan", "6:30 PM", 40),
        _item("Culture Moment: Ollie & Elliot", "7:10 PM", 5),
        _item("Connect Groups", "7:15 PM", 45),
        _item("Close & Hangout", "8:00 PM", 0),
    ]
    items, rescued = rescue_missing_rows(model_items, RAW)
    assert rescued == 3
    assert [i["title"] for i in items[:3]] == [
        "Team Setup & Band practice",
        "Team prayer + Meeting",
        "Youth Arrival + Hangout",
    ]
    assert items[2]["start_time"] == "6:00 PM"
    assert items[2]["duration_min"] == 25
    assert items[2]["type"] == "other"


def test_nothing_missing_changes_nothing():
    model_items = [
        _item("Team Setup & Band practice", "5:00 PM", 30),
        _item("Team prayer + Meeting", "5:30 PM", 30),
        _item("Youth Arrival + Hangout", "6:00 PM", 25),
        _item("MC Welcome: Ollie & Elliot", "6:25 PM", 5),
        _item("Games Fun Month Amos & Ethan", "6:30 PM", 40),
        _item("Culture Moment: Ollie & Elliot", "7:10 PM", 5),
        _item("Connect Groups", "7:15 PM", 45),
        _item("Close & Hangout", "8:00 PM", 0),
    ]
    items, rescued = rescue_missing_rows(list(model_items), RAW)
    assert rescued == 0
    assert items == model_items


def test_rows_are_matched_by_time_even_if_the_model_reworded_the_title():
    """'Games Fun Month: Amos & Ethan' (colon added per the title rule)
    must still count as covering the 6:30 row."""
    model_items = [_item("Games Fun Month: Amos & Ethan", "6:30 PM", 40)]
    _items, rescued = rescue_missing_rows(
        model_items, "6:30 PM 40 Games Fun Month Amos & Ethan\n")
    assert rescued == 0


def test_a_period_after_pm_still_matches():
    """The model writes '6:30 PM.' sometimes; the raw text says
    '6:30 PM'. Punctuation must not defeat the match."""
    model_items = [_item("Games", "6:30 PM.", 40)]
    _items, rescued = rescue_missing_rows(
        model_items, "6:30 PM 40 Games Fun Month\n")
    assert rescued == 0


def test_a_row_without_start_time_is_covered_by_title_overlap():
    """Older prompts had no start_time field — a model that returns the
    right rows with empty times must not trigger duplicates."""
    model_items = [_item("Team Setup & Band practice", "", 30)]
    _items, rescued = rescue_missing_rows(
        model_items, "5:00 PM 30 Team Setup & Band practice\n")
    assert rescued == 0


def test_split_song_items_do_not_confuse_the_guard():
    """The model legitimately splits 'Praise and Worship' + songs from
    its notes into several items; extra items without raw-text rows are
    left alone."""
    raw = "7:05 PM 25 Praise and Worship Songs: Great Are You Lord, Build My Life\n"
    model_items = [
        _item("Praise and Worship", "7:05 PM", 25),
        {"type": "song", "title": "Great Are You Lord", "start_time": "",
         "duration_min": 0, "notes": ""},
        {"type": "song", "title": "Build My Life", "start_time": "",
         "duration_min": 0, "notes": ""},
    ]
    items, rescued = rescue_missing_rows(list(model_items), raw)
    assert rescued == 0
    assert len(items) == 3


def test_rescued_rows_interleave_in_time_order():
    """A dropped middle row lands between its neighbours, not at the end."""
    raw = ("6:25 PM 5 MC Welcome\n"
           "6:30 PM 40 Games\n"
           "7:10 PM 5 Culture Moment\n")
    model_items = [_item("MC Welcome", "6:25 PM", 5),
                   _item("Culture Moment", "7:10 PM", 5)]
    items, rescued = rescue_missing_rows(model_items, raw)
    assert rescued == 1
    assert [i["title"] for i in items] == ["MC Welcome", "Games",
                                           "Culture Moment"]


# ── the real 14 Aug 2026 PDF, when present ───────────────────────────────

def test_the_real_runsheet_pdf_end_to_end():
    """The operator's actual file, through the real pdfplumber path.
    Untracked (.gitignore covers *.pdf), so this runs on machines that
    have the sample and skips cleanly elsewhere — CI included."""
    import pytest
    from pathlib import Path
    pdf = Path(__file__).resolve().parent.parent / "14_Aug_2026.pdf"
    if not pdf.exists():
        pytest.skip("sample runsheet not present")
    from propresenterrunsheet.parsing.pdf import extract_pdf_text
    rows = extract_timed_rows(extract_pdf_text(str(pdf)))
    assert [r["title"] for r in rows] == [
        "Team Setup & Band practice",
        "Team prayer + Meeting",
        "Youth Arrival + Hangout",
        "MC Welcome: Ollie & Elliot",
        "Games Fun Month Amos & Ethan",
        "Culture Moment: Ollie & Elliot",
        "Connect Groups",
        "Close & Hangout",
    ]
