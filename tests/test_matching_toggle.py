"""Tests for the "Populate with media from PP" toggle.

For a brand-new event there is no template and no reusable media, so
every match attempt is noise — the operator only wants coloured headers
in ProPresenter. The toggle turns matching off at BOTH ends.

The strongest assertion here is the negative one: with the toggle off,
parse makes **zero ProPresenter calls**. That is not just tidiness —
it means the parse works with ProPresenter closed, and it is measurably
faster on the production machine, whose library holds 1,261 items.

The toggle deliberately does not persist. It resets to on with every new
file (enforced in the front end), because a matching switch that quietly
stayed off until next Sunday is the worse failure.
"""
import io

import pytest


class _Resp:
    status_code = 200

    def json(self):
        return {"model": "test/model:free", "choices": [{"message": {
            "content": '{"service_name":"New Event","items":['
                       '{"title":"Welcome","type":"mc_on_stage"},'
                       '{"title":"Great Are You Lord","type":"song"}]}'}}]}

    def raise_for_status(self):
        return None


@pytest.fixture
def pp_spy(client, monkeypatch):
    """Parse client that RECORDS every ProPresenter call instead of
    refusing them, so a test can assert none were made."""
    import propresenterrunsheet.routes.parse as parse_mod
    calls = []

    monkeypatch.setattr(parse_mod, "extract_pdf_text",
                        lambda _p: "10:00 Welcome\n10:05 Great Are You Lord")
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: None)

    def spy(name, result):
        def _f(*_a, **_k):
            calls.append(name)
            return result
        return _f

    monkeypatch.setattr(parse_mod, "fetch_pp_playlists",
                        spy("fetch_pp_playlists", [
                            {"uuid": "TPL", "name": "Youth Service - Library"}]))
    monkeypatch.setattr(parse_mod, "auto_detect_template_uuid",
                        spy("auto_detect_template_uuid", "TPL"))
    monkeypatch.setattr(parse_mod, "fetch_pp_playlist_items",
                        spy("fetch_pp_playlist_items", []))
    client._pp_calls = calls
    return client


def _parse(client, matching=None):
    import requests
    data = {"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "new-event.pdf"),
            "or_key": "sk-or-test", "or_model": "test/model:free"}
    if matching is not None:
        data["matching"] = matching
    orig = requests.post
    requests.post = lambda *a, **k: _Resp()
    try:
        return client.post("/api/upload_and_parse", data=data,
                           content_type="multipart/form-data")
    finally:
        requests.post = orig


# ── parse ────────────────────────────────────────────────────────────────

def test_matching_on_by_default_talks_to_propresenter(pp_spy):
    """The guard for the guard: if this stops calling PP, the negative
    test below would pass for the wrong reason."""
    _parse(pp_spy)
    assert pp_spy._pp_calls


def test_matching_off_makes_zero_propresenter_calls(pp_spy):
    _parse(pp_spy, matching="off")
    assert pp_spy._pp_calls == []


@pytest.mark.parametrize("value", ["off", "false", "0", "no"])
def test_off_is_recognised_in_the_shapes_a_form_can_send(pp_spy, value):
    _parse(pp_spy, matching=value)
    assert pp_spy._pp_calls == []


@pytest.mark.parametrize("value", ["on", "true", "1", "yes"])
def test_on_values_keep_matching_enabled(pp_spy, value):
    _parse(pp_spy, matching=value)
    assert pp_spy._pp_calls


def test_matching_off_still_returns_parsed_items(pp_spy):
    """Headers-only is the whole point — the items must still come back,
    just without any template links attached."""
    items = _parse(pp_spy, matching="off").get_json()["items"]
    assert [i["title"] for i in items] == ["Welcome", "Great Are You Lord"]
    assert all(i.get("library_match") is None for i in items)


def test_matching_off_still_clamps_item_types(pp_spy):
    """Type clamping is not matching, and the coloured headers depend on
    it — turning matching off must not let an invented type through."""
    items = _parse(pp_spy, matching="off").get_json()["items"]
    assert {i["type"] for i in items} <= {
        "song", "mc_on_stage", "announcement", "sermon",
        "prayer_and_ministry", "other"}


def test_matching_off_omits_template_names_from_the_prompt(pp_spy):
    """No PP fetch means no section names, so the library addendum must
    not be assembled either — otherwise the prompt asks the model to tag
    items against a list that isn't there."""
    import requests
    sent = {}

    def _fake_post(*_a, **kw):
        sent["payload"] = kw.get("json") or {}
        return _Resp()

    data = {"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "new-event.pdf"),
            "or_key": "sk-or-test", "or_model": "test/model:free",
            "matching": "off"}
    orig = requests.post
    requests.post = _fake_post
    try:
        pp_spy.post("/api/upload_and_parse", data=data,
                    content_type="multipart/form-data")
    finally:
        requests.post = orig
    assert "Youth Service - Library" not in sent["payload"][
        "messages"][0]["content"]


# ── create ───────────────────────────────────────────────────────────────

@pytest.fixture
def create_spy(client, monkeypatch):
    """Create-route client recording the two matching steps."""
    import propresenterrunsheet.routes.playlist as pl_mod
    calls = []
    monkeypatch.setattr(pl_mod, "_rematch_template",
                        lambda *a, **k: calls.append("rematch"))
    monkeypatch.setattr(pl_mod, "fetch_media_bin",
                        lambda *a, **k: calls.append("bin") or [])
    client._calls = calls
    return client


def _create(client, matching=None):
    import requests

    class _R:
        status_code = 200
        text = "{}"
        def json(self):
            return {"id": {"uuid": "NEW"}}
        def raise_for_status(self):
            return None

    payload = {"name": "New Event", "host": "localhost", "port": "50001",
               "matched": [{"parsed": {"title": "Welcome",
                                       "type": "mc_on_stage"},
                            "match": None}]}
    if matching is not None:
        payload["matching"] = matching

    orig = (requests.post, requests.put, requests.get)
    requests.post = requests.put = requests.get = lambda *a, **k: _R()
    try:
        return client.post("/api/create_playlist", json=payload)
    finally:
        requests.post, requests.put, requests.get = orig


def test_create_matches_by_default(create_spy):
    """Guard for the guard below."""
    _create(create_spy)
    assert create_spy._calls


def test_create_with_matching_off_skips_rescue_and_media_bin(create_spy):
    """Create re-runs template linking to rescue a parse made while PP
    was closed. With the toggle off that rescue must not fire, or it
    would hand back exactly what the operator turned off."""
    _create(create_spy, matching=False)
    assert create_spy._calls == []


def test_create_with_matching_off_still_builds_a_playlist(create_spy):
    """Headers-only is a real deliverable, not a no-op."""
    r = _create(create_spy, matching=False)
    assert r.status_code == 200
    assert "error" not in (r.get_json() or {})
