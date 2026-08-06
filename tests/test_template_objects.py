"""Tests for flat template playlists — item-level object reuse.

The template feature originally assumed a template playlist is organised
into sections: header items grouping media, with the LLM tagging runsheet
items by section name. Real operators build something simpler: a flat
playlist named e.g. "template SUNDAY" holding one named object per
reusable thing — a Welcome slide, a Countdown video, a preservice loop.
`playlist_to_sections` drops everything before the first header, so a
flat template produced zero sections and nothing ever matched.

These tests pin the item-level path: extract the flat objects, match
runsheet titles against them ("Welcome and Connection Cards" → the
"Welcome" slide), and wrap a hit as a one-item section so the existing
render/expand pipeline (runsheet-coloured header + template item beneath
it) works unchanged.

The PP_ITEMS fixture mirrors the actual JSON ProPresenter 7 returned for
the user's real template playlist — including the trailing space in
"Countdown " and the `duration: 15` that makes it a loop.
"""
import pytest

from propresenterrunsheet.propresenter.templates import (
    playlist_to_objects, resolve_object,
)


def _media(name, uuid="U", target="T", duration=None):
    it = {"id": {"uuid": uuid, "name": name, "index": 0},
          "type": "media", "is_hidden": False, "is_pco": False,
          "target_uuid": target, "destination": "presentation"}
    if duration is not None:
        it["duration"] = duration
    return it


def _presentation(name, uuid="U", pres_uuid="P"):
    return {"id": {"uuid": uuid, "name": name, "index": 0},
            "type": "presentation", "is_hidden": False, "is_pco": False,
            "presentation_info": {"presentation_uuid": pres_uuid,
                                  "arrangement_name": "",
                                  "arrangement_uuid": ""},
            "destination": "presentation"}


def _header(name, uuid="H"):
    return {"id": {"uuid": uuid, "name": name, "index": 0},
            "type": "header", "target_uuid": ""}


# Mirrors the live /v1/playlist/<uuid> response for "template SUNDAY".
PP_ITEMS = [
    _media("Welcome", uuid="C716CBA7", target="3626D448"),
    _presentation("PRESERVICE LOOP", uuid="33C26AE4", pres_uuid="1B718FE3"),
    _media("Countdown ", uuid="9A582EC5", target="3914716D", duration=15),
    _presentation("END SERVICE LOOP", uuid="8FE5ADA6", pres_uuid="7977D6A1"),
    _media("Worship", uuid="CD9C641F", target="BB9780A8"),
    _presentation("1 Samuel 10_6 (ASV)", uuid="65853308", pres_uuid="AAA"),
    _media("Annoucments", uuid="ANN", target="ANNT"),
    _media("Preach - Beyond", uuid="PRE", target="PRET"),
]


# ── playlist_to_objects ──────────────────────────────────────────────────────

def test_flat_playlist_yields_all_objects():
    """The regression: a flat playlist (no headers at all) must surface
    every named object, where playlist_to_sections returns []."""
    objs = playlist_to_objects(PP_ITEMS)
    assert [o["name"] for o in objs] == [
        "Welcome", "PRESERVICE LOOP", "Countdown", "END SERVICE LOOP",
        "Worship", "1 Samuel 10_6 (ASV)", "Annoucments", "Preach - Beyond"]


def test_objects_keep_the_fields_the_expander_needs():
    """The payload builder reproduces items in PP's native shape; asset
    UUID location differs by type and `duration` is what makes a loop."""
    objs = {o["name"]: o for o in playlist_to_objects(PP_ITEMS)}
    countdown = objs["Countdown"]
    assert countdown["type"] == "media"
    assert countdown["target_uuid"] == "3914716D"
    assert countdown["duration"] == 15
    loop = objs["PRESERVICE LOOP"]
    assert loop["type"] == "presentation"
    assert loop["presentation_info"]["presentation_uuid"] == "1B718FE3"


def test_objects_come_from_inside_sections_too():
    """A sectioned template's media are also reusable objects — the
    item-level fallback should work regardless of how the operator
    organises the playlist."""
    items = [_header("Culture"), _media("Culture Bumper", uuid="CB")]
    assert [o["name"] for o in playlist_to_objects(items)] == ["Culture Bumper"]


def test_headers_are_not_objects():
    items = [_header("Culture"), _media("Slide")]
    assert all(o["name"] != "Culture" for o in playlist_to_objects(items))


def test_tolerates_junk_entries():
    items = [{"type": "media"}, {}, _media("Good", uuid="G")]
    objs = playlist_to_objects(items)
    assert [o["name"] for o in objs] == ["Good"]


# ── resolve_object ───────────────────────────────────────────────────────────

@pytest.fixture
def objects():
    return playlist_to_objects(PP_ITEMS)


def test_countdown_matches_despite_extra_words_and_trailing_space(objects):
    """Real pair from the user's runsheet + template: "Countdown - Start
    9:27am" must find "Countdown " (PP preserved a trailing space)."""
    hit = resolve_object("Countdown - Start 9:27am", objects)
    assert hit and hit["name"] == "Countdown"


def test_welcome_matches_inside_a_longer_title(objects):
    hit = resolve_object("Welcome and Connection Cards", objects)
    assert hit and hit["name"] == "Welcome"


def test_multi_word_template_name_requires_all_its_words(objects):
    """"Post Service Media" shares only "service" with "END SERVICE LOOP" —
    a partial overlap must not match, or every runsheet item containing
    "service" would drag a loop video in."""
    assert resolve_object("Post Service Media", objects) is None


def test_multi_word_template_name_matches_when_all_words_present(objects):
    hit = resolve_object("Preach - Beyond - Ps Cathie Green", objects)
    assert hit and hit["name"] == "Preach - Beyond"


def test_more_specific_template_wins():
    """When both "Welcome" and "Welcome Kids" fit, prefer the one that
    explains more of the title."""
    objs = playlist_to_objects([
        _media("Welcome", uuid="W1", target="T1"),
        _media("Welcome Kids", uuid="W2", target="T2"),
    ])
    hit = resolve_object("Welcome Kids Moment", objs)
    assert hit and hit["name"] == "Welcome Kids"


def test_unrelated_title_matches_nothing(objects):
    assert resolve_object("Go Live - Start Online Streaming", objects) is None


def test_empty_inputs_never_match(objects):
    assert resolve_object("", objects) is None
    assert resolve_object("Countdown", []) is None
    # An object whose name normalises to nothing must not match everything.
    weird = playlist_to_objects([_media("—", uuid="X", target="XT")])
    assert resolve_object("Anything at all", weird) is None


# ── operator-taught aliases (2026-08-04 field report) ────────────────────────
# Real case: the youth runsheet says "Youth Arrival + Hangout" and the
# template slide is called "PreLoop Youth". No word overlap, so nothing
# matches — the operator's workaround was renaming the template header to
# copy the runsheet's wording, which breaks as soon as the wording changes.
# An alias teaches the link once: runsheet phrase → template object name.

from propresenterrunsheet.propresenter.templates import resolve_with_aliases


def _objs():
    return playlist_to_objects([
        _media("PreLoop Youth", uuid="PLY", target="PLYT"),
        _media("Welcome", uuid="W", target="WT"),
    ])


def test_alias_links_a_runsheet_phrase_to_a_template_object():
    aliases = [{"match": "Youth Arrival", "template": "PreLoop Youth"}]
    hit = resolve_with_aliases("Youth Arrival + Hangout", _objs(), aliases)
    assert hit and hit["name"] == "PreLoop Youth"


def test_alias_matching_ignores_case_and_surrounding_words():
    aliases = [{"match": "youth arrival", "template": "PreLoop Youth"}]
    hit = resolve_with_aliases("6:00 PM YOUTH ARRIVAL and hangout",
                               _objs(), aliases)
    assert hit and hit["name"] == "PreLoop Youth"


def test_alias_wins_over_the_word_rule():
    """An explicit teaching beats the generic matcher — that's the point."""
    aliases = [{"match": "Welcome", "template": "PreLoop Youth"}]
    hit = resolve_with_aliases("Welcome and Connection Cards", _objs(),
                               aliases)
    assert hit and hit["name"] == "PreLoop Youth"


def test_alias_naming_a_missing_object_falls_back_to_the_word_rule():
    aliases = [{"match": "Welcome", "template": "Does Not Exist"}]
    hit = resolve_with_aliases("Welcome and Connection Cards", _objs(),
                               aliases)
    assert hit and hit["name"] == "Welcome"


def test_no_aliases_behaves_exactly_like_resolve_object():
    for aliases in (None, [], [{"match": "", "template": ""}]):
        hit = resolve_with_aliases("Welcome and Cards", _objs(), aliases)
        assert hit and hit["name"] == "Welcome"
    assert resolve_with_aliases("Nothing here", _objs(), []) is None
