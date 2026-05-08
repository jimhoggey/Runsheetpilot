"""Renderer smoke tests — verify _render_cue and _render_test_card always
produce valid JPEG bytes for every (role, verbosity) and edge-case state."""
import datetime as dt

import pytest


JPEG_MAGIC = b"\xff\xd8\xff"


def _example_state():
    return {
        "service_name": "Sunday Service",
        "items": [
            {"type": "song", "title": "Build My Life", "duration_min": 5,
             "notes": "9:30 AM",
             "cues": {"screen": "Cue song slides",
                      "sound":  "Band mics live",
                      "lights": "Stage wash"}},
            {"type": "sermon", "title": "King Jesus — Ps Nick",
             "duration_min": 30, "notes": "10:14 AM",
             "cues": {"screen": "Sermon slides",
                      "sound":  "Mic on for Ps Nick",
                      "lights": "Spot — preacher"}},
        ],
        "current_index": 0,
        "current_started_at": (dt.datetime.now() - dt.timedelta(seconds=78)).isoformat(),
    }


@pytest.mark.parametrize("role", ["screen", "sound", "lights"])
@pytest.mark.parametrize("verbosity", ["compact", "detailed"])
def test_render_cue_returns_jpeg_for_every_combo(app_module, role, verbosity):
    out = app_module._render_cue(role, _example_state(), verbosity=verbosity)
    assert isinstance(out, bytes)
    assert out.startswith(JPEG_MAGIC)
    # Sanity: a 240×240 JPEG with a few text lines should be > 1 KB and < 64 KB
    assert 1_000 < len(out) < 64_000


def test_render_cue_handles_empty_state(app_module):
    out = app_module._render_cue("screen", {}, verbosity="compact")
    assert out.startswith(JPEG_MAGIC)


def test_render_cue_handles_missing_cues(app_module):
    state = _example_state()
    for it in state["items"]:
        it.pop("cues", None)
    out = app_module._render_cue("sound", state, verbosity="detailed")
    assert out.startswith(JPEG_MAGIC)


def test_render_cue_handles_extremely_long_titles(app_module):
    state = _example_state()
    state["items"][0]["title"] = "A really exceptionally long title " * 5
    state["items"][1]["title"] = "Another extremely long next-up title here"
    for v in ("compact", "detailed"):
        out = app_module._render_cue("screen", state, verbosity=v)
        assert out.startswith(JPEG_MAGIC)


def test_render_cue_invalid_verbosity_falls_back_to_default(app_module):
    out = app_module._render_cue("screen", _example_state(),
                                 verbosity="ridiculous")
    assert out.startswith(JPEG_MAGIC)


def test_render_cue_handles_last_item(app_module):
    """At the last runsheet item there's no 'next' — both layouts must still
    render and show 'END OF SERVICE' instead of a next title."""
    state = _example_state()
    state["current_index"] = len(state["items"]) - 1
    for v in ("compact", "detailed"):
        out = app_module._render_cue("screen", state, verbosity=v)
        assert out.startswith(JPEG_MAGIC)


def test_render_cue_red_countdown_under_30s(app_module):
    """Sanity: when remaining < 30 s the countdown switches to red. We can't
    easily inspect pixels so we just assert no crash and a valid frame."""
    state = _example_state()
    # Force ~10 s remaining
    state["items"][0]["duration_min"] = 1
    state["current_started_at"] = (dt.datetime.now() - dt.timedelta(seconds=50)).isoformat()
    out = app_module._render_cue("screen", state, verbosity="compact")
    assert out.startswith(JPEG_MAGIC)


def test_render_test_card_returns_jpeg(app_module):
    """Test card is rendered server-side then pushed to the device."""
    out = app_module._render_test_card("sound", "192.168.1.50")
    assert out.startswith(JPEG_MAGIC)
    assert 500 < len(out) < 32_000


def test_render_test_card_no_ip(app_module):
    out = app_module._render_test_card("lights", "")
    assert out.startswith(JPEG_MAGIC)


@pytest.mark.parametrize("role", ["screen", "sound", "lights"])
def test_render_standby_returns_jpeg_for_each_role(app_module, role):
    """Standby page is the pre-service waiting screen — rendered for each
    role so the role-coloured top strip stays consistent across the cue
    and standby modes."""
    out = app_module._render_standby(role)
    assert isinstance(out, bytes)
    assert out.startswith(JPEG_MAGIC)
    assert 500 < len(out) < 32_000


def test_render_standby_unknown_role_falls_back(app_module):
    out = app_module._render_standby("ghost")
    assert out.startswith(JPEG_MAGIC)
