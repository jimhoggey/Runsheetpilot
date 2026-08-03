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


@pytest.fixture
def parse_client(client, monkeypatch):
    """Test client with the PDF extractor and OpenRouter call stubbed out, so
    these tests exercise only the response-handling logic."""
    import propresenterrunsheet.routes.parse as parse_mod

    monkeypatch.setattr(parse_mod, "extract_pdf_text",
                        lambda _p: "10:00 Welcome\n10:05 Worship")
    # Keep ProPresenter out of it — template lookup is best-effort anyway.
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no PP")))
    # No live catalogue fetch: the suite must not depend on openrouter.ai being
    # reachable, and returning None makes resolve_model honour the model id the
    # test passes in rather than substituting today's auto-pick.
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: None)
    return client


def _post(client, ai_reply, model="test/model:free"):
    import requests

    class _Session:
        @staticmethod
        def post(*_a, **_k):
            return _FakeResponse(ai_reply, model=model)

    import propresenterrunsheet.routes.parse as parse_mod
    orig = requests.post
    requests.post = _Session.post
    try:
        return client.post(
            "/api/upload_and_parse",
            data={"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "service.pdf"),
                  "or_key": "sk-or-test", "or_model": model},
            content_type="multipart/form-data")
    finally:
        requests.post = orig


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


# ── the happy path still works ───────────────────────────────────────────────

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
