"""Tests for the anonymous analytics client.

The thing under test here is mostly a PRIVACY contract, not a feature:
this app handles a church's internal runsheets, so the tests that matter
are the ones proving what can never leave the machine. Specifically:

  * only whitelisted event names are sent at all — the EVENTS tuple is
    the complete, auditable surface;
  * exception messages are scrubbed of paths, filenames, quoted user
    values and the OS username before they go anywhere;
  * nothing is sent from a dev run or the test suite;
  * the session id is never written to disk (persisting it would make
    two separate runs linkable, which is the whole point of the design).

And one reliability contract: track() must never raise and never block,
because it is called from inside request handlers.
"""
import queue

import pytest

from propresenterrunsheet import stats


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """Never touch the network; always start from a drained queue."""
    monkeypatch.setattr(stats, "_post", lambda payload: None)
    while not stats._q.empty():
        try:
            stats._q.get_nowait()
        except queue.Empty:
            break
    yield
    stats._state["enabled"] = None


def _enable(monkeypatch):
    monkeypatch.setattr(stats, "enabled", lambda: True)


def _drain():
    out = []
    while not stats._q.empty():
        out.append(stats._q.get_nowait())
    return out


# ── the privacy surface ──────────────────────────────────────────────────

def test_only_whitelisted_events_are_queued(monkeypatch):
    _enable(monkeypatch)
    stats.track("app_started")
    stats.track("definitely_not_an_event", secret="x")
    names = [e["eventName"] for e in _drain()]
    assert names == ["app_started"]


def test_every_wired_event_name_is_whitelisted():
    """A typo'd name is a silent no-op, so the whitelist is checked
    against what the code actually calls."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parent.parent
    called = set()
    for path in (root / "propresenterrunsheet").rglob("*.py"):
        # encoding is explicit: Python defaults to the locale codec, which
        # is cp1252 on Windows, and these sources are full of UTF-8 (⚠ — ✅).
        # Without it this test passes on Mac/Linux and dies on the Windows
        # build gate.
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'stats\.track\(\s*"([a-z_]+)"', text):
            called.add(m.group(1))
    assert called, "no stats.track calls found — did the wiring move?"
    assert called <= set(stats.EVENTS), called - set(stats.EVENTS)


def test_non_scalar_props_are_dropped():
    """Aptabase turns lists/dicts into '[Array]'/'{Object}'; sending a
    structure would be a way for content to leak by accident."""
    stats._state["enabled"] = True
    stats.track("app_started", ok=1, text="fine", flag=True,
                leaked=["runsheet", "titles"], obj={"a": 1})
    props = _drain()[0]["props"]
    assert props == {"ok": 1, "text": "fine", "flag": True}


def test_string_props_are_length_capped():
    stats._state["enabled"] = True
    stats.track("app_started", model="m" * 500)
    assert len(_drain()[0]["props"]["model"]) == 200


def test_session_id_is_never_persisted(tmp_path):
    """It lives in memory only. If this ever writes to DATA_DIR, two
    runs of the app become linkable."""
    before = set(p.name for p in stats.DATA_DIR.iterdir()) \
        if stats.DATA_DIR.exists() else set()
    sid = stats._session_id()
    after = set(p.name for p in stats.DATA_DIR.iterdir()) \
        if stats.DATA_DIR.exists() else set()
    assert sid.isdigit()
    assert after == before


def test_disabled_in_a_dev_run(monkeypatch):
    """The suite and any `python propresenter_app.py` run send nothing."""
    monkeypatch.delenv("RUNSHEET_PILOT_STATS", raising=False)
    monkeypatch.setattr(stats.sys, "frozen", False, raising=False)
    stats._state["enabled"] = None
    assert stats.enabled() is False
    stats.track("app_started")
    assert _drain() == []


def test_env_var_can_force_it_off(monkeypatch):
    monkeypatch.setenv("RUNSHEET_PILOT_STATS", "0")
    stats._state["enabled"] = None
    assert stats.enabled() is False


# ── scrubbing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,banned", [
    ("[Errno 13] Permission denied: '/Users/operatorj/Documents/Runsheet.pdf'",
     ["operatorj", "Runsheet", "Documents"]),
    (r"C:\Users\operatorj\AppData\Local\Temp\_MEI123\index.html",
     ["operatorj", "AppData", "index.html"]),
    (r"cannot open \\server\share\C3 SUMMIT 2025.mp4", ["SUMMIT", "server"]),
    ("KeyError('Youth Service : Community Night')",
     ["Youth", "Community"]),
])
def test_scrub_removes_identifying_content(raw, banned):
    out = stats.scrub(raw)
    for token in banned:
        assert token not in out, f"{token!r} survived in {out!r}"


def test_scrub_keeps_the_useful_shape():
    """It must still be diagnosable — the error class and errno stay."""
    out = stats.scrub("[Errno 13] Permission denied: '/Users/x/a.pdf'")
    assert "Errno 13" in out and "Permission denied" in out


def test_scrub_replaces_the_os_username(monkeypatch):
    monkeypatch.setenv("USER", "operatorj")
    assert "operatorj" not in stats.scrub("failed for user operatorj today")


def test_scrub_is_length_capped():
    assert len(stats.scrub("x" * 900)) <= 160


def test_scrub_survives_non_strings():
    assert isinstance(stats.scrub(None), str)
    assert isinstance(stats.scrub(ValueError("boom")), str)


# ── crash reporting ──────────────────────────────────────────────────────

def test_report_error_sends_shape_not_content(monkeypatch):
    _enable(monkeypatch)
    try:
        raise FileNotFoundError(
            "[Errno 2] No such file: '/Users/operatorj/Youth Runsheet.pdf'")
    except FileNotFoundError as e:
        stats.report_error(e, where_kind="route", route="create_playlist")
    ev = _drain()[0]
    assert ev["eventName"] == "crash"
    assert ev["props"]["error"] == "FileNotFoundError"
    assert ev["props"]["where_kind"] == "route"
    assert "operatorj" not in ev["props"]["msg"]
    assert "Youth" not in ev["props"]["msg"]
    # `where` is basenames + line numbers only, never full paths.
    assert "/" not in ev["props"]["where"]
    assert "test_stats.py" in ev["props"]["where"]


def test_error_events_are_capped_per_run(monkeypatch):
    """A crash loop is one bug, not 500 events."""
    _enable(monkeypatch)
    stats._state["errors"] = 0
    for _ in range(stats._MAX_ERRORS_PER_RUN + 25):
        stats.report_error(ValueError("boom"))
    assert len(_drain()) == stats._MAX_ERRORS_PER_RUN
    stats._state["errors"] = 0


# ── reliability: this runs inside request handlers ───────────────────────

def test_track_never_raises_on_bad_input(monkeypatch):
    _enable(monkeypatch)
    stats.track("app_started", **{"k" * 200: "v"})
    stats.track("app_started", none_value=None)
    _drain()


def test_a_full_queue_drops_events_instead_of_blocking(monkeypatch):
    """Nothing in a request path may ever wait on analytics."""
    _enable(monkeypatch)
    for _ in range(stats._q.maxsize + 10):
        stats.track("app_started")
    assert stats._q.qsize() <= stats._q.maxsize
    _drain()


def test_event_body_matches_the_aptabase_contract(monkeypatch):
    _enable(monkeypatch)
    stats.track("playlist_created", pp_items=12, pp_sections=8)
    ev = _drain()[0]
    assert set(ev) == {"timestamp", "sessionId", "eventName",
                       "systemProps", "props"}
    sp = ev["systemProps"]
    # osName is required for a desktop app — without it the server treats
    # the event as a web hit and parses a User-Agent that isn't there.
    assert sp["osName"] and sp["sdkVersion"] and sp["appVersion"]
    assert ev["timestamp"].endswith("Z")
    assert len(ev["eventName"]) <= 60


# ── settings_saved: which field changed ─────────────────────────────────────
#
# The feature: settings_saved used to report keys=15 on every autosave and
# nothing else, so the dashboard could not tell "unticked Create timers" from
# "pasted a new API key". It now names the changed fields.
#
# The hazard: the settings dict holds or_key (OpenRouter API key), license_key
# (Service Mate licence), pp_host (a hostname or LAN IP), and library_dir /
# export_dir (filesystem paths containing the OS username). track() does not
# scrub prop values. So these tests are mostly about what must NOT be sent.

_SECRET = "sk-or-v1-DEADBEEFdeadbeef0123456789"

_SENSITIVE = [
    ("or_key",                 _SECRET),
    ("license_key",            "RP1-AAAA-BBBB-CCCC-DDDD"),
    ("pp_host",                "Fynns-MacBook-Air.local"),
    ("library_dir",            "/Users/fynnj/Documents/ProPresenter/Libraries"),
    ("export_dir",             "/Users/fynnj/Desktop"),
    ("template_playlist_uuid", "6C1A0B2E-1111-2222-3333-444455556666"),
]


def test_a_toggle_records_both_the_name_and_its_new_state():
    """The whole point: "they unticked Create timers" must be legible."""
    props = stats.settings_change_props({"create_timers": True},
                                        {"create_timers": False})
    assert props["changed"] == "create_timers"
    assert props["n_changed"] == 1
    assert props["create_timers"] is False


@pytest.mark.parametrize("field,value", _SENSITIVE,
                         ids=[f[0] for f in _SENSITIVE])
def test_sensitive_fields_record_the_name_but_never_the_value(field, value):
    """Knowing the API key changed is useful. Knowing the key is a breach."""
    props = stats.settings_change_props({field: ""}, {field: value})
    assert props["changed"] == field, "the field name is the useful signal"
    assert field not in props, f"{field} value must never be a prop"
    assert value not in repr(props), f"{value!r} leaked: {props}"


def test_no_sensitive_value_survives_a_realistic_whole_object_save():
    """The UI posts all 15 fields at once. One safe field changing must not
    drag the rest of the object along with it."""
    after = {"create_timers": False, "threshold": 0.7,
             **{f: v for f, v in _SENSITIVE}}
    props = stats.settings_change_props({}, after)
    blob = repr(props)
    for _, value in _SENSITIVE:
        assert value not in blob, f"{value!r} leaked: {blob}"
    assert props["create_timers"] is False
    assert props["threshold"] == 0.7


def test_unknown_new_settings_are_name_only_by_default():
    """Deny-by-default. A setting added later must not start transmitting
    its value because someone forgot this list existed."""
    props = stats.settings_change_props({}, {"some_future_setting": "value"})
    assert props["changed"] == "some_future_setting"
    assert "some_future_setting" not in props


def test_a_save_that_changed_nothing_is_reported_as_zero():
    """Redundant saves stay visible — a loop rewriting identical settings
    is exactly the bug this should expose, not hide."""
    same = {"create_timers": True, "threshold": 0.55}
    props = stats.settings_change_props(dict(same), dict(same))
    assert props["n_changed"] == 0
    assert props["changed"] == ""


def test_a_partial_save_only_reports_its_own_keys():
    """The parse-timing writer posts one key; it must not report the other
    fourteen as unchanged-but-considered, nor as changed."""
    props = stats.settings_change_props(
        {"create_timers": True, "or_key": _SECRET, "parse_times": []},
        {"parse_times": [12.5]})
    assert props["changed"] == "parse_times"
    assert _SECRET not in repr(props)


def test_meta_prop_names_cannot_be_shadowed_by_a_real_setting():
    """settings_change_props owns "changed" and "n_changed". A settings
    field of either name would silently overwrite them."""
    from propresenterrunsheet.settings import _default_settings
    for name in stats._SETTINGS_META_PROPS:
        assert name not in _default_settings(), (
            f"a real setting is named {name!r}, which collides with the "
            f"props settings_change_props emits")
