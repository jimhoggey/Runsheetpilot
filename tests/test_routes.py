"""Flask route tests using the Flask test client. State files (runsheet,
clocks) are redirected into a tmpdir via the `isolated_state` fixture in
conftest.py so these tests don't touch the user's real config."""
import json


JPEG_MAGIC = b"\xff\xd8\xff"


# ── Health / version ─────────────────────────────────────────────────────────

def test_health_returns_ok(client, app_module):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["version"] == app_module.VERSION
    assert "platform" in body


# ── Runsheet state lifecycle ─────────────────────────────────────────────────

def test_runsheet_state_starts_empty(client):
    r = client.get("/api/runsheet/state")
    assert r.status_code == 200
    assert r.get_json() == {}


def test_runsheet_state_post_then_get_roundtrips(client):
    payload = {
        "service_name": "Sunday Service",
        "items": [
            {"type": "song",   "title": "Build My Life", "duration_min": 5},
            {"type": "sermon", "title": "King Jesus",    "duration_min": 30},
        ],
        "current_index": 0,
    }
    p = client.post("/api/runsheet/state", json=payload)
    assert p.status_code == 200
    assert p.get_json()["ok"] is True

    g = client.get("/api/runsheet/state").get_json()
    assert g["service_name"] == "Sunday Service"
    assert len(g["items"]) == 2
    # Cue rule-fallback should have been applied so the device never goes blank
    assert g["items"][0]["cues"]["screen"]
    assert g["items"][0]["cues"]["sound"]
    assert g["items"][0]["cues"]["lights"]


def test_runsheet_cue_advances_index(client):
    client.post("/api/runsheet/state", json={
        "items": [{"type": "song", "title": "a"},
                  {"type": "song", "title": "b"},
                  {"type": "song", "title": "c"}],
    })
    r = client.post("/api/runsheet/cue", json={"delta": 1})
    assert r.status_code == 200
    assert r.get_json()["current_index"] == 1
    r = client.post("/api/runsheet/cue", json={"delta": 1})
    assert r.get_json()["current_index"] == 2
    # Can't go past the last item
    r = client.post("/api/runsheet/cue", json={"delta": 1})
    assert r.get_json()["current_index"] == 2


def test_runsheet_cue_jumps_to_explicit_index(client):
    client.post("/api/runsheet/state", json={
        "items": [{"type": "song", "title": str(i)} for i in range(5)],
    })
    r = client.post("/api/runsheet/cue", json={"index": 3})
    assert r.get_json()["current_index"] == 3


def test_runsheet_cue_clamps_negative(client):
    client.post("/api/runsheet/state", json={
        "items": [{"type": "song", "title": "a"}],
    })
    r = client.post("/api/runsheet/cue", json={"delta": -5})
    assert r.get_json()["current_index"] == 0


def test_runsheet_cue_errors_when_no_state(client):
    r = client.post("/api/runsheet/cue", json={"delta": 1})
    assert r.status_code == 400


def test_runsheet_state_delete(client):
    client.post("/api/runsheet/state", json={"items": [{"title": "a"}]})
    d = client.delete("/api/runsheet/state")
    assert d.status_code == 200
    assert client.get("/api/runsheet/state").get_json() == {}


# ── Clocks config ────────────────────────────────────────────────────────────

def test_clocks_get_returns_default(client, app_module):
    r = client.get("/api/clocks").get_json()
    assert r["enabled"] is True
    assert r["brightness"] == 70
    assert {c["role"] for c in r["clocks"]} == {"screen", "sound", "lights"}


def test_clocks_post_persists_verbosity(client):
    payload = {
        "clocks": [
            {"id": "screen", "role": "screen", "ip": "192.168.1.50",
             "name": "Screen", "verbosity": "detailed"},
            {"id": "sound", "role": "sound", "ip": "",
             "name": "Sound", "verbosity": "compact"},
            {"id": "lights", "role": "lights", "ip": "",
             "name": "Lights", "verbosity": "compact"},
        ],
        "brightness": 80,
        "enabled": True,
    }
    p = client.post("/api/clocks", json=payload)
    assert p.status_code == 200

    g = client.get("/api/clocks").get_json()
    assert g["brightness"] == 80
    by_id = {c["id"]: c for c in g["clocks"]}
    assert by_id["screen"]["verbosity"] == "detailed"
    assert by_id["screen"]["ip"] == "192.168.1.50"
    assert by_id["sound"]["verbosity"] == "compact"


def test_clocks_post_rejects_unknown_verbosity_with_default(client):
    """An unrecognised verbosity value should fall back to compact rather
    than 500-ing or persisting garbage."""
    p = client.post("/api/clocks", json={
        "clocks": [{"id": "screen", "role": "screen", "ip": "",
                    "name": "S", "verbosity": "huge_text_yo"}],
    })
    assert p.status_code == 200
    g = client.get("/api/clocks").get_json()
    assert g["clocks"][0]["verbosity"] == "compact"


def test_clock_probe_with_no_ip_returns_helpful_error(client):
    r = client.post("/api/clocks/sound/probe")
    body = r.get_json()
    assert body["ok"] is False
    assert "no IP" in body["error"]


def test_clock_test_with_no_ip_returns_helpful_error(client):
    r = client.post("/api/clocks/lights/test")
    body = r.get_json()
    assert body["ok"] is False


def test_clock_probe_unknown_id_404s(client):
    r = client.post("/api/clocks/no_such_role/probe")
    assert r.status_code == 404


# ── Inline clock preview ─────────────────────────────────────────────────────

def test_preview_returns_jpeg_for_each_role(client):
    for role in ("screen", "sound", "lights"):
        r = client.get(f"/api/clocks/preview?role={role}")
        assert r.status_code == 200
        assert r.mimetype == "image/jpeg"
        assert r.data.startswith(JPEG_MAGIC)


def test_preview_compact_and_detailed_differ(client):
    a = client.get("/api/clocks/preview?role=screen&verbosity=compact").data
    b = client.get("/api/clocks/preview?role=screen&verbosity=detailed").data
    assert a.startswith(JPEG_MAGIC)
    assert b.startswith(JPEG_MAGIC)
    assert a != b  # different layouts → different bytes


def test_preview_unknown_verbosity_falls_back_to_default(client):
    r = client.get("/api/clocks/preview?role=screen&verbosity=invalid")
    assert r.status_code == 200
    assert r.data.startswith(JPEG_MAGIC)


def test_preview_unknown_role_falls_back_to_screen(client):
    r = client.get("/api/clocks/preview?role=ghost")
    assert r.status_code == 200
    assert r.data.startswith(JPEG_MAGIC)


# ── Index page sanity ────────────────────────────────────────────────────────

def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<!DOCTYPE html>" in r.data
    # Service Mate UI is on the main page
    assert b"Service Mate" in r.data
