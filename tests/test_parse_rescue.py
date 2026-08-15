"""Route-level test: the timed-row guard inside /api/upload_and_parse.

tests/test_timed_rows.py proves the guard's mechanics; this proves the
WIRING — that the route actually runs it, on the raw text, before the
template-matching loop, and reports the count. A guard that exists but
isn't called is how the 14 Aug regression would happen again.
"""
import io


RAW = ("5:00 PM 30 Team Setup & Band practice\n"
       "5:30 PM 30 Team prayer + Meeting\n"
       "6:00 PM 25 Youth Arrival + Hangout\n"
       "6:25 PM 5 MC Welcome: Ollie & Elliot\n")

# The model returns ONLY the MC Welcome row — the 14 Aug failure shape.
MODEL_REPLY = ('{"service_name":"Youth","items":['
               '{"title":"MC Welcome: Ollie & Elliot","type":"mc_on_stage",'
               '"start_time":"6:25 PM","duration_min":5}]}')


def _parse(client):
    import requests

    class _Resp:
        status_code = 200
        def json(self):
            return {"model": "test/model:free",
                    "choices": [{"message": {"content": MODEL_REPLY}}]}
        def raise_for_status(self):
            return None

    orig = requests.post
    requests.post = lambda *a, **k: _Resp()
    try:
        return client.post(
            "/api/upload_and_parse",
            data={"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "youth.pdf"),
                  "or_key": "sk-or-test", "or_model": "test/model:free"},
            content_type="multipart/form-data")
    finally:
        requests.post = orig


def test_dropped_rows_come_back_through_the_route(parse_client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text", lambda _p: RAW)

    body = _parse(parse_client).get_json()
    assert body["rescued_rows"] == 3
    assert [i["title"] for i in body["items"]] == [
        "Team Setup & Band practice",
        "Team prayer + Meeting",
        "Youth Arrival + Hangout",
        "MC Welcome: Ollie & Elliot",
    ]


def test_rescued_rows_have_clamped_types_and_cues(parse_client, monkeypatch):
    """Rescued items flow through the same post-processing as parsed
    ones — the type clamp and the Service Mate cue fill run AFTER the
    guard, so a synthesized row is indistinguishable downstream."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text", lambda _p: RAW)

    items = _parse(parse_client).get_json()["items"]
    rescued = items[0]
    assert rescued["type"] == "other"
    assert rescued["start_time"] == "5:00 PM"
    assert rescued["duration_min"] == 30
    # The cue fill ran: all three Service Mate roles are populated.
    assert set(rescued["cues"].keys()) >= {"screen", "sound", "lights"}
    assert all(v.strip() for v in rescued["cues"].values())
    # library_match is present-and-None like every unmatched item.
    assert rescued.get("library_match") is None


def test_full_reply_rescues_nothing(parse_client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text",
                        lambda _p: "6:25 PM 5 MC Welcome: Ollie & Elliot\n")

    body = _parse(parse_client).get_json()
    assert body["rescued_rows"] == 0
    assert len(body["items"]) == 1
