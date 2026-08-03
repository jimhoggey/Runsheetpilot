"""Tests for /api/upload_and_parse's handling of unusable AI responses.

These cover two real failures seen with `openrouter/free`, which routes each
request to a randomly chosen free model. About one pick in eight was
`nvidia/nemotron-3.5-content-safety:free` — a safety classifier that ignores
the prompt and answers with a verdict instead of a runsheet.

1. It replied `User Safety: safe`. That is not JSON, so `parse_ai_response`
   raised and the operator saw the raw decoder message:
   "AI response was not valid JSON: Expecting value: line 1 column 1 (char 0)".

2. The worse variant: had it replied `{"safety": "safe"}` — valid JSON with no
   `items` key — nothing raised at all. The route carried on to seed Service
   Mate state with zero items, and `_write_runsheet_state` is an unconditional
   overwrite, so a junk reply silently wiped the live clock state mid-service.

They also cover the misleading 401 (confirmed live 2026-08-03): OpenRouter
returns HTTP 401 when an *upstream provider's* credentials break, with the
provider named in `error.metadata.provider_name` — the operator's own key is
fine (GET /api/v1/key returned 200 at the same moment). The old handler mapped
every 401 to "check the key in the sidebar", sending people off to rotate a
working key. During that outage provider Darkbloom broke several free models
at once while other providers' models kept working, so the route now retries
once on the next-ranked free model before surfacing an honest error.
"""
import io
import json

import pytest

from propresenterrunsheet.service_mate import state as sm_state


class _FakeResponse:
    def __init__(self, content, status_code=200, model="test/model:free"):
        self.status_code = status_code
        self._content = content
        self._model = model

    def json(self):
        return {"model": self._model,
                "choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        return None


class _FakeErrorResponse:
    """A non-2xx OpenRouter reply. `body` is what .json() returns; body=None
    models an unparseable (non-JSON) error page."""

    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self):
        import requests
        raise requests.exceptions.HTTPError(f"{self.status_code} Client Error")


def _provider_error_body(provider="Darkbloom", code=401):
    """The exact failure shape observed live on 2026-08-03: OpenRouter echoes
    an upstream provider's error status, naming the provider in the metadata.
    `previous_errors` lists the other providers it tried first. A *genuine*
    key rejection carries no provider metadata — the request dies at
    OpenRouter's own door before any provider is involved."""
    return {"error": {
        "code": code,
        "message": "Provider returned error",
        "metadata": {
            "provider_name": provider,
            "raw": "401 {'error': {'message': 'invalid API key'}}",
            "previous_errors": [
                {"code": 429, "provider_name": "Google AI Studio"}],
        },
    }}


# The `parse_client` fixture lives in conftest.py so other test modules
# (test_item_types.py) can drive the parse route too.


def _post_responses(client, responses, model="test/model:free", calls=None):
    """Upload the fake PDF with OpenRouter's replies fully scripted.

    `responses` are handed out one per POST, in order — the retry tests need
    a failure followed by a success. Pass a `calls` list to capture each
    request's JSON payload, so a test can assert which model was asked."""
    import requests

    responses = list(responses)
    if calls is None:
        calls = []

    def _fake_post(*_a, **kw):
        calls.append(kw.get("json") or {})
        return responses.pop(0)

    orig = requests.post
    requests.post = _fake_post
    try:
        return client.post(
            "/api/upload_and_parse",
            data={"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "service.pdf"),
                  "or_key": "sk-or-test", "or_model": model},
            content_type="multipart/form-data")
    finally:
        requests.post = orig


def _post(client, ai_reply, model="test/model:free"):
    return _post_responses(
        client, [_FakeResponse(ai_reply, model=model)], model=model)


def _seed_live_state(tmp_path):
    """Put a real runsheet on disk, as if a service were already running."""
    live = {"service_name": "Sunday Morning",
            "items": [{"type": "song", "title": "Build My Life"}],
            "current_index": 0}
    sm_state.RUNSHEET_STATE_FILE.write_text(json.dumps(live))
    return live


# ── non-JSON reply ───────────────────────────────────────────────────────────

def test_non_json_reply_reports_which_model_replied_and_what_it_said(
        parse_client, isolated_state):
    """The operator needs to know the model misbehaved — not see a decoder
    error. The old message named neither the model nor the reply."""
    r = _post(parse_client, "User Safety: safe",
              model="nvidia/nemotron-3.5-content-safety:free")
    body = r.get_json()
    assert "error" in body
    assert "nvidia/nemotron-3.5-content-safety:free" in body["error"]
    assert "User Safety: safe" in body["error"]
    assert "Expecting value" not in body["error"]


def test_non_json_reply_does_not_wipe_live_runsheet_state(
        parse_client, isolated_state):
    _seed_live_state(isolated_state)
    _post(parse_client, "User Safety: safe")
    kept = json.loads(sm_state.RUNSHEET_STATE_FILE.read_text())
    assert kept["service_name"] == "Sunday Morning"
    assert len(kept["items"]) == 1


# ── valid JSON, but not a runsheet ───────────────────────────────────────────

def test_valid_json_without_items_is_rejected(parse_client, isolated_state):
    """`{"safety": "safe"}` parses fine and yields zero items. That must be an
    error, not a silent success."""
    r = _post(parse_client, '{"safety": "safe"}')
    body = r.get_json()
    assert "error" in body, "a reply with no runsheet items must not succeed"


def test_valid_json_without_items_does_not_wipe_live_runsheet_state(
        parse_client, isolated_state):
    """The bug that mattered: a junk-but-parseable reply overwriting the live
    Service Mate state mid-service, with nothing shown to the operator."""
    _seed_live_state(isolated_state)
    _post(parse_client, '{"safety": "safe"}')
    kept = json.loads(sm_state.RUNSHEET_STATE_FILE.read_text())
    assert kept["service_name"] == "Sunday Morning", \
        "live runsheet state was overwritten by a reply containing no items"
    assert len(kept["items"]) == 1


def test_empty_items_array_is_rejected(parse_client, isolated_state):
    r = _post(parse_client, '{"service_name": "X", "items": []}')
    assert "error" in r.get_json()


# ── provider-side failures wearing OpenRouter's status codes ─────────────────

def test_provider_side_401_is_not_blamed_on_the_api_key(
        parse_client, isolated_state):
    """The confirmed misdiagnosis: provider Darkbloom's credentials broke,
    OpenRouter relayed it as a 401, and the app told the operator to go check
    a key that was verifiably fine. The message must name the real culprit
    and explicitly clear the key."""
    calls = []
    r = _post_responses(
        parse_client,
        [_FakeErrorResponse(401, _provider_error_body())],
        model="google/gemma-4-26b-a4b-it:free", calls=calls)
    assert r.status_code == 200
    err = r.get_json()["error"]
    assert "google/gemma-4-26b-a4b-it:free" in err
    assert "Darkbloom" in err
    assert "API key is fine" in err
    assert "rejected the API key" not in err
    # Catalogue is offline in this fixture — no backup model is knowable,
    # so there must be exactly one OpenRouter call, not a blind retry.
    assert len(calls) == 1


def test_provider_side_401_retries_once_with_the_next_ranked_model(
        parse_client, isolated_state, monkeypatch):
    """During the outage the neighbouring free models on other providers kept
    working — so before surfacing anything, try the model ranked just below
    the failing one. The operator gets a parsed runsheet, not an apology."""
    import propresenterrunsheet.routes.parse as parse_mod
    from tests.test_model_catalogue import _catalogue, _model

    cat = _catalogue(
        _model("google/gemma-4-26b-a4b-it:free", ctx=262144),
        _model("nvidia/nemotron-3-super-120b-a12b:free", ctx=128000),
    )
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: cat)

    good = json.dumps({"service_name": "Sunday Morning",
                       "items": [{"type": "song", "title": "Build My Life"}]})
    calls = []
    r = _post_responses(
        parse_client,
        [_FakeErrorResponse(401, _provider_error_body()),
         _FakeResponse(good, model="nvidia/nemotron-3-super-120b-a12b:free")],
        model="google/gemma-4-26b-a4b-it:free", calls=calls)
    body = r.get_json()
    assert "error" not in body, body
    assert len(body["items"]) == 1
    assert [c.get("model") for c in calls] == [
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free"]
    # The retry's parse is a real success — it must seed state like any other.
    written = json.loads(sm_state.RUNSHEET_STATE_FILE.read_text())
    assert written["service_name"] == "Sunday Morning"


def test_provider_failure_on_both_models_names_both(
        parse_client, isolated_state, monkeypatch):
    """Darkbloom served several free models at once, so the backup can fail
    the same way. Exactly one retry, then an error that names both models —
    otherwise "pick a different model" sends the operator to the one we
    already tried — and still doesn't blame the key."""
    import propresenterrunsheet.routes.parse as parse_mod
    from tests.test_model_catalogue import _catalogue, _model

    cat = _catalogue(
        _model("google/gemma-4-26b-a4b-it:free", ctx=262144),
        _model("openai/gpt-oss-20b:free", ctx=128000),
    )
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: cat)

    calls = []
    r = _post_responses(
        parse_client,
        [_FakeErrorResponse(401, _provider_error_body()),
         _FakeErrorResponse(401, _provider_error_body())],
        model="google/gemma-4-26b-a4b-it:free", calls=calls)
    err = r.get_json()["error"]
    assert "google/gemma-4-26b-a4b-it:free" in err
    assert "openai/gpt-oss-20b:free" in err
    assert "Darkbloom" in err
    assert "API key is fine" in err
    assert "rejected the API key" not in err
    assert len(calls) == 2, "exactly one retry — never a loop"


def test_provider_side_401_with_no_other_usable_model_does_not_retry(
        parse_client, isolated_state, monkeypatch):
    """A catalogue that offers nothing but the failing model itself: retrying
    would re-ask the same broken provider, so don't."""
    import propresenterrunsheet.routes.parse as parse_mod
    from tests.test_model_catalogue import _catalogue, _model

    cat = _catalogue(_model("google/gemma-4-26b-a4b-it:free", ctx=262144))
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: cat)

    calls = []
    r = _post_responses(
        parse_client,
        [_FakeErrorResponse(401, _provider_error_body())],
        model="google/gemma-4-26b-a4b-it:free", calls=calls)
    err = r.get_json()["error"]
    assert "Darkbloom" in err
    assert "API key is fine" in err
    assert len(calls) == 1


def test_provider_side_429_gets_the_provider_message_not_a_500(
        parse_client, isolated_state):
    """Provider failures wear other status codes too (Google AI Studio's
    429 appeared in the same outage's previous_errors). Any error status
    carrying provider metadata deserves the honest message — not the generic
    raise_for_status 500."""
    r = _post_responses(
        parse_client,
        [_FakeErrorResponse(
            429, _provider_error_body(provider="Google AI Studio", code=429))],
        model="test/model:free")
    assert r.status_code == 200
    err = r.get_json()["error"]
    assert "Google AI Studio" in err
    assert "429" in err
    assert "API key is fine" in err


def test_bare_401_still_reports_a_key_problem(parse_client, isolated_state):
    """A 401 with no provider metadata IS a key problem — the request died at
    OpenRouter's own door. The original message must survive the fix.
    (Pinning test: this passed before the provider handling existed.)"""
    r = _post_responses(
        parse_client,
        [_FakeErrorResponse(401, {"error": {
            "code": 401, "message": "No auth credentials found"}})])
    err = r.get_json()["error"]
    assert "rejected the API key" in err
    assert "API key is fine" not in err


def test_bare_401_with_unparseable_body_still_reports_a_key_problem(
        parse_client, isolated_state):
    """Belt and braces: a 401 whose body isn't JSON at all (proxy error page,
    HTML) must fall back to the key message, not crash into a 500.
    (Pinning test for the resp.json() call the provider check introduces.)"""
    r = _post_responses(parse_client, [_FakeErrorResponse(401, body=None)])
    err = r.get_json()["error"]
    assert "rejected the API key" in err


# ── the happy path still works ───────────────────────────────────────────────

def test_flat_template_objects_attach_to_matching_items(
        parse_client, isolated_state, monkeypatch):
    """The template-SUNDAY case: a FLAT template playlist (no section
    headers) is a repository of named objects. "Countdown - Start 9:27am"
    must come back wired to the template's "Countdown " loop (note PP's
    trailing space), wrapped in the section shape the payload builder
    already expands as header + item-underneath."""
    from tests.test_template_objects import PP_ITEMS
    import propresenterrunsheet.routes.parse as parse_mod

    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda *_a, **_k:
                        [{"name": "template SUNDAY", "uuid": "TPL", "index": 0}])
    monkeypatch.setattr(parse_mod, "fetch_pp_playlist_items",
                        lambda *_a, **_k: PP_ITEMS)

    reply = json.dumps({"service_name": "Sunday", "items": [
        {"type": "other",        "title": "Countdown - Start 9:27am"},
        {"type": "announcement", "title": "Welcome and Connection Cards"},
        {"type": "song",         "title": "Worship Medley"},
        {"type": "other",        "title": "Go Live - Start Online Streaming"},
    ]})
    body = _post(parse_client, reply).get_json()
    assert "error" not in body, body
    items = body["items"]

    countdown = items[0]["library_match"]
    assert countdown and countdown["header"]["name"] == "Countdown"
    assert len(countdown["items"]) == 1
    assert countdown["items"][0]["target_uuid"] == "3914716D"
    assert countdown["items"][0]["duration"] == 15

    welcome = items[1]["library_match"]
    assert welcome and welcome["header"]["name"] == "Welcome"

    # Songs stay with the picker flow — the template's "Worship" slide
    # must NOT hijack a song titled "Worship Medley".
    assert not items[2].get("library_match")

    # Nothing in the template explains this title — header only.
    assert not items[3].get("library_match")


def test_sectioned_template_still_wins_over_object_fallback(
        parse_client, isolated_state, monkeypatch):
    """Templates organised with headers keep the richer behaviour: an
    LLM-tagged section (all its media) beats the one-object fallback."""
    import propresenterrunsheet.routes.parse as parse_mod

    sectioned = [
        {"id": {"uuid": "H1", "name": "Culture", "index": 0},
         "type": "header", "target_uuid": ""},
        {"id": {"uuid": "M1", "name": "Culture Bumper", "index": 1},
         "type": "media", "target_uuid": "A1", "destination": "presentation"},
        {"id": {"uuid": "M2", "name": "Culture Lower Third", "index": 2},
         "type": "media", "target_uuid": "A2", "destination": "presentation"},
    ]
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda *_a, **_k:
                        [{"name": "Sunday Library", "uuid": "TPL", "index": 0}])
    monkeypatch.setattr(parse_mod, "fetch_pp_playlist_items",
                        lambda *_a, **_k: sectioned)

    reply = json.dumps({"service_name": "Sunday", "items": [
        {"type": "announcement", "title": "Culture Moment",
         "library_match": "Culture"},
    ]})
    body = _post(parse_client, reply).get_json()
    lib = body["items"][0]["library_match"]
    assert lib["header"]["name"] == "Culture"
    assert len(lib["items"]) == 2, \
        "the LLM-tagged section (2 slides) must win over a 1-object match"


def test_valid_runsheet_still_parses_and_seeds_state(
        parse_client, isolated_state):
    reply = json.dumps({"service_name": "Sunday Morning",
                        "items": [{"type": "song", "title": "Build My Life"},
                                  {"type": "sermon", "title": "King Jesus"}]})
    r = _post(parse_client, reply)
    body = r.get_json()
    assert "error" not in body, body
    assert len(body["items"]) == 2
    assert body["suggested_name"] == "Sunday Morning"
    written = json.loads(sm_state.RUNSHEET_STATE_FILE.read_text())
    assert len(written["items"]) == 2
