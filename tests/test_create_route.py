"""Tests for /api/create_playlist's Media-bin resolution + recovery.

Background (live-bisected against ProPresenter 7): PP's playlist PUT
resolves media items by NAME against the Media bin and ignores the uuid;
media absent from the bin 404s with an EMPTY body no matter what uuid is
sent. The route used to surface that as "ProPresenter rejected one of the
song UUIDs (404). Re-scan / re-fetch your library…" — wrong subject
(these aren't songs), wrong advice (no refresh can help), and it left the
operator with no playlist at all.

The behaviour pinned here: resolve media against the bin up front, drop
what cannot link (headers survive), always end with a created playlist,
and report the dropped slides so the UI can explain the one-time fix.
"""
import json

import pytest

import propresenterrunsheet.routes.playlist as playlist_mod


BIN = [{"uuid": "BIN-WELCOME", "name": "Welcome"}]


def _media_entry(name, uuid="TPL-U", target="TPL-T"):
    return {"name": name, "uuid": uuid, "index": 0, "type": "media",
            "target_uuid": target, "presentation_info": {}, "duration": None,
            "destination": "presentation"}


def _matched(title, media_name=None, mtype="other"):
    parsed = {"type": mtype, "title": title, "library_match": None}
    if media_name:
        parsed["library_match"] = {
            "header": {"name": media_name, "uuid": "H", "color": {}},
            "items": [_media_entry(media_name)]}
    return {"parsed": parsed, "match": parsed["library_match"]}


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def pp(monkeypatch):
    """Fake ProPresenter: records every PUT body; per-test scripted PUT
    status codes. Bin fetch is stubbed at the route module boundary."""
    state = {"puts": [], "put_codes": [204], "bin": BIN}

    import requests

    def fake_post(url, json=None, timeout=0):
        return _Resp(200, {"id": {"uuid": "NEW-PL", "name": "x"}})

    def fake_put(url, json=None, timeout=0):
        state["puts"].append(json)
        code = state["put_codes"][min(len(state["puts"]) - 1,
                                      len(state["put_codes"]) - 1)]
        return _Resp(code, text="")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(playlist_mod, "fetch_media_bin",
                        lambda _base: state["bin"])
    return state


def _create(client, matched):
    return client.post("/api/create_playlist", json={
        "host": "localhost", "port": "1", "name": "Test Service",
        "matched": matched, "create_timers": False}).get_json()


def test_media_is_resolved_to_bin_identity_before_the_put(client, pp):
    """The PUT must carry the bin's uuid and exact name — the only
    identity PP honours — not the template's."""
    body = _create(client, [_matched("Welcome and Cards", "Welcome")])
    assert body.get("ok") is True, body
    put = pp["puts"][0]
    media = [e for e in put if e["type"] == "media"]
    assert len(media) == 1
    assert media[0]["id"]["uuid"] == "BIN-WELCOME"
    assert media[0]["target_uuid"] == "BIN-WELCOME"
    assert body["unlinked"] == []


def test_media_not_in_bin_is_dropped_but_create_succeeds(client, pp):
    """The old behaviour was total failure with jargon advice. Now: the
    playlist is created, the header is there, and the response names the
    slide that couldn't attach."""
    body = _create(client, [_matched("Countdown - 9:27", "Countdown "),
                            _matched("Go Live")])
    assert body.get("ok") is True, body
    put = pp["puts"][0]
    assert [e["type"] for e in put] == ["header", "header"], \
        "unlinkable media must be dropped, headers kept"
    assert body["unlinked"] == [{"item_title": "Countdown - 9:27",
                                 "media_name": "Countdown"}]


def test_unexpected_put_404_retries_without_linked_slides(client, pp):
    """Belt and braces: if PP still refuses after bin resolution, strip
    the linked slides and push again rather than stranding the operator
    with nothing."""
    pp["put_codes"] = [404, 204]
    body = _create(client, [_matched("Welcome and Cards", "Welcome")])
    assert body.get("ok") is True, body
    assert len(pp["puts"]) == 2
    assert [e["type"] for e in pp["puts"][1]] == ["header"]
    assert body["unlinked"] == [{"item_title": "Welcome and Cards",
                                 "media_name": "Welcome"}]


def test_double_failure_message_has_no_jargon(client, pp):
    """If even headers-only is refused, the message must speak human:
    no UUIDs, no 're-scan your library', and it must not blame songs."""
    pp["put_codes"] = [404, 404]
    body = _create(client, [_matched("Welcome and Cards", "Welcome")])
    assert "error" in body
    for banned in ("UUID", "uuid", "Re-scan", "re-fetch", "song"):
        assert banned not in body["error"], \
            f"jargon {banned!r} in: {body['error']}"
    assert "ProPresenter" in body["error"]


def test_bin_fetch_failure_still_creates_with_old_style_payload(
        client, pp, monkeypatch):
    """fetch_media_bin returns [] on any failure, and an empty result is
    indistinguishable from a transient PP hiccup — so relinking must be
    SKIPPED, not applied (applying it would drop every slide on a blip).
    The template identity goes out as before; if PP then refuses, the
    safe-mode retry still saves the create."""
    monkeypatch.setattr(playlist_mod, "fetch_media_bin", lambda _base: [])
    body = _create(client, [_matched("Welcome and Cards", "Welcome")])
    assert body.get("ok") is True, body
    media = [e for e in pp["puts"][0] if e["type"] == "media"]
    assert len(media) == 1 and media[0]["id"]["uuid"] == "TPL-T", \
        "empty bin must skip relinking, not drop the slide"
