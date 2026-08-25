"""The masthead — the lines above the first timed row.

A runsheet says what it IS at the top, and says what HAPPENS underneath.
Only the first of those answers "which template playlist is this for", so
that is the only part the template picker gets to see.

Shape, from the 21 Aug 2026 tech runsheet:

    Friday, 21 August, 2026
    6:00 PM                                  ← bare service-start time
    Youth Service : EVANGELISM 101           ← names the service
    5:00 PM 30 Team Setup + Band practice    ← first row; masthead ends

The bare "6:00 PM" on line 2 must not end the masthead early — it is a
time with no title, which `extract_timed_rows` already refuses to treat
as a row. Same test, one definition, so the two can't drift.
"""
from propresenterrunsheet.parsing.timed_rows import service_header
from propresenterrunsheet.propresenter.templates import auto_detect_template_uuid


# Both fixtures follow the shape of real runsheets off this operator's
# machine: date line, bare service-start time, then the line that names
# the service. Volunteer names are replaced with roles — they are real
# people, and nothing here tests them.
REAL = """Friday, 21 August, 2026
6:00 PM
Youth Service : EVANGELISM 101
5:00 PM 30 Team Setup + Band practice
5:30 PM 30 Team prayer + Meeting
6:00 PM 20 Youth Arrival + Hangout Sign in: two leaders
Set up basketball in the car park, set up Xbox. THIS IS YOUTH
6:20 PM 5 MC Welcome - MC team
"""

# The other service on the same machine, and the one that started this:
# its masthead says Young Adults, and NOTHING in the document says youth.
YOUNG_ADULTS = """Saturday, 29 August, 2026
3:30 PM
Young Adults Service - BIG DAY IN
2:00 PM 75 Teams Arrive and Set Up
3:15 PM 15 Whole Team Pre-Service Prayer
3:30 PM 30 YA Arrival and Hang Out
4:00 PM 5 Video
4:17 PM 3 MC Welcome - MC team
"""


def test_masthead_stops_at_the_first_real_row():
    assert service_header(REAL).splitlines() == [
        "Friday, 21 August, 2026",
        "6:00 PM",
        "Youth Service : EVANGELISM 101",
    ]


def test_a_bare_service_start_time_does_not_end_the_masthead():
    """Line 2 is a time with no title. If it counted as a row the
    masthead would stop one line short — exactly before the line that
    names the service, which is the only line we are here for."""
    assert "Youth Service" in service_header(REAL)


def test_the_body_never_reaches_the_template_picker():
    """The point of the whole exercise. "THIS IS YOUTH" sits in a setup
    note further down; on a young adults runsheet with the same note, it
    used to be enough to score a confident hit on the youth library."""
    head = service_header(REAL)
    assert "THIS IS YOUTH" not in head
    assert "Team Setup" not in head


def test_no_masthead_means_no_guess():
    """A runsheet that opens straight into rows never named its service.
    "" so the caller hints on the filename alone rather than falling back
    to reading the body — guessing from the body is what we removed."""
    assert service_header("5:00 PM 30 Team Setup\n5:30 PM 30 Prayer") == ""
    assert service_header("") == ""
    assert service_header(None) == ""


def test_a_cover_page_cannot_hand_over_the_whole_document():
    """No timed rows at all (a mangled OCR read) must still be bounded."""
    head = service_header("\n".join(f"Line {i} of preamble" for i in range(80)))
    assert len(head.splitlines()) <= 12
    assert len(head) <= 400 + 40


# ── The two runsheets, end to end through the picker ─────────────────────────

YOUTH_ONLY = [{"uuid": "u-youth", "name": "Youth Service - Library"}]


def _hint(raw, filename="21_Aug_2026 Tech.pdf"):
    return " ".join(filter(None, [filename, service_header(raw)]))


def test_the_youth_runsheet_still_finds_the_youth_template():
    assert auto_detect_template_uuid(
        YOUTH_ONLY, hint=_hint(REAL)) == "u-youth"


def test_the_reported_failure_the_real_young_adults_runsheet():
    """29 Aug 2026, the runsheet that started this. On a machine whose
    only template is the youth library, the answer is no template.

    Note what is NOT the fix here: this runsheet never says "youth", so
    it scored zero under the old hint too. The bug was purely the
    fallback that turned a zero score into "use the first template
    anyway" — see tests/test_template_decline.py."""
    assert "youth" not in YOUNG_ADULTS.lower()
    assert auto_detect_template_uuid(
        YOUTH_ONLY, hint=_hint(YOUNG_ADULTS, "29_Aug_2026 Final 1.pdf")) is None


def test_the_two_services_route_to_their_own_templates():
    """With both templates built, each runsheet finds its own — the
    reason Auto exists, and the case a blanket decline would break."""
    both = YOUTH_ONLY + [{"uuid": "u-ya", "name": "Young Adults - Library"}]
    assert auto_detect_template_uuid(both, hint=_hint(REAL)) == "u-youth"
    assert auto_detect_template_uuid(
        both, hint=_hint(YOUNG_ADULTS, "29_Aug_2026 Final 1.pdf")) == "u-ya"


def test_body_wording_cannot_outvote_the_masthead():
    """Isolation, not realism: this document's body is wall-to-wall youth
    wording ("THIS IS YOUTH", "Youth Arrival") while its masthead says
    Young Adults. A real runsheet would not look like this — the point is
    that the picker no longer reads far enough to care."""
    mixed = REAL.replace("Youth Service : EVANGELISM 101",
                         "Young Adults : EVANGELISM 101")
    assert "THIS IS YOUTH" in mixed
    assert auto_detect_template_uuid(YOUTH_ONLY, hint=_hint(mixed)) is None
