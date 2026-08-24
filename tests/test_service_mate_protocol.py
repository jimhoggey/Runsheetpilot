"""Tests for the Service Mate JSON state protocol.

The wire contract is implemented twice — the C++ parser in the firmware repo
and the payload builder here — and nothing in either build catches a
disagreement. These tests pin the field names, the optionality rules, and the
two behaviours that silently reintroduce the countdown stutter if they regress:
`now` re-stamped per clock, and `ends_at` held stable per item.
"""
import datetime as dt

import pytest

from propresenterrunsheet.service_mate.protocol import (
    EndsAtHolder,
    build_state_payload,
)


NOW = dt.datetime(2026, 8, 20, 19, 7, 35, 120000)


def _state(**over):
    s = {
        "items": [
            {"title": "Games Fun Month Amos & Ethan", "type": "song",
             "duration_min": 5, "notes": "Fill prizes into the golden Esky",
             "cues": {"screen": "Show Points screen",
                      "sound": "Band mics live", "lights": "Stage wash"}},
            {"title": "MC Welcome", "type": "mc_on_stage", "duration_min": 4,
             "cues": {"screen": "MC slide / lower-thirds"}},
        ],
        "current_index": 0,
        "current_started_at": "2026-08-20T19:05:00.000000",
    }
    s.update(over)
    return s


# ------------------------------------------------------------- payload ----

def test_payload_has_every_contract_field():
    p = build_state_payload("screen", "compact", _state(),
                            ends_at=NOW + dt.timedelta(seconds=145), now=NOW)
    for key in ("now", "ends_at", "role", "layout", "title", "cue", "type",
                "next_title", "next_type", "next_cue"):
        assert key in p, f"missing contract field {key}"
    assert p["role"] == "screen"
    assert p["layout"] == "compact"
    assert p["title"] == "Games Fun Month Amos & Ethan"
    assert p["cue"] == "Show Points screen"
    assert p["next_title"] == "MC Welcome"


def test_timestamps_are_naive_iso_with_milliseconds():
    # The firmware parses exactly this shape and ignores timezones by design.
    p = build_state_payload("screen", "compact", _state(),
                            ends_at=NOW + dt.timedelta(seconds=145), now=NOW)
    assert p["now"] == "2026-08-20T19:07:35.120"
    assert p["ends_at"] == "2026-08-20T19:10:00.120"
    assert "+" not in p["now"] and "Z" not in p["now"]


def test_ends_at_is_null_when_there_is_no_deadline():
    p = build_state_payload("screen", "compact", _state(), ends_at=None, now=NOW)
    assert p["ends_at"] is None


def test_starts_at_is_omitted_not_null_when_unknown():
    # The firmware distinguishes absent from null: absent hides the progress
    # bar, null would be a parse failure on a field it expects to be a string.
    s = _state()
    del s["current_started_at"]
    p = build_state_payload("screen", "compact", s, ends_at=None, now=NOW)
    assert "starts_at" not in p


def test_starts_at_present_when_known():
    p = build_state_payload("screen", "compact", _state(), ends_at=None, now=NOW)
    assert p["starts_at"] == "2026-08-20T19:05:00.000"


def test_next_duration_omitted_when_next_item_has_no_duration():
    s = _state()
    del s["items"][1]["duration_min"]
    s["items"][1]["notes"] = "no duration anywhere"
    p = build_state_payload("screen", "compact", s, ends_at=None, now=NOW)
    assert "next_duration_s" not in p


def test_next_duration_in_seconds_when_known():
    p = build_state_payload("screen", "compact", _state(), ends_at=None, now=NOW)
    assert p["next_duration_s"] == 240        # 4 min, in SECONDS not minutes


def test_cue_differs_per_role():
    st = _state()
    assert build_state_payload("sound", "compact", st, None, NOW)["cue"] \
        == "Band mics live"
    assert build_state_payload("lights", "compact", st, None, NOW)["cue"] \
        == "Stage wash"


def test_last_item_has_empty_next_rather_than_crashing():
    s = _state(current_index=1)
    p = build_state_payload("screen", "compact", s, ends_at=None, now=NOW)
    assert p["next_title"] == ""
    assert "next_duration_s" not in p


def test_standby_layout_is_passed_through():
    p = build_state_payload("screen", "standby", _state(), None, NOW)
    assert p["layout"] == "standby"


# ------------------------------------------------- ends_at stability ------

def test_ends_at_is_held_stable_across_pushes():
    """pp_remaining_seconds is a whole-second value sampled from ProPresenter
    every 2s. Recomputing ends_at per push would move the deadline around and
    reintroduce exactly the stutter this project removes."""
    h = EndsAtHolder()
    s = _state(pp_remaining_seconds=145)
    first = h.resolve(s, NOW)
    # a moment later PP still says 145 (it only ticks every second)
    later = h.resolve(s, NOW + dt.timedelta(milliseconds=600))
    assert first == later, "ends_at moved between pushes"
    # and again after it ticks down by one
    s2 = _state(pp_remaining_seconds=144)
    assert h.resolve(s2, NOW + dt.timedelta(seconds=1)) == first


def test_ends_at_re_derives_when_the_item_changes():
    h = EndsAtHolder()
    first = h.resolve(_state(pp_remaining_seconds=145), NOW)
    second = h.resolve(_state(current_index=1, pp_remaining_seconds=300),
                       NOW + dt.timedelta(seconds=5))
    assert second != first


def test_ends_at_re_derives_on_a_real_timer_change():
    """A jump beyond the tolerance means the operator reset or adjusted the
    ProPresenter timer, which should be obeyed at once rather than smoothed."""
    h = EndsAtHolder()
    first = h.resolve(_state(pp_remaining_seconds=145), NOW)
    bumped = h.resolve(_state(pp_remaining_seconds=600),
                       NOW + dt.timedelta(seconds=1))
    assert bumped != first
    assert abs((bumped - (NOW + dt.timedelta(seconds=601))).total_seconds()) < 2


def test_ends_at_tolerates_sampling_noise_within_two_seconds():
    h = EndsAtHolder()
    first = h.resolve(_state(pp_remaining_seconds=145), NOW)
    # 1s of jitter is sampling phase, not a real change
    nudged = h.resolve(_state(pp_remaining_seconds=145),
                       NOW + dt.timedelta(seconds=1))
    assert nudged == first


def test_ends_at_is_none_when_remaining_is_unknown():
    h = EndsAtHolder()
    s = _state()
    del s["items"][0]["duration_min"]
    del s["current_started_at"]
    assert h.resolve(s, NOW) is None


# ------------------------------------------------ firmware detection ------

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"status {self.status_code}")


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    from propresenterrunsheet.service_mate import geekmagic
    geekmagic._CUSTOM_FW_CACHE.clear()
    yield
    geekmagic._CUSTOM_FW_CACHE.clear()


def test_probe_detects_custom_firmware(monkeypatch):
    from propresenterrunsheet.service_mate import geekmagic
    import requests as req
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["timeout"] = kw.get("timeout")
        return _Resp(200, {"firmware": "servicemate-1.0.0"})

    monkeypatch.setattr(req, "get", fake_get)
    assert geekmagic._probe_custom("10.0.0.5") is True
    assert seen["url"].endswith("/api/state")
    # Must not stall the 500ms daemon loop when a clock is unplugged.
    assert seen["timeout"] <= 0.5


def test_probe_returns_false_for_stock_firmware(monkeypatch):
    from propresenterrunsheet.service_mate import geekmagic
    import requests as req

    def fake_get(url, **kw):
        raise req.exceptions.ConnectionError("refused")

    monkeypatch.setattr(req, "get", fake_get)
    assert geekmagic._probe_custom("10.0.0.6") is False


def test_probe_result_is_cached_per_ip(monkeypatch):
    """The daemon ticks every 500ms; probing on each tick would hammer every
    clock 120 times a minute."""
    from propresenterrunsheet.service_mate import geekmagic
    import requests as req
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(200, {"firmware": "servicemate-1.0.0"})

    monkeypatch.setattr(req, "get", fake_get)
    for _ in range(5):
        geekmagic._probe_custom("10.0.0.7")
    assert len(calls) == 1


def test_probe_cache_expires(monkeypatch):
    """A clock reflashed back to stock must be noticed without a restart."""
    from propresenterrunsheet.service_mate import geekmagic
    import requests as req
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(200, {"firmware": "servicemate-1.0.0"})

    monkeypatch.setattr(req, "get", fake_get)
    geekmagic._probe_custom("10.0.0.8")
    # age the cache entry past its TTL
    ok, _ts = geekmagic._CUSTOM_FW_CACHE["10.0.0.8"]
    geekmagic._CUSTOM_FW_CACHE["10.0.0.8"] = (ok, 0.0)
    geekmagic._probe_custom("10.0.0.8")
    assert len(calls) == 2


def test_push_state_posts_json_and_reports_success(monkeypatch):
    from propresenterrunsheet.service_mate import geekmagic
    import requests as req
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["json"] = kw.get("json")
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(req, "post", fake_post)
    assert geekmagic._push_state("10.0.0.9", {"now": "x"}) is True
    assert seen["url"].endswith("/api/state")
    assert seen["json"] == {"now": "x"}


def test_push_state_reports_failure_without_raising(monkeypatch):
    from propresenterrunsheet.service_mate import geekmagic
    import requests as req

    def fake_post(url, **kw):
        raise req.exceptions.ConnectionError("gone")

    monkeypatch.setattr(req, "post", fake_post)
    assert geekmagic._push_state("10.0.0.10", {"now": "x"}) is False


# --------------------------------------------------- daemon dual-mode -----

@pytest.fixture
def daemon_env(monkeypatch, isolated_state):
    """A two-clock setup: one custom firmware, one stock."""
    from propresenterrunsheet.service_mate import daemon, state as sm_state
    from propresenterrunsheet import licensing

    monkeypatch.setattr(licensing, "service_mate_allowed", lambda: True)
    monkeypatch.setattr(licensing, "start_trial_if_needed", lambda: None)

    sm_state._write_clocks_config({
        "clocks": [
            {"id": "screen", "ip": "10.0.0.1", "role": "screen",
             "verbosity": "compact"},
            {"id": "sound", "ip": "10.0.0.2", "role": "sound",
             "verbosity": "compact"},
        ],
        "brightness": 100, "enabled": True,
    })
    sm_state._write_runsheet_state(_state(pp_remaining_seconds=145))

    daemon._CLOCKS_LOOP_LAST_PUSHED.clear()
    daemon._ENDS_AT.reset()

    sent, jpegs = [], []
    monkeypatch.setattr(daemon, "_probe_custom", lambda ip: ip == "10.0.0.1")
    monkeypatch.setattr(daemon, "_push_state",
                        lambda ip, p: (sent.append((ip, p)), True)[1])
    monkeypatch.setattr(daemon, "_push_to_clock",
                        lambda ip, b: (jpegs.append(ip), True)[1])
    monkeypatch.setattr(daemon, "_maybe_advance_from_pp", lambda s: s)
    return daemon, sent, jpegs


def test_custom_clock_gets_state_and_stock_clock_still_gets_jpeg(daemon_env):
    """Dual-mode is a hard requirement: breaking the image fallback bricks
    every clock not yet reflashed."""
    daemon, sent, jpegs = daemon_env
    daemon._clocks_loop_tick(0)
    assert [ip for ip, _ in sent] == ["10.0.0.1"]
    assert jpegs == ["10.0.0.2"]


def test_dedup_excludes_now(daemon_env):
    """`now` changes on every tick. Including it in the dedup key would defeat
    dedup entirely and push 120 times a minute instead of on change."""
    daemon, sent, _ = daemon_env
    daemon._clocks_loop_tick(0)
    daemon._clocks_loop_tick(1)
    daemon._clocks_loop_tick(2)
    assert len(sent) == 1, "pushed again despite unchanged content"


def test_heartbeat_repushes_when_content_is_unchanged(daemon_env, monkeypatch):
    """A rebooted clock must rejoin without waiting for the next cue."""
    daemon, sent, _ = daemon_env
    daemon._clocks_loop_tick(0)
    for ip in list(daemon._CLOCKS_LOOP_LAST_PUSHED):
        h, _ts = daemon._CLOCKS_LOOP_LAST_PUSHED[ip]
        daemon._CLOCKS_LOOP_LAST_PUSHED[ip] = (h, 0.0)      # age it out
    daemon._clocks_loop_tick(1)
    assert len(sent) == 2


def test_now_is_restamped_for_each_clock(daemon_env, monkeypatch):
    """One `now` reused across sequential POSTs gives later clocks a worse
    offset, baked in until the next push. It must be stamped per clock."""
    daemon, sent, _ = daemon_env
    monkeypatch.setattr(daemon, "_probe_custom", lambda ip: True)
    stamps = []
    real_now = dt.datetime.now

    class _Clock(dt.datetime):
        # Subclass so fromisoformat and friends still work; protocol.py parses
        # starts_at through the same module.
        @staticmethod
        def now(tz=None):
            t = real_now()
            stamps.append(t)
            return t

    monkeypatch.setattr(daemon._dt, "datetime", _Clock)
    daemon._clocks_loop_tick(0)
    assert len(sent) == 2
    assert len(stamps) >= 2, "now() called fewer times than there are clocks"


def test_every_clock_receives_the_same_deadline(daemon_env, monkeypatch):
    """This is what makes the clocks tick together. Per-clock deadlines would
    desynchronise them no matter how good the offset maths is."""
    daemon, sent, _ = daemon_env
    monkeypatch.setattr(daemon, "_probe_custom", lambda ip: True)
    daemon._clocks_loop_tick(0)
    deadlines = {p["ends_at"] for _ip, p in sent}
    assert len(deadlines) == 1, f"clocks got different deadlines: {deadlines}"


def test_standby_sends_standby_layout(daemon_env):
    from propresenterrunsheet.service_mate import state as sm_state
    daemon, sent, _ = daemon_env
    s = _state(pp_remaining_seconds=145)
    s["standby"] = True
    sm_state._write_runsheet_state(s)
    daemon._clocks_loop_tick(0)
    assert sent and all(p["layout"] == "standby" for _ip, p in sent)


# ------------------------------------------------------- brightness ------

def test_brightness_omitted_when_not_configured():
    p = build_state_payload("screen", "compact", _state(), None, NOW)
    assert "brightness" not in p


def test_brightness_included_and_clamped():
    p = build_state_payload("screen", "compact", _state(), None, NOW,
                            brightness=60)
    assert p["brightness"] == 60
    assert build_state_payload("screen", "compact", _state(), None, NOW,
                               brightness=0)["brightness"] == 1
    assert build_state_payload("screen", "compact", _state(), None, NOW,
                               brightness=999)["brightness"] == 100


def test_daemon_sends_configured_brightness_to_custom_clocks(daemon_env):
    """The Settings slider had no effect on reflashed clocks: the app only ever
    spoke the stock GeekMagic brightness endpoint."""
    daemon, sent, _ = daemon_env
    daemon._clocks_loop_tick(0)
    assert sent and all(p.get("brightness") == 100 for _ip, p in sent)


def test_next_type_is_sent(daemon_env):
    """Dropped silently by the firmware parser until the drift audit caught it."""
    daemon, sent, _ = daemon_env
    daemon._clocks_loop_tick(0)
    assert sent[0][1]["next_type"] == "mc_on_stage"


# --------------------------------------------------- rotating cues -------

def test_cues_list_is_sent_as_an_array():
    s = _state()
    s["items"][0]["cues"]["sound"] = ["Mics on for Kiara & Annie",
                                      "Trailer video - unmute audio"]
    p = build_state_payload("sound", "compact", s, None, NOW)
    assert p["cues"] == ["Mics on for Kiara & Annie",
                         "Trailer video - unmute audio"]


def test_legacy_string_cue_still_works():
    """Runsheets parsed before cues became arrays must not need a re-parse."""
    p = build_state_payload("screen", "compact", _state(), None, NOW)
    assert p["cues"] == ["Show Points screen"]


def test_cue_field_kept_for_older_firmware():
    s = _state()
    s["items"][0]["cues"]["screen"] = ["First thing", "Second thing"]
    p = build_state_payload("screen", "compact", s, None, NOW)
    assert p["cue"] == "First thing"


def test_cues_fall_back_to_the_rule_table():
    s = _state()
    s["items"][0]["cues"] = {}
    p = build_state_payload("sound", "compact", s, None, NOW)
    assert p["cues"] == ["Band mics live · MC mute"]      # SOUND_CUES["song"]


def test_cues_are_capped_in_count_and_length():
    s = _state()
    s["items"][0]["cues"]["screen"] = ["a" * 90, "b", "c", "d", "e", "f"]
    p = build_state_payload("screen", "compact", s, None, NOW)
    assert len(p["cues"]) == 4                 # firmware holds MAX_CUES
    assert all(len(c) <= 60 for c in p["cues"])


# ------------------------------------------- cue shape normalisation ------

def test_ensure_item_cues_accepts_a_list():
    """The model returning the new array shape must not break the parse. This
    raised "'list' object has no attribute 'strip'" and killed parsing outright."""
    from propresenterrunsheet.service_mate.state import _ensure_item_cues
    item = {"title": "MC Welcome", "type": "mc_on_stage",
            "cues": {"screen": ["Slide - Welcome", "Trailer video ready"]}}
    out = _ensure_item_cues(item)
    assert out["cues"]["screen"] == ["Slide - Welcome", "Trailer video ready"]
    assert isinstance(out["cues"]["sound"], list) and out["cues"]["sound"]
    assert isinstance(out["cues"]["lights"], list) and out["cues"]["lights"]


def test_ensure_item_cues_accepts_a_string():
    from propresenterrunsheet.service_mate.state import _ensure_item_cues
    item = {"title": "MC Welcome", "type": "mc_on_stage",
            "cues": {"screen": "Slide - Welcome"}}
    out = _ensure_item_cues(item)
    assert out["cues"]["screen"] == ["Slide - Welcome"]


def test_ensure_item_cues_handles_empty_and_missing():
    from propresenterrunsheet.service_mate.state import _ensure_item_cues
    for cues in ({}, {"screen": ""}, {"screen": []}, {"screen": None}):
        out = _ensure_item_cues({"title": "X", "type": "song", "cues": dict(cues)})
        for role in ("screen", "sound", "lights"):
            assert isinstance(out["cues"][role], list)
            assert all(isinstance(c, str) and c for c in out["cues"][role])
