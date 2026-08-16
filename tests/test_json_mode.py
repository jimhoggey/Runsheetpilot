"""Tests for JSON mode on the OpenRouter request.

The model picker has always filtered for models that advertise
`structured_outputs`, but the request never actually ASKED for JSON —
`response_format` was missing, so that filter bought nothing at request
time. Asking is the cheapest reliability lever there is: with JSON mode a
compliant model cannot wrap the answer in prose or markdown fences, which
is one of the two ways a parse fails outright.

It must degrade safely, though. A model that advertises the capability
and still 400s on `response_format` (it happens on free tiers) must not
kill the parse — the request is retried once WITHOUT it, and the existing
regex-tolerant parser handles the reply as before.
"""
import io
import json


GOOD = ('{"service_name":"Youth","items":['
        '{"title":"Welcome","type":"mc_on_stage","start_time":"6:25 PM",'
        '"duration_min":5}]}')


class _Resp:
    def __init__(self, status=200, content=GOOD, body=None):
        self.status_code = status
        self._content = content
        self._body = body
        self.text = json.dumps(body) if body else content

    def json(self):
        if self._body is not None:
            return self._body
        return {"model": "test/model:free",
                "choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}")


def _post(client, responses):
    import requests
    responses = list(responses)
    calls = []

    def fake(*_a, **kw):
        calls.append(kw.get("json") or {})
        return responses.pop(0)

    orig = requests.post
    requests.post = fake
    try:
        r = client.post(
            "/api/upload_and_parse",
            data={"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "s.pdf"),
                  "or_key": "sk-or-test", "or_model": "test/model:free"},
            content_type="multipart/form-data")
        return r, calls
    finally:
        requests.post = orig


def test_request_asks_for_json_mode(parse_client):
    _r, calls = _post(parse_client, [_Resp()])
    assert calls[0].get("response_format") == {"type": "json_object"}


def test_a_400_blaming_response_format_retries_without_it(parse_client):
    """The provider rejects the parameter; the same model is asked again
    plainly, and the parse succeeds on that reply."""
    rejected = _Resp(status=400, body={"error": {
        "code": 400,
        "message": "response_format is not supported by this model"}})
    r, calls = _post(parse_client, [rejected, _Resp()])
    assert r.status_code == 200
    assert "error" not in r.get_json()
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert calls[1]["model"] == calls[0]["model"]


def test_a_400_for_another_reason_is_not_retried_as_json_mode(parse_client):
    """Only a rejection that names the parameter earns the plain retry —
    an unrelated 400 must not be masked by a silent second request."""
    other = _Resp(status=400, body={"error": {
        "code": 400, "message": "context length exceeded"}})
    r, calls = _post(parse_client, [other])
    assert len(calls) == 1
    assert "error" in r.get_json()


def test_json_mode_survives_the_provider_failover(parse_client, monkeypatch):
    """The next-ranked-model retry also asks for JSON mode."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "next_usable_model",
                        lambda *_a, **_k: "backup/model:free")
    provider_fail = _Resp(status=401, body={"error": {
        "code": 401, "message": "Provider returned error",
        "metadata": {"provider_name": "Darkbloom", "raw": "x"}}})
    _r, calls = _post(parse_client, [provider_fail, _Resp()])
    assert len(calls) == 2
    assert calls[1]["model"] == "backup/model:free"
    assert calls[1].get("response_format") == {"type": "json_object"}
