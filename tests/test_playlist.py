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


# ─── library_match: the parse step links runsheet items to reusable
# presentations the operator already has in their PP library. Once linked,
# build_playlist_payload should emit a real `presentation` entry (not a
# header) regardless of the item's parsed `type`. ─────────────────────────


def test_library_match_on_non_song_becomes_presentation_item():
    """The whole point of the feature — runsheet has 'Culture Moment'
    (type=announcement), parse stage linked it to the library's 'Culture'
    presentation, so the playlist gets a real presentation entry with the
    library UUID instead of a coloured header the volunteer has to fill in."""
    payload = build_playlist_payload([{
        "parsed": {
            "type": "announcement",
            "title": "Culture Moment - Generosity - Ps Melissa",
            "notes": "10:00 AM",
            "library_match": {"uuid": "lib-culture-uuid",
                              "name": "Culture",
                              "index": 7},
        },
        "match": None,
    }])
    item = payload[0]
    assert item["type"] == "presentation"
    assert item["target_uuid"] == "lib-culture-uuid"
    assert item["id"] == {"uuid": "lib-culture-uuid", "name": "Culture",
                          "index": 7}


def test_library_match_preempts_song_fuzzy_match():
    """If both library_match (LLM-resolved) AND a song fuzzy match are set,
    library_match wins — the LLM saw the full library at parse time, so it
    has more context than a one-shot title fuzz."""
    payload = build_playlist_payload([{
        "parsed": {
            "type": "song",
            "title": "Build My Life",
            "library_match": {"uuid": "lib-bml",
                              "name": "Build My Life (Acoustic)",
                              "index": 12},
        },
        # A different fuzzy hit that should be ignored
        "match": {"uuid": "fuzz-bml", "name": "Build My Life", "index": 3},
    }])
    item = payload[0]
    assert item["target_uuid"] == "lib-bml"
    assert item["id"]["name"] == "Build My Life (Acoustic)"


def test_library_match_with_no_uuid_falls_through_to_song_fuzzy_match():
    """Defensive: if library_match is a dict but missing uuid (malformed,
    empty string), we fall back to the song fuzzy match instead of emitting
    a broken presentation entry."""
    payload = build_playlist_payload([{
        "parsed": {
            "type": "song",
            "title": "Build My Life",
            "library_match": {"uuid": "", "name": "junk", "index": 0},
        },
        "match": {"uuid": "fuzz-bml", "name": "Build My Life", "index": 3},
    }])
    item = payload[0]
    assert item["target_uuid"] == "fuzz-bml"


def test_library_match_none_keeps_non_song_as_header():
    """Non-song items without a library_match stay as coloured headers —
    unchanged behaviour, no regression for items the LLM correctly left
    untagged (e.g. 'Welcome and Connection Cards' when no matching
    presentation exists in the library)."""
    payload = build_playlist_payload([{
        "parsed": {"type": "announcement", "title": "Welcome", "notes": "",
                   "library_match": None},
        "match":  None,
    }])
    assert payload[0]["type"] == "header"
    assert payload[0]["id"]["name"] == "Welcome"


def test_library_match_missing_field_keeps_existing_behaviour():
    """Items from a parse that never went through library augmentation
    (no `library_match` key at all) should still flow through the original
    paths — guards against accidentally regressing older saved runsheets."""
    payload = build_playlist_payload([
        {"parsed": {"type": "song", "title": "Old"},
         "match":  {"uuid": "u-old", "name": "Old", "index": 0}},
        {"parsed": {"type": "sermon", "title": "Older"}, "match": None},
    ])
    assert payload[0]["type"] == "presentation"
    assert payload[0]["target_uuid"] == "u-old"
    assert payload[1]["type"] == "header"


# ─── Section expansion: when `library_match` is a section dict from the
# operator's template playlist (header + media items), expand the runsheet
# item into the runsheet's own labelled header + each template media item
# as a real presentation entry. This is the headline feature — operators
# stop dragging "Culture" / "Welcome" slides in by hand every week. ────────


# Fixture-style helper: the section shape playlist_to_sections produces.
# Items carry their original PP `type` ("media" for video/image, "presentation"
# for .pro). The user's real "Youth Service - Library" Culture section turned
# out to be all media files — preserving the type avoided the PP-side 404.
def _culture_section():
    return {
        "header": {"name": "Culture", "uuid": "hdr-culture-uuid",
                   "color": {"red": 0.5, "green": 0.5, "blue": 0.5,
                             "alpha": 1.0}},
        "items": [
            {"name": "Heat the house", "uuid": "tpl-1-uuid",
             "target_uuid": "pres-heat-uuid", "index": 1, "type": "media"},
            {"name": "NO ONE STANDS ALONE", "uuid": "tpl-2-uuid",
             "target_uuid": "pres-noosa-uuid", "index": 2, "type": "media"},
            {"name": "Take Ownership", "uuid": "tpl-3-uuid",
             "target_uuid": "pres-take-uuid", "index": 3, "type": "media"},
        ],
    }


def test_section_library_match_expands_into_header_plus_presentations():
    """The headline feature: runsheet item "Culture: Lauren and Fynn — 6:45 PM"
    with library_match = Culture section (3 media items) expands to:
      1) runsheet's own coloured header (preserves Lauren/Fynn/time context)
      2) presentation entry for Heat the house
      3) presentation entry for NO ONE STANDS ALONE
      4) presentation entry for Take Ownership"""
    payload = build_playlist_payload([{
        "parsed": {
            "type":  "announcement",
            "title": "Culture: Lauren and Fynn",
            "notes": "6:45 PM",
            "library_match": _culture_section(),
        },
        "match": None,
    }])
    assert len(payload) == 4
    # 1. Header — runsheet's own labelling (NOT the template's "Culture" header).
    assert payload[0]["type"] == "header"
    assert "Culture: Lauren and Fynn" in payload[0]["id"]["name"]
    assert "6:45 PM" in payload[0]["id"]["name"]
    # 2-4. Each template media item becomes a real entry pointing at the
    # actual asset's UUID. Two PP-API rules locked in here:
    #   (a) id.uuid mirrors target_uuid (the asset UUID), NOT the template's
    #       playlist-item UUID — PP otherwise 404s because it looks up
    #       id.uuid against its asset store.
    #   (b) `type` preserves the template's original "media" / "presentation"
    #       — PP 404s on a media UUID sent as type="presentation" because
    #       it tries to resolve it from the .pro library.
    assert payload[1]["type"] == "media"               # was media in template
    assert payload[1]["target_uuid"] == "pres-heat-uuid"
    assert payload[1]["id"]["uuid"] == "pres-heat-uuid"  # NOT "tpl-1-uuid"
    assert payload[1]["id"]["name"] == "Heat the house"
    assert payload[2]["type"] == "media"
    assert payload[2]["target_uuid"] == "pres-noosa-uuid"
    assert payload[2]["id"]["uuid"] == "pres-noosa-uuid"
    assert payload[3]["type"] == "media"
    assert payload[3]["target_uuid"] == "pres-take-uuid"
    assert payload[3]["id"]["uuid"] == "pres-take-uuid"


def test_section_expansion_defaults_type_when_template_item_has_none():
    """Backward-compat: older code paths produce section dicts whose items
    lack the `type` field. We default to "presentation" so existing
    behaviour (single-pres reuse) stays unchanged."""
    section_no_type = {
        "header": {"name": "Old Shape", "uuid": "h"},
        "items":  [{"name": "Slide", "uuid": "old-tpl-uuid",
                    "target_uuid": "pres-old-uuid", "index": 0}],
    }
    payload = build_playlist_payload([{
        "parsed": {"type": "other", "title": "X",
                   "library_match": section_no_type},
        "match":  None,
    }])
    # header + 1 entry
    assert payload[1]["type"] == "presentation"
    assert payload[1]["target_uuid"] == "pres-old-uuid"


def test_section_expansion_preserves_loop_with_presentation_info_and_duration():
    """LOOPS in PP are `type: "presentation"` items with `duration` set
    and the .pro UUID nested in `presentation_info.presentation_uuid`.
    When the operator's MC Welcome section has a loop, the new playlist
    must keep the loop behaviour — that means echoing both
    presentation_info AND duration in the PUT payload, and using the
    nested presentation UUID (not target_uuid which is empty) as the
    asset UUID for id.uuid."""
    mc_welcome_with_loop = {
        "header": {"name": "MC Welcome", "uuid": "h-mcw"},
        "items":  [
            {"name": "loop",
             "uuid":  "tpl-loop-uuid",   # template playlist-item UUID — NOT used
             "type":  "presentation",
             "target_uuid":       "",
             "presentation_info": {"presentation_uuid": "pres-loop-uuid",
                                   "arrangement_name": "",
                                   "arrangement_uuid": ""},
             "duration":          15,
             "destination":       "presentation"},
            {"name": "preach_screen_3",
             "uuid":  "tpl-ps3-uuid",
             "type":  "media",
             "target_uuid":       "media-ps3-uuid",
             "presentation_info": {},
             "duration":          None,
             "destination":       "presentation"},
        ],
    }
    payload = build_playlist_payload([{
        "parsed": {"type": "mc_on_stage", "title": "MC Welcome - Lauren & Daisy",
                   "notes": "6:20 PM", "library_match": mc_welcome_with_loop},
        "match":  None,
    }])
    # header + loop + media = 3
    assert len(payload) == 3
    assert payload[0]["type"] == "header"

    loop = payload[1]
    assert loop["type"] == "presentation"
    assert loop["id"]["uuid"] == "pres-loop-uuid"           # from pinfo, NOT tpl-uuid
    assert loop["presentation_info"]["presentation_uuid"] == "pres-loop-uuid"
    assert loop["duration"] == 15                           # loop behaviour preserved
    assert loop["destination"] == "presentation"
    # PP's PUT requires target_uuid present even on presentations — we
    # emit empty string when the template doesn't supply one. (PP's own
    # GET omits target_uuid for loops but PUT validation is stricter.)
    assert loop["target_uuid"] == ""

    media = payload[2]
    assert media["type"] == "media"
    assert media["target_uuid"] == "media-ps3-uuid"
    assert media["id"]["uuid"] == "media-ps3-uuid"
    # Non-loop media items don't get a duration emitted (PP infers from file).
    assert "duration" not in media


def test_section_expansion_loop_without_duration_still_routes_pinfo():
    """A plain non-looping .pro presentation in a template (no duration)
    should still pass through with presentation_info intact and id.uuid
    set to the pinfo presentation UUID."""
    section = {
        "header": {"name": "X", "uuid": "h-x"},
        "items":  [{"name": "intro.pro",
                    "uuid":  "tpl-intro",
                    "type":  "presentation",
                    "target_uuid":       "",
                    "presentation_info": {"presentation_uuid": "pres-intro",
                                          "arrangement_name": "",
                                          "arrangement_uuid": ""},
                    "duration":          None,
                    "destination":       "presentation"}],
    }
    payload = build_playlist_payload([{
        "parsed": {"type": "other", "title": "X", "library_match": section},
        "match":  None,
    }])
    pres = payload[1]
    assert pres["type"] == "presentation"
    assert pres["id"]["uuid"] == "pres-intro"
    assert pres["presentation_info"]["presentation_uuid"] == "pres-intro"
    assert "duration" not in pres        # no loop = no duration emitted


def test_section_expansion_preserves_runsheet_order_across_items():
    """When multiple runsheet items each have a section_match, the
    output preserves runsheet order AND the per-item expansions stay
    contiguous (no interleaving)."""
    welcome = {
        "header": {"name": "Welcome", "uuid": "h-w"},
        "items":  [{"name": "Welcome 1", "uuid": "tpl-w1",
                    "target_uuid": "pres-w1", "index": 0}],
    }
    payload = build_playlist_payload([
        {"parsed": {"type": "announcement", "title": "MC Welcome",
                    "library_match": welcome}, "match": None},
        {"parsed": {"type": "sermon", "title": "Preach"}, "match": None},
        {"parsed": {"type": "announcement", "title": "Culture",
                    "library_match": _culture_section()}, "match": None},
    ])
    # Welcome (header + 1 pres) → Preach (header only) → Culture (header + 3 pres)
    assert len(payload) == 1 + 1 + 1 + 1 + 3
    assert payload[0]["type"] == "header" and "MC Welcome" in payload[0]["id"]["name"]
    assert payload[1]["target_uuid"] == "pres-w1"
    assert payload[2]["type"] == "header" and "Preach" in payload[2]["id"]["name"]
    assert payload[3]["type"] == "header" and "Culture" in payload[3]["id"]["name"]
    assert [it["target_uuid"] for it in payload[4:]] == [
        "pres-heat-uuid", "pres-noosa-uuid", "pres-take-uuid"]


def test_section_with_empty_items_only_emits_runsheet_header():
    """Defensive: a section whose template has no media (just a lone
    header in PP) shouldn't produce a presentation entry with empty
    target_uuid — it should just emit the runsheet header."""
    empty_section = {"header": {"name": "Lonely Section", "uuid": "h-lonely"},
                     "items": []}
    payload = build_playlist_payload([{
        "parsed": {"type": "other", "title": "Some section",
                   "library_match": empty_section},
        "match":  None,
    }])
    assert len(payload) == 1
    assert payload[0]["type"] == "header"
    assert "Some section" in payload[0]["id"]["name"]


def test_section_match_preempts_song_fuzzy_match():
    """A section_match (template playlist) should win over the song
    fuzzy match (library scan) — section reuse is more deliberate."""
    payload = build_playlist_payload([{
        "parsed": {"type": "song", "title": "Heat the house",
                   "library_match": _culture_section()},
        # If the fuzzy match were honoured, we'd get one presentation;
        # section expansion gives us header + 3 presentations instead.
        "match":  {"uuid": "wrong-uuid", "name": "wrong", "index": 0},
    }])
    assert len(payload) == 4  # header + 3 expanded media items
    assert "Heat the house" in payload[0]["id"]["name"]
    assert payload[1]["target_uuid"] == "pres-heat-uuid"
