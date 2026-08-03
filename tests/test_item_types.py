"""Tests for the fixed runsheet item-type list.

The model was inventing types — a real parse tagged an item with the
literal type "worship and ministry time" (the whole title), which broke
the tag colours, the Service Mate cue lookup, and any future logic keyed
on type. The operator's decision: there is a SET list, and everything
maps into it:

    song, mc_on_stage, announcement, sermon, prayer_and_ministry, other

`prayer` is renamed to `prayer_and_ministry` (altar call / ministry
moment after the sermon, or ministry time within the worship block).
Whatever the model emits, the server clamps it to this list — prompt
wording alone demonstrably doesn't enforce anything.
"""
import json

import pytest

from propresenterrunsheet.parsing.ai import (
    ALLOWED_ITEM_TYPES, canonicalize_item_type,
)


def test_allowed_list_is_exactly_the_agreed_six():
    assert ALLOWED_ITEM_TYPES == (
        "song", "mc_on_stage", "announcement", "sermon",
        "prayer_and_ministry", "other")


@pytest.mark.parametrize("t", list(
    ("song", "mc_on_stage", "announcement", "sermon",
     "prayer_and_ministry", "other")))
def test_allowed_types_pass_through(t):
    assert canonicalize_item_type(t) == t


def test_whitespace_and_case_are_forgiven():
    assert canonicalize_item_type("  Song ") == "song"
    assert canonicalize_item_type("MC on stage") == "mc_on_stage"


def test_legacy_prayer_becomes_prayer_and_ministry():
    """Old saved states and old model habits both say "prayer"."""
    assert canonicalize_item_type("prayer") == "prayer_and_ministry"


def test_ministry_flavoured_inventions_become_prayer_and_ministry():
    """The real invented type from the user's parse, plus close cousins.
    Ministry time — including the worship-block header — is
    prayer_and_ministry per the operator's definition."""
    assert canonicalize_item_type("worship and ministry time") == \
        "prayer_and_ministry"
    assert canonicalize_item_type("altar call") == "prayer_and_ministry"
    assert canonicalize_item_type("worship") == "prayer_and_ministry"


def test_preach_synonyms_become_sermon():
    assert canonicalize_item_type("preach") == "sermon"
    assert canonicalize_item_type("message") == "sermon"


def test_retired_types_clamp_to_other():
    """scripture / offering / video are no longer in the list — they were
    never in the operator's mental model and the model used them rarely
    and inconsistently."""
    for t in ("scripture", "offering", "video"):
        assert canonicalize_item_type(t) == "other"


def test_garbage_clamps_to_other():
    for t in ("", None, "🎸", "completely made up thing", 42):
        assert canonicalize_item_type(t) == "other"


# ── the route applies it ─────────────────────────────────────────────────────

def test_parse_route_clamps_invented_types(parse_client, isolated_state):
    """End-to-end through /api/upload_and_parse: whatever the model
    emits, the response only ever contains the six allowed types."""
    from tests.test_parse_route import _post

    reply = json.dumps({"service_name": "S", "items": [
        {"type": "worship and ministry time", "title": "Worship and Ministry Time"},
        {"type": "prayer",                    "title": "Altar Call"},
        {"type": "scripture",                 "title": "John 3:16"},
        {"type": "song",                      "title": "Holy Forever"},
        {"type": "banana",                    "title": "Go Live"},
    ]})
    body = _post(parse_client, reply).get_json()
    assert "error" not in body, body
    types = [it["type"] for it in body["items"]]
    assert types == ["prayer_and_ministry", "prayer_and_ministry",
                     "other", "song", "other"]
