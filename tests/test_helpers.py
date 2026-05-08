"""Pure-function unit tests. No I/O, no Flask, no LAN."""
import datetime as dt


# ── Text normalisation + fuzzy matching ──────────────────────────────────────

def test_norm_lowercases_and_strips_punctuation(app_module):
    assert app_module._norm("Build My Life!") == "build my life"
    assert app_module._norm("  King's Highway  ") == "kings highway"
    assert app_module._norm("") == ""
    assert app_module._norm(None) == ""


def test_fuzzy_match_returns_best_above_threshold(app_module):
    library = [
        {"name": "Build My Life", "uuid": "u1", "index": 0},
        {"name": "King of Kings", "uuid": "u2", "index": 1},
        {"name": "Holy Forever",  "uuid": "u3", "index": 2},
    ]
    match, score = app_module.fuzzy_match("build my life", library, threshold=0.5)
    assert match is not None
    assert match["uuid"] == "u1"
    assert score > 0.9


def test_fuzzy_match_returns_none_below_threshold(app_module):
    library = [{"name": "Build My Life", "uuid": "u1", "index": 0}]
    match, _ = app_module.fuzzy_match("totally different", library, threshold=0.9)
    assert match is None


# ── Time / duration parsing ──────────────────────────────────────────────────

def test_extract_time_str_canonicalises_known_formats(app_module):
    assert app_module._extract_time_str("9:30 AM service open") == "9:30 AM"
    assert app_module._extract_time_str("starts at 12:05PM") == "12:05 PM"
    assert app_module._extract_time_str("9:30am") == "9:30 AM"


def test_extract_time_str_rejects_garbage(app_module):
    # Out-of-range hour, no AM/PM marker, completely missing
    assert app_module._extract_time_str("13:00 PM") == ""
    assert app_module._extract_time_str("9:30 service") == ""
    assert app_module._extract_time_str("") == ""
    assert app_module._extract_time_str(None) == ""


def test_extract_duration_min_prefers_explicit_field(app_module):
    item = {"duration_min": 20, "notes": "30 min in title"}
    assert app_module._extract_duration_min(item) == 20


def test_extract_duration_min_falls_back_to_notes_then_title(app_module):
    assert app_module._extract_duration_min(
        {"duration_min": 0, "notes": "20 min slot"}
    ) == 20
    assert app_module._extract_duration_min(
        {"duration_min": 0, "notes": "", "title": "Worship (15 minutes)"}
    ) == 15


def test_extract_duration_min_rejects_silly_durations(app_module):
    # >= 24h is rejected to keep accidentally-matched timestamps from
    # producing nonsense timers.
    assert app_module._extract_duration_min({"title": "1500 minutes"}) == 0
    assert app_module._extract_duration_min({}) == 0


def test_format_mmss_handles_positive_negative_and_none(app_module):
    assert app_module._format_mmss(0) == "00:00"
    assert app_module._format_mmss(78) == "01:18"
    assert app_module._format_mmss(3600) == "60:00"
    assert app_module._format_mmss(-15) == "-00:15"
    assert app_module._format_mmss(None) == "--:--"


def test_parse_pp_time_recognises_pp_formats(app_module):
    assert app_module._parse_pp_time("00:01:30") == 90
    assert app_module._parse_pp_time("12:34") == 12 * 60 + 34
    assert app_module._parse_pp_time("45") == 45
    assert app_module._parse_pp_time(120) == 120
    assert app_module._parse_pp_time(None) is None
    assert app_module._parse_pp_time("nonsense") is None


# ── Header decoration stripping (auto-track relies on this) ──────────────────

def test_clean_header_name_strips_action_needed_prefix(app_module):
    # PP playlist stores songs we couldn't match as "⚠ ACTION NEEDED — Title  (9:50 AM)"
    cleaned = app_module._clean_header_name("⚠ ACTION NEEDED — Alleluia  (9:50 AM)")
    assert cleaned == "Alleluia"


def test_clean_header_name_strips_book_emoji_and_time_tail(app_module):
    assert app_module._clean_header_name(
        "📖 Genesis 1:23-28  —  9:55 AM"
    ) == "Genesis 1:23-28"


def test_clean_header_name_strips_em_dash_time_tail(app_module):
    assert app_module._clean_header_name(
        "Worship and Ministry Time  —  9:30 AM"
    ) == "Worship and Ministry Time"


def test_clean_header_name_passthrough_when_no_decorations(app_module):
    assert app_module._clean_header_name("Plain item title") == "Plain item title"
    assert app_module._clean_header_name("") == ""


# ── Per-role cue lookup ──────────────────────────────────────────────────────

def test_cue_for_prefers_llm_cue_over_rule_table(app_module):
    item = {"type": "song", "cues": {"sound": "Custom mic call"}}
    assert app_module._cue_for("sound", item) == "Custom mic call"


def test_cue_for_falls_back_to_rule_table_when_llm_cue_missing(app_module):
    item = {"type": "sermon", "cues": {}}
    assert app_module._cue_for("sound", item) == "Speaker mic ON"
    assert app_module._cue_for("lights", item) == "Spot — preacher"


def test_cue_for_handles_unknown_type_gracefully(app_module):
    assert app_module._cue_for("screen", {"type": "made_up_type"}) == "Get ready"


def test_cue_for_returns_empty_for_none_item(app_module):
    assert app_module._cue_for("sound", None) == ""


def test_ensure_item_cues_fills_missing_roles_only(app_module):
    item = {"type": "song", "cues": {"screen": "Custom"}}
    out = app_module._ensure_item_cues(item)
    cues = out["cues"]
    assert cues["screen"] == "Custom"
    # The other two roles came from the rule table:
    assert cues["sound"] == "Band mics live · MC mute"
    assert cues["lights"] == "Stage wash — band"


def test_ensure_item_cues_creates_cues_dict_when_absent(app_module):
    out = app_module._ensure_item_cues({"type": "prayer"})
    assert "cues" in out
    assert all(out["cues"].get(r) for r in ("screen", "sound", "lights"))


# ── Remaining-time computation ───────────────────────────────────────────────

def test_compute_remaining_seconds_prefers_pp_remaining(app_module):
    state = {
        "items": [{"duration_min": 5}],
        "current_index": 0,
        "current_started_at": dt.datetime.now().isoformat(),
        "pp_remaining_seconds": 42,
    }
    assert app_module._compute_remaining_seconds(state) == 42


def test_compute_remaining_seconds_falls_back_to_clock_diff(app_module):
    started = dt.datetime.now() - dt.timedelta(seconds=60)
    state = {
        "items": [{"duration_min": 2}],  # 120 s total
        "current_index": 0,
        "current_started_at": started.isoformat(),
    }
    rem = app_module._compute_remaining_seconds(state)
    assert rem is not None
    # Allow for ~3 s of test runtime drift
    assert 55 <= rem <= 65


def test_compute_remaining_seconds_returns_none_when_unknown(app_module):
    assert app_module._compute_remaining_seconds({}) is None
    # No started_at → can't compute
    assert app_module._compute_remaining_seconds({
        "items": [{"duration_min": 2}], "current_index": 0,
    }) is None


# ── Next-visible-item helper ─────────────────────────────────────────────────

def test_next_visible_item_returns_following_or_none(app_module):
    items = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    assert app_module._next_visible_item(items, 0)["title"] == "b"
    assert app_module._next_visible_item(items, 1)["title"] == "c"
    assert app_module._next_visible_item(items, 2) is None
    assert app_module._next_visible_item([], 0) is None


# ── Rendered-text width ──────────────────────────────────────────────────────

def test_text_width_returns_positive_int_for_real_text(app_module):
    f = app_module._sm_font(20)
    assert app_module._text_width(f, "hello") > 0
    assert app_module._text_width(f, "") == 0


# ── Active-section detection (with mocked HTTP) ──────────────────────────────

class _StubResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = ""

    def json(self):
        return self._payload


def test_pp_active_section_index_walks_back_to_header(app_module, monkeypatch):
    """Operator clicks media at index 3 (under the 'Countdown' header at
    index 2). _pp_active_section_index should walk back and match the header
    name 'Countdown - Start 9:27am  —  9:25 AM' → cleaned 'Countdown - Start
    9:27am' → runsheet item 1."""
    playlist_uuid = "PLIST-UUID"
    active_response = {"presentation": {
        "playlist":      {"uuid": playlist_uuid, "name": "Sunday", "index": 0},
        "playlist_item": {"id": {"uuid": "x", "name": "media file", "index": 3}},
    }}
    full_playlist_response = {"items": [
        {"id": {"name": "Go live - online streaming  —  9:24 AM", "index": 0}, "type": "header"},
        {"id": {"name": "preach_screen_3", "index": 1}, "type": "media"},
        {"id": {"name": "Countdown - Start 9:27am  —  9:25 AM", "index": 2}, "type": "header"},
        {"id": {"name": "manual_media", "index": 3}, "type": "media"},
    ]}

    def fake_get(url, *a, **kw):
        if url.endswith("/v1/playlist/active"):
            return _StubResponse(active_response)
        if f"/v1/playlist/{playlist_uuid}" in url:
            return _StubResponse(full_playlist_response)
        return _StubResponse({}, status=404)

    # Patch the playlist cache on the underlying module — propresenter_app
    # only re-exports the binding, so patching the alias there wouldn't change
    # what pp_track.py reads.
    from propresenterrunsheet.service_mate import pp_track
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    import requests as req
    monkeypatch.setattr(req, "get", fake_get)

    state = {"items": [
        {"title": "Go live - online streaming"},
        {"title": "Countdown - Start 9:27am"},
        {"title": "Worship and Ministry Time"},
    ], "current_index": 0}
    idx = app_module._pp_active_section_index(state, "http://localhost:55416")
    assert idx == 1


def test_pp_active_section_index_returns_none_when_pp_unreachable(app_module, monkeypatch):
    import requests as req

    def boom(*a, **kw):
        raise req.exceptions.ConnectionError("nope")

    monkeypatch.setattr(req, "get", boom)
    from propresenterrunsheet.service_mate import pp_track
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    assert app_module._pp_active_section_index(
        {"items": [{"title": "a"}]}, "http://localhost:55416"
    ) is None


def test_pp_active_section_uuid_match_wins_over_index(app_module, monkeypatch):
    """The bug case: PP briefly returns a stale or off-by-one numeric index
    after an operator clicks a media item. The accompanying UUID is correct,
    so we should resolve the active item's position by UUID and walk back
    from THERE — not from the bogus index, which would land on the next
    section's header."""
    playlist_uuid = "PLIST-UUID"
    # PP says active item is at index=3 (the next section's header), but its
    # UUID matches the SERMON item which actually lives at position 1.
    active_response = {"presentation": {
        "playlist":      {"uuid": playlist_uuid, "name": "Sunday", "index": 0},
        "playlist_item": {"id": {"uuid": "SERMON-UUID",
                                 "name": "Sermon Slides", "index": 3}},
    }}
    full_playlist_response = {"items": [
        {"id": {"uuid": "PREACH-HDR", "name": "Preach  —  10:30 AM"},
         "type": "header"},
        {"id": {"uuid": "SERMON-UUID", "name": "Sermon Slides"},
         "type": "presentation"},
        {"id": {"uuid": "RESPONSE-HDR", "name": "Response  —  11:00 AM"},
         "type": "header"},
        {"id": {"uuid": "RESPONSE-SONG", "name": "Response Song"},
         "type": "presentation"},
    ]}

    def fake_get(url, *a, **kw):
        if url.endswith("/v1/playlist/active"):
            return _StubResponse(active_response)
        if f"/v1/playlist/{playlist_uuid}" in url:
            return _StubResponse(full_playlist_response)
        return _StubResponse({}, status=404)

    from propresenterrunsheet.service_mate import pp_track
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    import requests as req
    monkeypatch.setattr(req, "get", fake_get)

    state = {"items": [
        {"title": "Preach"},       # idx 0 — what we should land on
        {"title": "Response"},     # idx 1 — wrong answer (the bug)
    ], "current_index": 0}
    # Without the UUID-first fix, `index=3` would point at "Response  —  11 AM",
    # which is itself a header → we'd return idx 1 (Response). With UUID-first
    # we resolve to position 1 (Sermon Slides), walk back to position 0
    # (Preach header), match runsheet idx 0.
    assert app_module._pp_active_section_index(
        state, "http://localhost:55416"
    ) == 0


def test_maybe_advance_requires_two_polls_before_committing(app_module, monkeypatch):
    """Stickiness: a single poll showing a new target shouldn't move
    current_index — that lets a transient bad poll right after a click drag
    the clocks to the wrong section. We require the same target to come back
    on the next poll before committing."""
    playlist_uuid = "PLIST-UUID"
    active_response = {"presentation": {
        "playlist":      {"uuid": playlist_uuid, "name": "Sunday", "index": 0},
        "playlist_item": {"id": {"uuid": "SERMON-UUID",
                                 "name": "Sermon Slides", "index": 1}},
    }}
    full_playlist_response = {"items": [
        {"id": {"uuid": "PREACH-HDR", "name": "Preach"},  "type": "header"},
        {"id": {"uuid": "SERMON-UUID", "name": "Sermon"},
         "type": "presentation"},
    ]}

    def fake_get(url, *a, **kw):
        if url.endswith("/v1/playlist/active"):
            return _StubResponse(active_response)
        if f"/v1/playlist/{playlist_uuid}" in url:
            return _StubResponse(full_playlist_response)
        if url.endswith("/v1/timers/current"):
            return _StubResponse([])
        return _StubResponse({}, status=404)

    from propresenterrunsheet.service_mate import pp_track
    from propresenterrunsheet import settings as ppr_settings
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    monkeypatch.setattr(pp_track, "_PENDING_SECTION_TARGET",
                        {"index": None, "count": 0})
    monkeypatch.setattr(ppr_settings, "load_settings",
                        lambda: {"pp_host": "localhost", "pp_port": "55416"})
    import requests as req
    monkeypatch.setattr(req, "get", fake_get)

    state = {"items": [
        {"title": "Welcome"},  # idx 0 — current
        {"title": "Preach"},   # idx 1 — target
    ], "current_index": 0,
       "auto_track": {"enabled": True}}

    # First poll: target detected (1), but pending — no commit yet.
    state = app_module._maybe_advance_from_pp(state)
    assert state["current_index"] == 0
    assert pp_track._PENDING_SECTION_TARGET["index"] == 1
    assert pp_track._PENDING_SECTION_TARGET["count"] == 1

    # Second poll: same target — commits.
    state = app_module._maybe_advance_from_pp(state)
    assert state["current_index"] == 1
    assert state.get("pp_source") == "section"
    assert pp_track._PENDING_SECTION_TARGET["index"] is None
    assert pp_track._PENDING_SECTION_TARGET["count"] == 0


def test_maybe_advance_resets_pending_when_target_changes(app_module, monkeypatch):
    """A flickering signal (target jumps poll-to-poll) shouldn't ever commit.
    The pending counter resets to 1 whenever the target changes."""
    playlist_uuid = "PLIST-UUID"
    full_playlist_response = {"items": [
        {"id": {"uuid": "H-PREACH", "name": "Preach"},  "type": "header"},
        {"id": {"uuid": "SLIDE-A", "name": "Slide A"},  "type": "presentation"},
        {"id": {"uuid": "H-RESP",  "name": "Response"}, "type": "header"},
        {"id": {"uuid": "SLIDE-B", "name": "Slide B"},  "type": "presentation"},
    ]}
    # Alternate which item PP claims is active on each call.
    active_uuids = iter(["SLIDE-A", "SLIDE-B", "SLIDE-A"])

    def fake_get(url, *a, **kw):
        if url.endswith("/v1/playlist/active"):
            uuid = next(active_uuids)
            return _StubResponse({"presentation": {
                "playlist":      {"uuid": playlist_uuid},
                "playlist_item": {"id": {"uuid": uuid, "name": "x", "index": 0}},
            }})
        if f"/v1/playlist/{playlist_uuid}" in url:
            return _StubResponse(full_playlist_response)
        if url.endswith("/v1/timers/current"):
            return _StubResponse([])
        return _StubResponse({}, status=404)

    from propresenterrunsheet.service_mate import pp_track
    from propresenterrunsheet import settings as ppr_settings
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    monkeypatch.setattr(pp_track, "_PENDING_SECTION_TARGET",
                        {"index": None, "count": 0})
    monkeypatch.setattr(ppr_settings, "load_settings",
                        lambda: {"pp_host": "localhost", "pp_port": "55416"})
    import requests as req
    monkeypatch.setattr(req, "get", fake_get)

    state = {"items": [
        {"title": "Preach"},    # idx 0
        {"title": "Response"},  # idx 1
    ], "current_index": 0,
       "auto_track": {"enabled": True}}

    # Three flickering polls — each one is a different target than the last,
    # so the counter never reaches the threshold; current_index stays put.
    for _ in range(3):
        state = app_module._maybe_advance_from_pp(state)
    assert state["current_index"] == 0


def test_maybe_advance_suppresses_signal_3_when_playlist_active(app_module, monkeypatch):
    """When PP has an active playlist but signal 1 can't resolve a confident
    section, signal 3 (presentation-name fuzzy match) must NOT fire — it was
    a major source of "advanced to wrong section" bugs. Signal 3 is only the
    right channel when there's no active playlist at all."""
    playlist_uuid = "PLIST-UUID"
    # Active playlist exists, but the active item's header doesn't appear in
    # our cached playlist (e.g., operator added an unrelated item) → signal 1
    # returns None for section_idx, but has_active_playlist is True.
    active_response = {"presentation": {
        "playlist":      {"uuid": playlist_uuid, "name": "Sunday"},
        "playlist_item": {"id": {"uuid": "UNKNOWN-UUID",
                                 "name": "Unknown", "index": 99}},
    }}
    full_playlist_response = {"items": [
        {"id": {"uuid": "H-WELCOME", "name": "Welcome"}, "type": "header"},
    ]}
    # Signal 3 would happily match this against runsheet item "Response".
    presentation_response = {"presentation": {
        "id": {"name": "Response"},
    }}

    def fake_get(url, *a, **kw):
        if url.endswith("/v1/playlist/active"):
            return _StubResponse(active_response)
        if f"/v1/playlist/{playlist_uuid}" in url:
            return _StubResponse(full_playlist_response)
        if url.endswith("/v1/timers/current"):
            return _StubResponse([])
        if url.endswith("/v1/presentation/active"):
            return _StubResponse(presentation_response)
        return _StubResponse({}, status=404)

    from propresenterrunsheet.service_mate import pp_track
    from propresenterrunsheet import settings as ppr_settings
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    monkeypatch.setattr(pp_track, "_PENDING_SECTION_TARGET",
                        {"index": None, "count": 0})
    monkeypatch.setattr(ppr_settings, "load_settings",
                        lambda: {"pp_host": "localhost", "pp_port": "55416"})
    import requests as req
    monkeypatch.setattr(req, "get", fake_get)

    state = {"items": [
        {"title": "Welcome"},   # idx 0 — current
        {"title": "Response"},  # idx 1 — what signal 3 would advance to
    ], "current_index": 0,
       "auto_track": {"enabled": True}}

    state = app_module._maybe_advance_from_pp(state)
    # Suppression worked: signal 3 didn't fire, so we stayed at 0.
    assert state["current_index"] == 0
    assert state.get("pp_source") != "presentation"


def test_maybe_advance_signal_3_still_fires_without_active_playlist(app_module, monkeypatch):
    """Sanity check: the suppression in the previous test only kicks in when
    a playlist is active. With no active playlist (PP in non-playlist mode),
    signal 3 should still work — that's its whole purpose."""
    # No active playlist — /v1/playlist/active returns empty/no presentation.
    def fake_get(url, *a, **kw):
        if url.endswith("/v1/playlist/active"):
            return _StubResponse({"presentation": None})
        if url.endswith("/v1/timers/current"):
            return _StubResponse([])
        if url.endswith("/v1/presentation/active"):
            return _StubResponse({"presentation": {
                "id": {"name": "Sermon"},
            }})
        return _StubResponse({}, status=404)

    from propresenterrunsheet.service_mate import pp_track
    from propresenterrunsheet import settings as ppr_settings
    monkeypatch.setattr(pp_track, "_PP_PLAYLIST_CACHE",
                        {"uuid": None, "items": [], "fetched_at": 0.0})
    monkeypatch.setattr(pp_track, "_PENDING_SECTION_TARGET",
                        {"index": None, "count": 0})
    monkeypatch.setattr(ppr_settings, "load_settings",
                        lambda: {"pp_host": "localhost", "pp_port": "55416"})
    import requests as req
    monkeypatch.setattr(req, "get", fake_get)

    state = {"items": [
        {"title": "Welcome"},
        {"title": "Sermon"},
    ], "current_index": 0,
       "auto_track": {"enabled": True}}

    state = app_module._maybe_advance_from_pp(state)
    assert state["current_index"] == 1
    assert state.get("pp_source") == "presentation"
