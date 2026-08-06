"""Tests for re-linking an already-parsed runsheet to ProPresenter.

Field request (2026-08-04): after parsing, the operator renames something
in ProPresenter and wants the links recomputed WITHOUT burning another AI
parse — the runsheet text hasn't changed, only what PP contains. This is
also the honest fix for a free tier capped at 50 AI requests a day.

`link_items_to_template` is the shared engine: it attaches
`library_match` to parsed items from the template playlist. The create
route calls it to rescue items PP was closed for; /api/match calls it
with force=True when the operator hits Re-match.
"""
import pytest

from propresenterrunsheet.propresenter.templates import (
    link_items_to_template,
)

TEMPLATE = [
    {"id": {"uuid": "H1", "name": "Culture", "index": 0}, "type": "header",
     "target_uuid": ""},
    {"id": {"uuid": "M1", "name": "Culture Bumper", "index": 1},
     "type": "media", "target_uuid": "A1", "destination": "presentation"},
    # Welcome gets its own header — without one it would belong to the
    # Culture section, which is correct PP semantics but not what this
    # fixture is trying to model.
    {"id": {"uuid": "H2", "name": "Welcome Slides", "index": 2},
     "type": "header", "target_uuid": ""},
    {"id": {"uuid": "M2", "name": "Welcome", "index": 3},
     "type": "media", "target_uuid": "A2", "destination": "presentation"},
]


def _fetch(_base, _uuid):
    return TEMPLATE


def test_links_unmatched_items_from_the_template():
    items = [{"type": "other", "title": "Welcome and Cards",
              "library_match": None}]
    n = link_items_to_template(items, "http://pp", "TPL", fetch=_fetch)
    assert n == 1
    assert items[0]["library_match"]["header"]["name"] == "Welcome"


def test_section_header_hit_expands_the_whole_section():
    items = [{"type": "other", "title": "Culture Moment",
              "library_match": None}]
    link_items_to_template(items, "http://pp", "TPL", fetch=_fetch)
    assert len(items[0]["library_match"]["items"]) == 1
    assert items[0]["library_match"]["items"][0]["name"] == "Culture Bumper"


def test_songs_are_left_alone():
    """Songs belong to the library fuzzy-match + Pick flow — a template
    slide called "Welcome" must never hijack a song."""
    items = [{"type": "song", "title": "Welcome Home", "library_match": None}]
    assert link_items_to_template(items, "http://pp", "TPL", fetch=_fetch) == 0
    assert items[0]["library_match"] is None


def test_without_force_an_existing_match_is_kept():
    existing = {"header": {"name": "OLD"}, "items": []}
    items = [{"type": "other", "title": "Welcome and Cards",
              "library_match": existing}]
    link_items_to_template(items, "http://pp", "TPL", fetch=_fetch)
    assert items[0]["library_match"] is existing


def test_force_recomputes_every_item():
    """The Re-match button's whole point: the operator renamed something
    in PP, so previously-computed links must be thrown away and redone."""
    items = [{"type": "other", "title": "Welcome and Cards",
              "library_match": {"header": {"name": "STALE"}, "items": []}}]
    n = link_items_to_template(items, "http://pp", "TPL", fetch=_fetch,
                               force=True)
    assert n == 1
    assert items[0]["library_match"]["header"]["name"] == "Welcome"


def test_force_clears_a_link_that_no_longer_matches():
    """If the operator renamed the template slide so nothing matches any
    more, the stale link must be dropped rather than left behind."""
    items = [{"type": "other", "title": "Nothing Matches This",
              "library_match": {"header": {"name": "STALE"}, "items": []}}]
    link_items_to_template(items, "http://pp", "TPL", fetch=_fetch,
                           force=True)
    assert items[0]["library_match"] is None


def test_aliases_are_honoured():
    items = [{"type": "other", "title": "Youth Arrival + Hangout",
              "library_match": None}]
    link_items_to_template(items, "http://pp", "TPL", fetch=_fetch,
                           aliases=[{"match": "Youth Arrival",
                                     "template": "Welcome"}])
    assert items[0]["library_match"]["header"]["name"] == "Welcome"


def test_empty_template_is_a_no_op_and_never_raises():
    items = [{"type": "other", "title": "Welcome", "library_match": None}]
    assert link_items_to_template(items, "http://pp", "TPL",
                                  fetch=lambda *_: []) == 0
    assert items[0]["library_match"] is None
