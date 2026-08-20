"""Tests for "does the countdown this runsheet asks for exist yet?".

The distinction that makes this useful is the Sunday case: that service
reuses a Countdown that already lives in the ProPresenter template, so
the app must say NOTHING about it. Suggesting the operator go and render
a timer they have used every week for a year is exactly how a panel
teaches people to ignore it.

So `in_template` and `in_pp` are silence, and only `rendered` (made but
not imported) and `missing` (nobody made it) reach the operator.
"""
import pytest

from propresenterrunsheet import service_visuals as sv


TIMERS = [
    {"name": "timer_1m00s_classic_20260818-171833.mp4",
     "path": "/x/timer_1m00s_classic_20260818-171833.mp4",
     "minutes": 1, "seconds": 0, "style": "classic"},
    {"name": "timer_3m00s_ring_20260819-104359.mp4",
     "path": "/x/timer_3m00s_ring_20260819-104359.mp4",
     "minutes": 3, "seconds": 0, "style": "ring"},
    {"name": "timer_5m00s_ring_20260819-125231.mp4",
     "path": "/x/timer_5m00s_ring_20260819-125231.mp4",
     "minutes": 5, "seconds": 0, "style": "ring"},
]


def item(title="", notes="", duration=0, matched=None):
    return {"title": title, "notes": notes, "duration_min": duration,
            "library_match": matched}


# ── the row has to ASK ───────────────────────────────────────────────────

@pytest.mark.parametrize("title,notes", [
    ("Countdown", ""),
    ("Pre-service", "5 minute countdown needed"),
    ("Welcome", "count down before we start"),
    ("Offering", "clock timer please"),
])
def test_rows_that_ask_for_a_timer_are_recognised(title, notes):
    assert sv.wants_timer(item(title, notes)) is True


@pytest.mark.parametrize("title,notes", [
    ("Games Fun Month Amos & Ethan", "Fill prizes into the golden Esky"),
    ("Connect Groups", ""),
    ("MC Welcome: Ollie & Elliot", "Invite Night coming up"),
])
def test_ordinary_rows_are_left_alone(title, notes):
    """Every timed row gets a ProPresenter countdown timer already;
    offering to render a video for all of them would bury the one row
    that actually asked."""
    assert sv.wants_timer(item(title, notes, duration=40)) is False


# ── how long ─────────────────────────────────────────────────────────────

def test_the_duration_column_is_used_by_default():
    assert sv.wanted_minutes(item("Countdown", duration=5)) == 5


@pytest.mark.parametrize("notes,expected", [
    ("5 minute countdown needed", 5),
    ("10min countdown", 10),
    ("need a 3 min countdown", 3),
    ("2m countdown", 2),
])
def test_a_length_written_in_words_wins(notes, expected):
    """"Countdown — 5 min countdown needed" in a 10-minute slot is a
    5-minute countdown, not a 10-minute one. Someone typed it on
    purpose."""
    assert sv.wanted_minutes(item("Countdown", notes, duration=10)) == expected


def test_no_length_anywhere_means_no_opinion():
    assert sv.wanted_minutes(item("Countdown", duration=0)) is None


def test_an_absurd_length_is_ignored():
    assert sv.wanted_minutes(item("Countdown", "9999 minute countdown")) is None


# ── the four states ──────────────────────────────────────────────────────

def test_a_template_match_says_nothing():
    """THE SUNDAY CASE. The service reuses a Countdown that already
    lives in the template, so it is already going into the playlist and
    there is nothing to tell anyone."""
    st = sv.timer_status(
        item("Countdown", duration=5, matched={"header": {}, "items": [{}]}),
        TIMERS, [])
    assert st["state"] == "in_template"
    assert sv.review([item("Countdown", duration=5,
                           matched={"header": {}, "items": [{}]})],
                     TIMERS, []) == []


def test_already_in_propresenters_media_bin_says_nothing():
    st = sv.timer_status(item("Countdown", duration=5), TIMERS,
                         ["timer_5m00s_ring_20260819-125231"])
    assert st["state"] == "in_pp"
    assert sv.review([item("Countdown", duration=5)], TIMERS,
                     ["timer_5m00s_ring_20260819-125231"]) == []


def test_rendered_but_not_imported_is_worth_saying():
    st = sv.timer_status(item("Countdown", duration=5), TIMERS, [])
    assert st["state"] == "rendered"
    assert st["name"] == "timer_5m00s_ring_20260819-125231.mp4"


def test_nothing_of_that_length_asks_the_operator_to_make_one():
    st = sv.timer_status(item("Countdown", duration=7), TIMERS, [])
    assert st["state"] == "missing"
    assert st["minutes"] == 7


# ── exact match only ─────────────────────────────────────────────────────

def test_a_near_miss_is_never_treated_as_a_match():
    """A 3-minute timer in a 5-minute slot is a wrong answer that looks
    like a right one."""
    st = sv.timer_status(item("Countdown", duration=4), TIMERS, [])
    assert st["state"] == "missing"
    assert st["nearest"]["minutes"] in (3, 5)


def test_the_nearest_is_offered_as_context_not_as_the_answer():
    st = sv.timer_status(item("Countdown", duration=6), TIMERS, [])
    assert st["state"] == "missing"
    assert st["nearest"]["minutes"] == 5


def test_seconds_must_match_too(tmp_path):
    odd = [{"name": "timer_5m30s_ring_20260819-125231.mp4", "path": "/x/a",
            "minutes": 5, "seconds": 30, "style": "ring"}]
    assert sv.timer_status(item("Countdown", duration=5),
                           odd, [])["state"] == "missing"


# ── scanning a real folder ───────────────────────────────────────────────

def test_scan_reads_service_visuals_naming(tmp_path):
    for name in ("timer_5m00s_ring_20260819-125231.mp4",
                 "timer_1m00s_classic_20260818-171833.mp4",
                 "timer_10m00s_ring_ms_20260820-090000.mp4"):
        (tmp_path / name).write_bytes(b"x")
    found = sv.scan_timers(str(tmp_path))
    assert [t["minutes"] for t in found] == [1, 5, 10]
    assert found[0]["style"] == "classic"


def test_scan_ignores_everything_that_is_not_a_timer(tmp_path):
    """That folder also holds spinners, scoreboards and backgrounds."""
    for name in ("Spinner Youth.mp4", "board_Points_20260815-140008.png",
                 "motionbg_waves_30s_20260101-000000.mp4", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert sv.scan_timers(str(tmp_path)) == []


def test_scan_survives_a_missing_folder():
    assert sv.scan_timers("/definitely/not/here") == []


def test_exports_dir_honours_the_service_visuals_override(monkeypatch, tmp_path):
    """Mirrors that app's own env override, so the two stay in step.

    Compared against os.path.abspath rather than a literal: the override
    is absolutised, and on Windows a bare "/tmp/x" becomes "D:\\tmp\\x".
    """
    import os
    target = str(tmp_path / "sv-elsewhere")
    monkeypatch.setenv("SERVICE_VISUALS_EXPORTS", target)
    assert sv.exports_dir() == os.path.abspath(target)


def test_exports_dir_defaults_under_documents(monkeypatch):
    monkeypatch.delenv("SERVICE_VISUALS_EXPORTS", raising=False)
    got = sv.exports_dir()
    assert got.endswith("Service Visuals")
    assert "Documents" in got


# ── review: only what needs a human ──────────────────────────────────────

def test_review_returns_only_actionable_rows():
    items = [
        item("Countdown", duration=5, matched={"header": {}}),   # template
        item("Countdown", duration=3),                            # rendered
        item("Countdown", duration=7),                            # missing
        item("Connect Groups", duration=45),                      # never asked
    ]
    out = sv.review(items, TIMERS, [])
    assert [r["state"] for r in out] == ["rendered", "missing"]
    assert [r["minutes"] for r in out] == [3, 7]
