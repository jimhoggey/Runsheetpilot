"""Tests for what the ProPresenter section header actually says.

The header is read at a glance, mid-service, by the person running
screens. It answers two questions: what is coming up, and when. It is
NOT the place for the runsheet's bullet points.

The bug this fixes: `_coloured_header_for` used to append the whole
`notes` blob to the title, so a header came out as

    MC Welcome: Ollie & Elliot  —  6:25 PM
    - Invite Night Coming up
    - Summit 2026 promo screen
    Fun Month Ending

That is four lines of prose where the operator needed six words. Note
that `title` and `notes` were always separate fields — this was never a
model problem, it was the renderer gluing them together.

The shape now is `Title — 6:25 PM (5 min)`. The people's names ride along
inside the title ("MC Welcome: Ollie & Elliot"), which is where the model
already puts them.

`start_time` is a real field on the parsed item now. Before it existed
the time survived only because the model happened to lead `notes` with
it — which is why every older test fixture in this suite has
`"notes": "10:14 AM"`. Those parses still exist in the wild (Service Mate
state files, a runsheet parsed before an update), so a leading time in
notes is still honoured as a fallback.
"""
from propresenterrunsheet.propresenter.playlist import (
    build_playlist_payload, header_label,
)


def _header(parsed):
    return build_playlist_payload([{"parsed": parsed, "match": None}])[0]["id"]["name"]


# ── the shape ────────────────────────────────────────────────────────────

def test_header_is_title_time_and_duration():
    assert _header({"type": "mc_on_stage",
                    "title": "MC Welcome: Ollie & Elliot",
                    "start_time": "6:25 PM",
                    "duration_min": 5}) == "MC Welcome: Ollie & Elliot — 6:25 PM (5 min)"


def test_the_notes_blob_never_reaches_the_header():
    """The actual reported bug, verbatim from the operator's runsheet."""
    label = _header({
        "type": "mc_on_stage",
        "title": "MC Welcome: Ollie & Elliot",
        "start_time": "6:25 PM",
        "duration_min": 5,
        "notes": "- Invite Night Coming up\n- Summit 2026 promo screen\n"
                 "Fun Month Ending",
    })
    assert label == "MC Welcome: Ollie & Elliot — 6:25 PM (5 min)"
    assert "Invite Night" not in label
    assert "\n" not in label


def test_peoples_names_in_the_title_are_preserved():
    """The screens person identifies a slot by who is on stage."""
    assert "Amos & Ethan" in _header({
        "type": "other", "title": "Games Fun Month Amos & Ethan",
        "start_time": "6:30 PM", "duration_min": 40})


# ── missing pieces degrade cleanly ───────────────────────────────────────

def test_no_duration_drops_the_bracket():
    assert _header({"type": "other", "title": "Connect Groups",
                    "start_time": "7:15 PM"}) == "Connect Groups — 7:15 PM"


def test_zero_duration_is_treated_as_absent():
    """duration_min is 0 when the runsheet didn't say — not '0 min'."""
    assert _header({"type": "other", "title": "Connect Groups",
                    "start_time": "7:15 PM",
                    "duration_min": 0}) == "Connect Groups — 7:15 PM"


def test_no_time_leaves_just_the_title_and_duration():
    assert _header({"type": "other", "title": "Close & Hangout",
                    "duration_min": 20}) == "Close & Hangout (20 min)"


def test_no_time_and_no_duration_is_just_the_title():
    assert _header({"type": "other", "title": "Close & Hangout"}) == "Close & Hangout"


def test_a_title_only_item_has_no_trailing_separator():
    """A dangling ' — ' looks like a truncation bug to the operator."""
    label = _header({"type": "other", "title": "Close & Hangout",
                     "start_time": "", "duration_min": 0})
    assert not label.endswith("—")
    assert not label.endswith("(")


# ── backward compatibility with parses made before start_time existed ────

def test_a_leading_time_in_notes_is_used_when_start_time_is_missing():
    assert _header({"type": "sermon", "title": "King Jesus",
                    "notes": "10:14 AM"}) == "King Jesus — 10:14 AM"


def test_only_the_leading_time_is_taken_not_the_rest_of_the_notes():
    assert _header({
        "type": "mc_on_stage", "title": "MC Welcome",
        "notes": "6:25 PM\n- Invite Night Coming up\n- Summit 2026 promo",
    }) == "MC Welcome — 6:25 PM"


def test_notes_without_a_leading_time_contribute_nothing():
    """'Pre-service' is prose, not a time — it must not land in the header."""
    assert _header({"type": "other", "title": "Countdown",
                    "notes": "Pre-service"}) == "Countdown"


def test_start_time_wins_over_a_time_in_notes():
    assert _header({"type": "sermon", "title": "King Jesus",
                    "start_time": "10:14 AM",
                    "notes": "9:00 AM was the old slot"}) == "King Jesus — 10:14 AM"


def test_24_hour_times_are_recognised_in_notes():
    assert _header({"type": "sermon", "title": "Message",
                    "notes": "19:40 start"}) == "Message — 19:40"


# ── it composes with the existing header rules ───────────────────────────

def test_scripture_keeps_its_book_emoji():
    assert _header({"type": "scripture", "title": "Genesis 1:1-3",
                    "start_time": "10:20 AM"}) == "📖 Genesis 1:1-3 — 10:20 AM"


def test_unmatched_song_keeps_its_action_needed_warning():
    """The red ACTION NEEDED header is how an operator spots a song PP
    couldn't find — shortening headers must not hide it."""
    label = _header({"type": "song", "title": "Mystery Song",
                     "start_time": "7:05 PM"})
    assert "ACTION NEEDED" in label
    assert "Mystery Song" in label


def test_unmatched_song_header_is_short_too():
    """This branch used to build its own label and append the raw notes,
    so it kept the bug after the main path was fixed. Both paths must go
    through header_label."""
    label = _header({
        "type": "song", "title": "Mystery Song", "start_time": "7:05 PM",
        "duration_min": 4,
        "notes": "Key of G\nBand to start quiet\nLeader: Sam",
    })
    assert label == "⚠ ACTION NEEDED — Mystery Song — 7:05 PM (4 min)"
    assert "Key of G" not in label


def test_header_label_is_reusable_on_its_own():
    """Exposed so the section-expansion path and the fallback path build
    the same string — that divergence is what caused this bug."""
    assert header_label({"title": "Culture Moment: Ollie & Elliot",
                         "start_time": "7:10 PM",
                         "duration_min": 5}) == \
        "Culture Moment: Ollie & Elliot — 7:10 PM (5 min)"


def test_whitespace_around_fields_is_tidied():
    assert _header({"type": "other", "title": "  Connect Groups  ",
                    "start_time": " 7:15 PM "}) == "Connect Groups — 7:15 PM"
