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

def test_settings_include_sm_hide_default(client):
    # The "Hide Service Mate" toggle must exist for fresh installs and
    # default to visible — hiding a feature by default would look broken.
    s = client.get("/api/settings").get_json()
    assert s["sm_hide"] is False


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
    """Fresh installs default to enabled=False so Service Mate is opt-in
    for users who don't own a GeekMagic clock — they get a quiet card +
    no background polling until they flip the master switch."""
    r = client.get("/api/clocks").get_json()
    assert r["enabled"] is False
    assert r["brightness"] == 70
    assert {c["role"] for c in r["clocks"]} == {"screen", "sound", "lights"}


def test_clocks_existing_file_without_enabled_key_treated_as_enabled(
        client, isolated_state, app_module):
    """Back-compat: operators who already had a clocks.json from before the
    master-switch existed get enabled=True so Service Mate keeps working
    after the upgrade. Only fresh installs (no clocks.json) inherit the
    new enabled=False default."""
    import json
    from propresenterrunsheet.service_mate import state as sm_state
    legacy = {"clocks": [{"id": "screen", "role": "screen", "ip": "",
                          "name": "S", "verbosity": "compact"}],
              "brightness": 50}  # NO "enabled" key — pre-switch file
    sm_state.CLOCKS_CONFIG_FILE.write_text(json.dumps(legacy))
    r = client.get("/api/clocks").get_json()
    assert r["enabled"] is True
    assert r["brightness"] == 50


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


def test_clock_probe_with_no_ip_returns_helpful_error(sm_enabled):
    r = sm_enabled.post("/api/clocks/sound/probe")
    body = r.get_json()
    assert body["ok"] is False
    assert "no IP" in body["error"]


def test_clock_test_with_no_ip_returns_helpful_error(sm_enabled):
    r = sm_enabled.post("/api/clocks/lights/test")
    body = r.get_json()
    assert body["ok"] is False


def test_clock_probe_unknown_id_404s(sm_enabled):
    r = sm_enabled.post("/api/clocks/no_such_role/probe")
    assert r.status_code == 404


def test_clock_action_routes_409_when_master_switch_off(client):
    """Master switch off → standby / preview / probe / test all return
    409 with a clear "Flip the master switch" hint. Keeps off meaning off
    even for callers hitting the API directly (curl, scripts)."""
    # client (no sm_enabled) starts disabled by default.
    for path, method in [
        ("/api/clocks/standby",       "POST"),
        ("/api/clocks/preview",       "GET"),
        ("/api/clocks/screen/probe",  "POST"),
        ("/api/clocks/screen/test",   "POST"),
    ]:
        fn = client.post if method == "POST" else client.get
        r = fn(path)
        assert r.status_code == 409, f"{method} {path} should 409 when SM off"
        body = r.get_json()
        assert body["ok"] is False
        assert "master switch" in body["error"].lower() \
            or "disabled" in body["error"].lower()


# ── Standby (pre-service waiting page) ───────────────────────────────────────

def test_standby_sets_flag_and_clears_items(sm_enabled):
    client = sm_enabled  # rest of test body uses `client`
    # Seed a real runsheet first…
    client.post("/api/runsheet/state", json={
        "items": [{"type": "song", "title": "a"},
                  {"type": "song", "title": "b"}],
        "current_index": 1,
    })
    # …then hit standby.
    r = client.post("/api/clocks/standby")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    state = client.get("/api/runsheet/state").get_json()
    assert state["standby"] is True
    assert state["items"] == []


def test_standby_invalidates_push_cache(sm_enabled, app_module):
    """The endpoint must clear `_CLOCKS_LOOP_LAST_PUSHED` so the daemon
    re-pushes on the next tick instead of waiting for the 40 s anti-bitrot
    refresh."""
    client = sm_enabled
    app_module._CLOCKS_LOOP_LAST_PUSHED["screen"] = ("deadbeef", 12345.0)
    client.post("/api/clocks/standby")
    assert "screen" not in app_module._CLOCKS_LOOP_LAST_PUSHED


def test_loading_runsheet_clears_standby(sm_enabled):
    client = sm_enabled
    client.post("/api/clocks/standby")
    assert client.get("/api/runsheet/state").get_json()["standby"] is True
    # Posting a real runsheet replaces state — standby key falls out.
    client.post("/api/runsheet/state", json={
        "items": [{"type": "song", "title": "a"}],
    })
    state = client.get("/api/runsheet/state").get_json()
    assert "standby" not in state or state["standby"] is False


def test_preview_shows_standby_when_flag_set(sm_enabled):
    """When the operator has hit Standby, the inline preview should mirror
    the device — so all three roles return a JPEG (the standby page)."""
    client = sm_enabled
    client.post("/api/clocks/standby")
    for role in ("screen", "sound", "lights"):
        r = client.get(f"/api/clocks/preview?role={role}")
        assert r.status_code == 200
        assert r.data.startswith(JPEG_MAGIC)


# ── Inline clock preview ─────────────────────────────────────────────────────

def test_preview_returns_jpeg_for_each_role(sm_enabled):
    for role in ("screen", "sound", "lights"):
        r = sm_enabled.get(f"/api/clocks/preview?role={role}")
        assert r.status_code == 200
        assert r.mimetype == "image/jpeg"
        assert r.data.startswith(JPEG_MAGIC)


def test_preview_compact_and_detailed_differ(sm_enabled):
    a = sm_enabled.get("/api/clocks/preview?role=screen&verbosity=compact").data
    b = sm_enabled.get("/api/clocks/preview?role=screen&verbosity=detailed").data
    assert a.startswith(JPEG_MAGIC)
    assert b.startswith(JPEG_MAGIC)
    assert a != b  # different layouts → different bytes


def test_preview_unknown_verbosity_falls_back_to_default(sm_enabled):
    r = sm_enabled.get("/api/clocks/preview?role=screen&verbosity=invalid")
    assert r.status_code == 200
    assert r.data.startswith(JPEG_MAGIC)


def test_preview_unknown_role_falls_back_to_screen(sm_enabled):
    r = sm_enabled.get("/api/clocks/preview?role=ghost")
    assert r.status_code == 200
    assert r.data.startswith(JPEG_MAGIC)


# ── /api/library/auto: silent library load on launch + before parse ─────────

def test_library_auto_falls_back_to_disk_when_pp_unreachable(
        client, tmp_path, monkeypatch):
    """No PP running + an existing local .pro folder → returns the disk
    items with source='disk'. The UI calls this on launch + before each
    parse so the operator doesn't have to manually fetch/scan."""
    # Create a fake "library" folder with two .pro files (empty contents
    # is fine — scan_library reads UUIDs but tolerates missing UUIDs).
    lib = tmp_path / "Default"
    lib.mkdir()
    (lib / "Build My Life.pro").write_bytes(b"")
    (lib / "King of Kings.pro").write_bytes(b"")
    r = client.get(f"/api/library/auto?host=127.0.0.1&port=1&dir={lib}")
    body = r.get_json()
    assert r.status_code == 200
    assert body["source"] == "disk"
    assert body["count"] == 2
    names = sorted(it["name"] for it in body["items"])
    assert names == ["Build My Life", "King of Kings"]


def test_library_auto_returns_none_when_pp_unreachable_and_no_disk(client):
    """No PP + no local folder → source='none', empty items. UI surfaces
    'No library — open Settings' in the sidebar footer."""
    r = client.get("/api/library/auto?host=127.0.0.1&port=1&dir=")
    body = r.get_json()
    assert r.status_code == 200
    assert body["source"] == "none"
    assert body["count"] == 0
    assert body["items"] == []


# ── Index page sanity ────────────────────────────────────────────────────────

def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<!DOCTYPE html>" in r.data
    # Service Mate UI is on the main page
    assert b"Service Mate" in r.data


# ── HTTP error handling ─────────────────────────────────────────────────────
def test_unknown_url_returns_404_not_500(client):
    """A missing URL — e.g. the browser's automatic /favicon.ico probe —
    must return a real 404, not be logged as an unhandled crash and masked
    as a 500 by the catch-all exception handler (regression: Windows log
    filled with tracebacks on every launch)."""
    assert client.get("/favicon.ico").status_code == 404
    assert client.get("/definitely-not-a-route").status_code == 404


def test_wrong_method_returns_405_not_500(client):
    """Wrong HTTP method on a real route keeps its 405 through the catch-all
    handler instead of collapsing to a 500."""
    # /api/health is GET-only
    assert client.post("/api/health").status_code == 405


def test_catch_all_passes_through_http_exception(app_module):
    """The catch-all returns HTTPExceptions unchanged (preserving their
    status), rather than logging + wrapping them as a 500."""
    from werkzeug.exceptions import NotFound
    exc = NotFound()
    assert app_module._unhandled(exc) is exc


def test_catch_all_returns_500_for_non_http_exception(app_module):
    """A genuine (non-HTTP) exception still becomes a JSON 500 — the
    pass-through only spares HTTPExceptions. Called directly so Flask's
    TESTING-mode exception propagation doesn't get in the way."""
    with app_module.app.app_context():
        resp, status = app_module._unhandled(RuntimeError("kaboom"))
    assert status == 500
    assert "kaboom" in resp.get_json()["error"]


# ── clock errors a volunteer can act on ──────────────────────────────────
# The raw requests text — "HTTPConnectionPool(host='192.168.1.119',
# port=80): Max retries exceeded with url: /app.json (Caused by
# NewConnectionError(… [Errno 65] No route to host))" — was reaching the
# UI verbatim. It reads like a crash and says nothing about what to do.
#
# The real incident behind these: the clock was momentarily off Wi-Fi and
# came back on its own. The IP was right the whole time, but the message
# sent the operator hunting for a wrong IP.

import pytest
import requests as _req

from propresenterrunsheet.service_mate.geekmagic import clock_error_message


def test_unreachable_says_asleep_not_wrong_ip():
    """These clocks drop off Wi-Fi and return. Sending someone to
    re-check a correct IP is the wrong first instruction."""
    exc = _req.exceptions.ConnectionError(
        "HTTPConnectionPool(host='192.168.1.119', port=80): Max retries "
        "exceeded with url: /app.json (Caused by NewConnectionError("
        "'<urllib3.connection.HTTPConnection object>: Failed to establish a "
        "new connection: [Errno 65] No route to host'))")
    msg = clock_error_message(exc, "192.168.1.119")
    assert "asleep" in msg or "Wi-Fi" in msg
    assert "power cycle" in msg
    assert "192.168.1.119" in msg


def test_refused_points_at_the_wrong_device():
    exc = _req.exceptions.ConnectionError("Connection refused")
    msg = clock_error_message(exc, "192.168.1.5")
    assert "isn't the clock" in msg


def test_timeout_says_it_is_there_but_silent():
    exc = _req.exceptions.ConnectTimeout("timed out")
    msg = clock_error_message(exc, "192.168.1.9")
    assert "didn't answer in time" in msg


@pytest.mark.parametrize("exc", [
    _req.exceptions.ConnectionError(
        "HTTPConnectionPool(host='x', port=80): Max retries exceeded"),
    _req.exceptions.ConnectTimeout("timed out"),
    _req.exceptions.HTTPError("500 Server Error"),
    ValueError("something odd"),
])
def test_no_message_ever_leaks_the_library_internals(exc):
    """urllib3 nouns in the UI are how a volunteer decides the app is
    broken and stops trusting it."""
    msg = clock_error_message(exc, "192.168.1.1")
    for noise in ("HTTPConnectionPool", "urllib3", "Max retries",
                  "NewConnectionError", "Errno", "Traceback"):
        assert noise not in msg, f"{noise!r} leaked: {msg}"
    assert msg.endswith(".")


def test_macos_message_names_the_local_network_setting(monkeypatch):
    """macOS reports a Local-Network denial with the SAME [Errno 65] as a
    dead device. v2.12.1 blamed the clock unconditionally, so an operator
    whose Mac was blocking the app power-cycled healthy hardware and got
    nowhere. On darwin the message must name the setting."""
    monkeypatch.setattr(
        "propresenterrunsheet.service_mate.geekmagic.sys.platform", "darwin")
    exc = _req.exceptions.ConnectionError("[Errno 65] No route to host")
    msg = clock_error_message(exc, "192.168.1.119")
    assert "Local Network" in msg
    assert "Privacy" in msg
    # The clock explanation must survive too - it is still the likelier
    # cause once permission has been granted.
    assert "power cycle" in msg


def test_non_macos_message_does_not_mention_macos(monkeypatch):
    """Windows has no Local Network gate. Telling a Windows operator to
    open System Settings is a dead end."""
    monkeypatch.setattr(
        "propresenterrunsheet.service_mate.geekmagic.sys.platform", "win32")
    exc = _req.exceptions.ConnectionError("[Errno 65] No route to host")
    msg = clock_error_message(exc, "192.168.1.119")
    assert "Local Network" not in msg
    assert "macOS" not in msg
    assert "power cycle" in msg


# ── settings_saved reports the changed field, end to end ────────────────────
#
# The unit tests in test_stats.py cover settings_change_props in isolation.
# This one covers the wiring the unit tests cannot: that the route reads the
# old settings BEFORE writing the new ones. Pass `after` as `before` by
# mistake and every diff comes back empty, with every unit test still green.

def test_route_diffs_against_what_was_on_disk_before_the_write(
        client, monkeypatch):
    from propresenterrunsheet import stats

    sent = []
    monkeypatch.setattr(stats, "track",
                        lambda name, **p: sent.append((name, p)))

    client.post("/api/settings", json={"create_timers": True})
    sent.clear()
    r = client.post("/api/settings", json={"create_timers": False})
    assert r.status_code == 200

    name, props = next(e for e in sent if e[0] == "settings_saved")
    assert props["changed"] == "create_timers"
    assert props["n_changed"] == 1
    assert props["create_timers"] is False


def test_route_never_puts_the_api_key_in_the_event(client, monkeypatch):
    """The realistic autosave: the UI posts all 15 fields including or_key."""
    from propresenterrunsheet import stats

    secret = "sk-or-v1-NEVERSENDTHIS0123456789"
    sent = []
    monkeypatch.setattr(stats, "track",
                        lambda name, **p: sent.append((name, p)))

    r = client.post("/api/settings", json={
        "or_key": secret, "create_timers": False,
        "export_dir": "/Users/someone/Desktop", "pp_host": "some-mac.local"})
    assert r.status_code == 200

    name, props = next(e for e in sent if e[0] == "settings_saved")
    blob = repr(props)
    assert secret not in blob, f"API key leaked into the event: {blob}"
    assert "someone" not in blob and "some-mac" not in blob, blob
    # ...while still saying WHICH fields moved.
    assert "or_key" in props["changed"]
    assert "export_dir" in props["changed"]
