"""Tests for Media-bin-aware linking of template media into new playlists.

The hard-won fact these tests encode (established by bisecting live PUTs
against ProPresenter 7): **PP's playlist PUT resolves media items by NAME
against the Media bin, and ignores the uuid entirely.** Verified probes:

    template Countdown's real uuid + name "Welcome"  -> 204 (accepted!)
    a random uuid            + name "Welcome"        -> 204
    any uuid                 + name "Countdown "     -> 404 (empty body)

"Welcome" exists in the operator's Media bin; "Countdown" does not (it was
dragged straight into the template playlist, never into Media). So sending
template target uuids is meaningless: a media item links if and only if a
media asset with that NAME is in the bin. Re-fetching the template cannot
fix a miss — which is why the old advice ("re-scan your library") sent the
operator in circles.

Consequences implemented here:
  1. fetch_media_bin() reads the bin so we can resolve by name up front.
  2. relink_media() swaps each matched media entry to its bin identity
     (bin uuid + bin's exact name), and REMOVES entries that aren't in
     the bin — reporting them so the UI can say, in plain words, "drag
     these into ProPresenter's Media area, then create again".
"""
from propresenterrunsheet.propresenter.media_bin import (
    fetch_media_bin, relink_media,
)


BIN = [
    {"uuid": "55C08096", "name": "Welcome"},
    # Trailing space preserved exactly as ProPresenter stores it — PP matches
    # media by EXACT name, so the space is load-bearing. See
    # test_bin_name_is_sent_verbatim_including_trailing_space.
    {"uuid": "AAA111",   "name": "Countdown "},
]


def _matched_with_media(title, media_name, uuid="TPL-U", target="TPL-T",
                        mtype="media"):
    entry = {"name": media_name, "uuid": uuid, "index": 0, "type": mtype,
             "target_uuid": target, "presentation_info": {}, "duration": None,
             "destination": "presentation"}
    if mtype == "presentation":
        entry["presentation_info"] = {"presentation_uuid": "PRES-U"}
    lib = {"header": {"name": media_name, "uuid": uuid, "color": {}},
           "items": [entry]}
    return {"parsed": {"type": "other", "title": title,
                       "library_match": lib},
            "match": lib}


# ── fetch_media_bin ──────────────────────────────────────────────────────────

def test_bin_name_is_sent_verbatim_including_trailing_space():
    """The whole point: PP matches media by EXACT name and ignores the uuid.

    Live probes against ProPresenter 7, same media item each time:
        uuid + name "Countdown " (bin's exact name)  -> 204
        uuid + name "Countdown"  (stripped)          -> 404
        image's uuid + video's exact name            -> 204  (uuid ignored)
        correct uuid + name with one extra space     -> 404

    So the bin's name must reach PP byte-for-byte. Stripping it — which is
    what shipped in v2.3.8 — silently 404s every media whose PP name has
    stray whitespace, which is exactly the operator's "Countdown "."""
    matched = [_matched_with_media("Countdown - Start 9:27am", "Countdown")]
    unlinked = relink_media(matched, BIN)
    assert unlinked == []
    entry = matched[0]["parsed"]["library_match"]["items"][0]
    assert entry["name"] == "Countdown ", \
        "must send the bin's exact name, trailing space and all"


def test_fetch_media_bin_preserves_exact_names():
    """fetch_media_bin must not strip: the raw name is what PP matches on."""
    class _R:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def fake_get(url, timeout=0):
        if url.endswith("/v1/media/playlists"):
            return _R([{"id": {"uuid": "MP1", "name": "Playlist"}}])
        return _R({"items": [
            {"id": {"uuid": "AAA111", "name": "Countdown "}},
            {"id": {"uuid": "  ", "name": "   "}},   # blank -> skipped
        ]})

    out = fetch_media_bin("http://pp", http_get=fake_get)
    assert [(m["uuid"], m["name"]) for m in out] == [("AAA111", "Countdown ")]


def test_fetch_media_bin_flattens_all_media_playlists():
    class _R:
        def __init__(self, payload): self._p = payload
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return self._p

    calls = []
    def fake_get(url, timeout=0):
        calls.append(url)
        if url.endswith("/v1/media/playlists"):
            return _R([{"id": {"uuid": "MP1", "name": "Playlist"}}])
        return _R({"items": [
            {"id": {"uuid": "55C08096", "name": "Welcome"}},
            {"id": {"uuid": "AAA111", "name": "Countdown"}},
            {"id": {"uuid": "", "name": "nameless-skipped"}},
            {"id": {"uuid": "BBB", "name": ""}},
        ]})

    out = fetch_media_bin("http://pp", http_get=fake_get)
    assert {(m["uuid"], m["name"]) for m in out} == {
        ("55C08096", "Welcome"), ("AAA111", "Countdown")}


def test_fetch_media_bin_returns_empty_on_any_failure():
    def broken_get(url, timeout=0):
        raise OSError("PP not running")
    assert fetch_media_bin("http://pp", http_get=broken_get) == []


# ── relink_media ─────────────────────────────────────────────────────────────

def test_media_in_bin_is_swapped_to_bin_identity():
    """The uuid PP actually honours is irrelevant — but sending the bin's
    exact name (and its uuid for cleanliness) is what guarantees a 204."""
    matched = [_matched_with_media("Welcome and Connection Cards", "Welcome",
                                   uuid="3626D448", target="3626D448")]
    unlinked = relink_media(matched, BIN)
    assert unlinked == []
    entry = matched[0]["parsed"]["library_match"]["items"][0]
    assert entry["uuid"] == "55C08096"
    assert entry["target_uuid"] == "55C08096"
    assert entry["name"] == "Welcome"


def test_name_matching_forgives_trailing_space_and_case():
    """FINDING the bin entry is whitespace/case-insensitive — the template
    name and the bin name may differ in stray spaces. What gets SENT is
    always the bin's exact name (see the verbatim test above)."""
    matched = [_matched_with_media("Countdown - Start 9:27am", "countdown")]
    unlinked = relink_media(matched, BIN)
    assert unlinked == []
    entry = matched[0]["parsed"]["library_match"]["items"][0]
    assert entry["uuid"] == "AAA111"
    assert entry["name"] == "Countdown "


def test_media_missing_from_bin_is_dropped_and_reported():
    matched = [_matched_with_media("Preach - Beyond - Ps Cathie",
                                   "Preach - Beyond")]
    unlinked = relink_media(matched, BIN)
    assert unlinked == [{"item_title": "Preach - Beyond - Ps Cathie",
                         "media_name": "Preach - Beyond"}]
    # The whole match collapses (its only item is gone) — the runsheet
    # item falls back to a plain coloured header, so create SUCCEEDS.
    assert matched[0]["parsed"]["library_match"] is None


def test_partial_section_keeps_the_linkable_items():
    m = _matched_with_media("Culture Moment", "Welcome")
    m["parsed"]["library_match"]["items"].append(
        {"name": "Not In Bin", "uuid": "X", "index": 1, "type": "media",
         "target_uuid": "XT", "presentation_info": {}, "duration": None,
         "destination": "presentation"})
    unlinked = relink_media([m], BIN)
    assert [u["media_name"] for u in unlinked] == ["Not In Bin"]
    names = [e["name"] for e in m["parsed"]["library_match"]["items"]]
    assert names == ["Welcome"]


def test_presentation_items_pass_through_untouched():
    """Presentation-type items PUT fine by uuid (verified live: 204) —
    they must not be renamed, swapped, or dropped."""
    matched = [_matched_with_media("Post Service Media", "Post Service Media",
                                   mtype="presentation")]
    unlinked = relink_media(matched, [])   # empty bin — irrelevant for these
    assert unlinked == []
    entry = matched[0]["parsed"]["library_match"]["items"][0]
    assert entry["uuid"] == "TPL-U"
    assert entry["type"] == "presentation"


def test_items_without_library_match_are_ignored():
    matched = [{"parsed": {"type": "song", "title": "Holy Forever",
                           "library_match": None}, "match": None}]
    assert relink_media(matched, BIN) == []
    assert matched[0]["parsed"]["library_match"] is None
