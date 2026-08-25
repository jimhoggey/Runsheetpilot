"""Auto must decline when no template is for THIS service.

The 25 Aug 2026 report: a Young Adults runsheet was parsed on a machine
whose ProPresenter held exactly one template playlist — "Youth Service -
Library". Auto attached it, and the new playlist came out pre-populated
with youth media that had nothing to do with the event.

The cause was a fallback, not a scoring bug. `auto_detect_template_uuid`
returned `candidates[0]` whenever nothing scored, so on a machine with
one template it could only ever return that one. The rule now: a template
whose name carries distinctive tokens is a CLAIM about which service it
serves, and zero overlap means the claim does not hold — decline. A
generically named template ("Library") makes no claim, so it cannot
contradict a runsheet and stays usable.

Three call sites resolve "Auto" — parse, re-match, and create — and each
used to invent its own hint, so they could disagree. Create was the
dangerous one: it re-derived from scratch and would silently re-attach a
template that parse had correctly declined. They now share one hint (the
service label the model reports) and carry the resolved answer forward,
so they agree by construction. The create-route test at the bottom is
the one that pins that; it is the regression that would actually reach a
Sunday.
"""
import io
import json

import pytest

import propresenterrunsheet.routes.playlist as playlist_mod


YOUTH_ONLY = [{"uuid": "u-youth", "name": "Youth Service - Library"}]

MANY = [
    {"uuid": "u-youth",  "name": "Youth Service - Library"},
    {"uuid": "u-sunday", "name": "Sunday Morning Library"},
    {"uuid": "u-wed",    "name": "Wednesday Prayer Library"},
]


# ── The rule itself ──────────────────────────────────────────────────────────

def test_declines_when_no_template_names_this_service(app_module):
    """The reported failure, at the unit level. "Young Adults" shares no
    distinctive token with "Youth Service - Library" — youth != young —
    so the answer is no template, not the only template."""
    assert app_module.auto_detect_template_uuid(
        YOUTH_ONLY, hint="Young Adults") is None
    assert app_module.auto_detect_template_uuid(
        MANY, hint="Young Adults") is None


def test_still_picks_the_template_that_does_name_the_service(app_module):
    """The decline must not cost us the case Auto exists for."""
    assert app_module.auto_detect_template_uuid(
        YOUTH_ONLY, hint="Youth") == "u-youth"
    assert app_module.auto_detect_template_uuid(
        MANY, hint="Sunday Morning") == "u-sunday"


def test_a_generically_named_template_is_never_a_contradiction(app_module):
    """"Library" claims nothing about which service it is for, so it
    cannot conflict with a runsheet — it stays the catch-all even when
    the service-specific templates all decline. Guards a naming style
    this operator does not use but a future one might."""
    pls = [{"uuid": "u-youth", "name": "Youth Service - Library"},
           {"uuid": "u-any",   "name": "Library"}]
    assert app_module.auto_detect_template_uuid(
        pls, hint="Young Adults") == "u-any"
    # A specific match still beats the catch-all.
    assert app_module.auto_detect_template_uuid(
        pls, hint="Youth") == "u-youth"


def test_no_hint_at_all_keeps_the_legacy_first_candidate(app_module):
    """No hint is no EVIDENCE, which is not the same as evidence of a
    mismatch. Callers that supply nothing keep the old behaviour."""
    assert app_module.auto_detect_template_uuid(MANY) == "u-youth"


def test_a_hint_of_only_filler_words_keeps_the_legacy_fallback(app_module):
    """"Service", "library" and "template" appear in every template name,
    so a hint made only of those carries no signal — that is the no-hint
    case, not a mismatch."""
    assert app_module.auto_detect_template_uuid(
        MANY, hint="Service Library template service the service") == "u-youth"


def test_no_template_named_playlists_at_all_is_still_none(app_module):
    """Unchanged: nothing to match against, and that has to be fine."""
    assert app_module.auto_detect_template_uuid(
        [{"uuid": "u1", "name": "Sunday Service — 22 May 2026"}],
        hint="Young Adults") is None
    assert app_module.auto_detect_template_uuid([], hint="Youth") is None


# ── The model's service label ────────────────────────────────────────────────

def test_parse_ai_response_returns_the_service_label(app_module):
    items, name, label = app_module.parse_ai_response(
        '{"service_name":"Young Adults — 24 Aug 2026",'
        '"service_type":"Young Adults","items":[{"title":"Welcome"}]}')
    assert label == "Young Adults"
    assert name == "Young Adults — 24 Aug 2026"
    assert len(items) == 1


def test_parse_ai_response_tolerates_a_reply_without_the_label(app_module):
    """Every free model on OpenRouter will not honour a new field on day
    one, and a missing label must degrade to "no extra evidence" rather
    than to a crash."""
    items, name, label = app_module.parse_ai_response(
        '{"service_name":"Youth","items":[{"title":"Welcome"}]}')
    assert label == ""
    assert name == "Youth"
    items, name, label = app_module.parse_ai_response('[{"title":"Welcome"}]')
    assert (name, label) == ("", "")


def test_the_prompt_asks_for_the_service_label(app_module):
    """The field only exists if the prompt asks for it. Pinned because the
    prompt is user-editable and a customised prompt that drops the field
    degrades to filename+text hinting rather than breaking."""
    assert "service_type" in app_module.DEFAULT_PROMPT


# ── Parse route: decline end-to-end, and say so ──────────────────────────────

def _reply(service_type, titles=("Welcome",)):
    return json.dumps({
        "service_name": "Test Event",
        "service_type": service_type,
        "items": [{"title": t, "type": "mc_on_stage"} for t in titles]})


@pytest.fixture
def pp_parse(client, monkeypatch):
    """Parse client whose ProPresenter holds ONE template — the Youth
    library — with a "Welcome" slide in it that would match by title."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text", lambda _p: "Welcome")
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: None)
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists",
                        lambda *_a, **_k: list(YOUTH_ONLY))
    monkeypatch.setattr(parse_mod, "fetch_pp_playlist_items", lambda *_a, **_k: [
        {"id": {"name": "Welcome", "uuid": "hdr-w", "index": 0},
         "type": "header", "header_color": {}},
        {"id": {"name": "Welcome Slide", "uuid": "it-w", "index": 1},
         "type": "media", "target_uuid": "pres-w"}])
    return client


def _parse(client, reply):
    import requests

    class _R:
        status_code = 200
        def json(self):
            return {"model": "test/model:free",
                    "choices": [{"message": {"content": reply}}]}
        def raise_for_status(self):
            return None

    orig = requests.post
    requests.post = lambda *a, **k: _R()
    try:
        return client.post("/api/upload_and_parse", data={
            "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "runsheet.pdf"),
            "or_key": "sk-or-test", "or_model": "test/model:free",
        }, content_type="multipart/form-data").get_json()
    finally:
        requests.post = orig


def test_parse_declines_and_reports_which_service_it_saw(pp_parse):
    """The whole point: youth media must not land on a young adults
    playlist, and the operator must be told why nothing populated."""
    body = _parse(pp_parse, _reply("Young Adults"))
    assert body["template"]["declined"] is True
    assert body["template"]["uuid"] == ""
    assert body["template"]["service_label"] == "Young Adults"
    # And no item kept a link to the youth template.
    assert all(it.get("library_match") is None for it in body["items"])


def test_parse_matches_when_the_service_lines_up(pp_parse):
    """Same machine, same single template — a youth runsheet still gets
    it. A decline rule that declines everything is not a fix."""
    body = _parse(pp_parse, _reply("Youth"))
    assert body["template"]["declined"] is False
    assert body["template"]["uuid"] == "u-youth"
    assert body["template"]["name"] == "Youth Service - Library"
    assert any(it.get("library_match") for it in body["items"])


def test_declining_is_not_an_error_when_pp_has_no_templates(client,
                                                            monkeypatch):
    """"There will be services where there isn't a template or a library
    built, and that has to be okay." Nothing to decline, so nothing to
    report — `declined` stays false and the UI shows no banner."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text", lambda _p: "Welcome")
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: None)
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda *_a, **_k: [])
    body = _parse(client, _reply("Young Adults"))
    assert body["template"]["declined"] is False
    assert body["template"]["uuid"] == ""


def test_a_pinned_template_is_never_second_guessed(pp_parse, monkeypatch):
    """The dropdown is an explicit instruction. If the operator pinned the
    Youth library, a "Young Adults" label does not overrule them."""
    from propresenterrunsheet import settings as pp_settings
    monkeypatch.setattr(pp_settings, "load_settings",
                        lambda: {"template_playlist_uuid": "u-youth"})
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "load_settings",
                        lambda: {"template_playlist_uuid": "u-youth"})
    body = _parse(pp_parse, _reply("Young Adults"))
    assert body["template"]["declined"] is False
    assert body["template"]["uuid"] == "u-youth"


# ── Create route: the decline has to survive to the last step ────────────────

@pytest.fixture
def pp_create(monkeypatch):
    """Fake PP for /api/create_playlist whose only template is the Youth
    library, holding a slide that WOULD match "Welcome" by title."""
    import requests

    class _R:
        def __init__(self, status=200, payload=None):
            self.status_code = status
            self._payload = payload or {}
            self.text = ""
        def json(self):
            return self._payload
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _R(200, {"id": {"uuid": "NEW",
                                                        "name": "x"}}))
    monkeypatch.setattr(requests, "put", lambda *a, **k: _R(204))
    monkeypatch.setattr(playlist_mod, "fetch_media_bin",
                        lambda _b: [{"uuid": "BIN-W", "name": "Welcome Slide"}])
    monkeypatch.setattr(playlist_mod, "fetch_pp_playlists",
                        lambda *_a, **_k: list(YOUTH_ONLY))
    monkeypatch.setattr(playlist_mod, "fetch_pp_playlist_items",
                        lambda *_a, **_k: [
                            {"id": {"name": "Welcome", "uuid": "h", "index": 0},
                             "type": "header", "header_color": {}},
                            {"id": {"name": "Welcome Slide", "uuid": "i",
                                    "index": 1},
                             "type": "media", "target_uuid": "pres-w"}])


def _create(client, service_label):
    matched = [{"parsed": {"type": "mc_on_stage", "title": "Welcome",
                           "library_match": None},
                "match": None}]
    return client.post("/api/create_playlist", json={
        "host": "localhost", "port": "1", "name": "Young Adults 24 Aug",
        "matched": matched, "create_timers": False,
        "service_label": service_label}).get_json()


def test_create_does_not_re_attach_a_template_parse_declined(client,
                                                             pp_create):
    """The trap. Create re-runs the template match for items that missed
    it at parse time — a real rescue for "ProPresenter was closed when I
    parsed". Given the service label, it must reach the same verdict as
    parse instead of quietly restoring the youth media."""
    body = _create(client, "Young Adults")
    assert body.get("ok") is True
    assert body.get("template_relinked", 0) == 0


def test_create_still_rescues_links_when_the_service_matches(client,
                                                            pp_create):
    """The same call, one word different — the rescue path is intact."""
    body = _create(client, "Youth")
    assert body.get("ok") is True
    assert body.get("template_relinked", 0) == 1
