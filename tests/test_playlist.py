"""Unit tests for build_playlist_payload — the pure function that turns the
operator's matched-song list into the JSON body PP's PUT /v1/playlist
endpoint expects.

These tests don't talk to ProPresenter — they exercise the payload shape
and the three branches of the loop (matched song → presentation, unmatched
song → red action-needed header, other types → coloured header)."""

from propresenterrunsheet.propresenter.playlist import (
    ACTION_NEEDED_COLOR,
    TYPE_COLORS,
    _color_dict,
    _color_for_type,
    build_playlist_payload,
)


def test_matched_song_becomes_presentation_item():
    payload = build_playlist_payload([{
        "parsed": {"type": "song", "title": "Build My Life"},
        "match":  {"uuid": "abc-123", "name": "Build My Life", "index": 5},
    }])
    assert len(payload) == 1
    item = payload[0]
    assert item["type"] == "presentation"
    assert item["target_uuid"] == "abc-123"
    assert item["id"] == {"uuid": "abc-123", "name": "Build My Life", "index": 5}
    assert item["is_hidden"] is False
    assert item["is_pco"] is False


def test_unmatched_song_becomes_red_action_header():
    payload = build_playlist_payload([{
        "parsed": {"type": "song", "title": "Mystery Song",
                   "notes": "Pre-service"},
        "match":  None,
    }])
    item = payload[0]
    assert item["type"] == "header"
    assert item["target_uuid"] == ""
    name = item["id"]["name"]
    assert "ACTION NEEDED" in name
    assert "Mystery Song" in name
    assert "Pre-service" in name
    assert item["header_color"] == _color_dict(ACTION_NEEDED_COLOR)


def test_non_song_type_becomes_coloured_header():
    payload = build_playlist_payload([{
        "parsed": {"type": "sermon", "title": "King Jesus",
                   "notes": "10:14 AM"},
        "match":  None,
    }])
    item = payload[0]
    assert item["type"] == "header"
    assert item["id"]["name"] == "King Jesus  —  10:14 AM"
    assert item["header_color"] == _color_for_type("sermon")


def test_scripture_gets_book_emoji_prefix():
    """Scripture items get a 📖 prefix so volunteers spot them in PP and
    open the built-in Bible feature manually for the actual verses."""
    payload = build_playlist_payload([{
        "parsed": {"type": "scripture", "title": "Genesis 1:1-3", "notes": ""},
        "match":  None,
    }])
    assert payload[0]["id"]["name"] == "📖 Genesis 1:1-3"
    assert payload[0]["header_color"] == _color_for_type("scripture")


def test_unknown_type_gets_other_colour():
    payload = build_playlist_payload([{
        "parsed": {"type": "completely_made_up", "title": "X", "notes": ""},
        "match":  None,
    }])
    # Unknown type falls back to TYPE_COLORS["other"]
    assert payload[0]["header_color"] == _color_for_type("other")


def test_handles_empty_matched_list():
    assert build_playlist_payload([]) == []


def test_preserves_runsheet_order():
    """Items in the payload appear in the same order they were given —
    PP's playlist is order-sensitive."""
    matched = [
        {"parsed": {"type": "other", "title": "A"}, "match": None},
        {"parsed": {"type": "song",  "title": "B"},
         "match":  {"uuid": "u-b", "name": "B", "index": 0}},
        {"parsed": {"type": "sermon", "title": "C"}, "match": None},
    ]
    payload = build_playlist_payload(matched)
    assert [it["id"]["name"] for it in payload] == ["A", "B", "C"]


def test_header_colour_helpers():
    """Sanity: the colour helpers produce the dict shape PP expects."""
    d = _color_for_type("song")
    assert set(d.keys()) == {"red", "green", "blue", "alpha"}
    # All values in [0, 1] — PP REST API expects 0-1 floats
    for v in d.values():
        assert 0.0 <= v <= 1.0
    # Alpha defaults to 1.0 across the table
    for rgba in TYPE_COLORS.values():
        assert rgba[3] == 1.0
