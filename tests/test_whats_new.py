"""Tests for the once-per-version "What's new" popup.

The contract: the popup shows exactly once per installed version — on the
first launch where the running VERSION differs from the `last_seen_version`
recorded in settings. That trigger deliberately covers BOTH update paths
(the in-app update button's relaunch, and a manually installed DMG),
because the person updating is not always the person who launches next.

Two decisions the tests pin down:

  * A FRESH install never sees the popup. With no recorded version there
    is nothing "new" to that user — their first sight of the app should
    be the welcome greeter, not a changelog. The GET records the version
    so the NEXT update pops.
  * GET does not mark the version seen — only dismissing does (POST).
    If the app crashes before the popup renders, the notes survive to
    the next launch instead of being silently eaten.
"""
import pytest

import propresenterrunsheet.settings as settings_mod
from propresenterrunsheet.config import VERSION, WHATS_NEW

load_settings = settings_mod.load_settings
save_settings = settings_mod.save_settings


@pytest.fixture
def fresh_settings(client, tmp_path, monkeypatch):
    """Point the settings file at an empty per-test location."""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE",
                        tmp_path / "settings.json")
    # The routes module imports load/save by name; they read SETTINGS_FILE
    # dynamically from the settings module, so patching there is enough.
    return client


def test_whats_new_has_at_most_three_notes():
    """The release rule, enforced where releases will trip over it."""
    assert 1 <= len(WHATS_NEW) <= 3
    assert all(isinstance(n, str) and n.strip() for n in WHATS_NEW)


def test_fresh_install_does_not_show_but_records(fresh_settings):
    r = fresh_settings.get("/api/whats_new")
    assert r.status_code == 200
    assert r.get_json()["show"] is False
    assert load_settings().get("last_seen_version") == VERSION


def test_updated_install_shows_the_notes(fresh_settings):
    save_settings({"last_seen_version": "2.7.0"})
    body = fresh_settings.get("/api/whats_new").get_json()
    assert body["show"] is True
    assert body["version"] == VERSION
    assert body["notes"] == list(WHATS_NEW)[:3]


def test_get_does_not_mark_seen(fresh_settings):
    """A crash between GET and render must not eat the notes."""
    save_settings({"last_seen_version": "2.7.0"})
    fresh_settings.get("/api/whats_new")
    assert fresh_settings.get(
        "/api/whats_new").get_json()["show"] is True


def test_dismiss_marks_seen(fresh_settings):
    save_settings({"last_seen_version": "2.7.0"})
    r = fresh_settings.post("/api/whats_new/seen")
    assert r.status_code == 200
    assert load_settings().get("last_seen_version") == VERSION
    assert fresh_settings.get(
        "/api/whats_new").get_json()["show"] is False


def test_current_version_already_seen_does_not_show(fresh_settings):
    save_settings({"last_seen_version": VERSION})
    assert fresh_settings.get(
        "/api/whats_new").get_json()["show"] is False


def test_dismiss_preserves_other_settings(fresh_settings):
    """save_settings writes the whole file — the ack must merge, not
    clobber the operator's key and host."""
    save_settings({"last_seen_version": "2.7.0", "or_key": "sk-keep-me",
                   "pp_host": "10.0.0.5"})
    fresh_settings.post("/api/whats_new/seen")
    s = load_settings()
    assert s.get("or_key") == "sk-keep-me"
    assert s.get("pp_host") == "10.0.0.5"
    assert s.get("last_seen_version") == VERSION


# ── The version badge's changelog ───────────────────────────────────────────

def test_current_version_has_release_notes():
    """A VERSION bump with no notes leaves the after-update popup empty.
    config.py uses .get() so the app still boots; this is what catches it."""
    from propresenterrunsheet.config import RELEASE_NOTES, VERSION
    assert VERSION in RELEASE_NOTES, (
        f"v{VERSION} has no entry in RELEASE_NOTES — add one before "
        f"releasing, most impactful bullet first.")


def test_whats_new_is_the_entry_for_the_running_version():
    """One source of truth. Two lists would drift, and the drifting one
    would be the changelog nobody notices is wrong."""
    from propresenterrunsheet.config import RELEASE_NOTES, VERSION, WHATS_NEW
    assert WHATS_NEW == RELEASE_NOTES[VERSION]


def test_every_release_note_entry_obeys_the_three_bullet_cap():
    from propresenterrunsheet.config import RELEASE_NOTES
    for ver, notes in RELEASE_NOTES.items():
        assert 1 <= len(notes) <= 3, f"v{ver} has {len(notes)} bullets"
        assert all(isinstance(n, str) and n.strip() for n in notes), ver


def test_recent_notes_are_newest_first_by_version_not_dict_order():
    """Sorted by parsed version: 2.9.0 must not outrank 2.14.0, and an
    entry added in the wrong place must not reorder the changelog."""
    from propresenterrunsheet import config
    fake = {"2.9.0": ["old"], "2.14.0": ["newer"], "2.13.0": ["mid"]}
    orig = config.RELEASE_NOTES
    try:
        config.RELEASE_NOTES = fake
        got = [r["version"] for r in config.recent_release_notes(3)]
    finally:
        config.RELEASE_NOTES = orig
    assert got == ["2.14.0", "2.13.0", "2.9.0"]


def test_recent_notes_headline_is_the_first_bullet():
    """The badge popup shows one line per release, so bullet one carries it."""
    from propresenterrunsheet.config import recent_release_notes
    for rel in recent_release_notes(3):
        assert rel["headline"] == rel["notes"][0]


def test_release_notes_endpoint_spans_versions_and_marks_nothing_seen(
        fresh_settings):
    """Reading the changelog must not consume the after-update popup."""
    from propresenterrunsheet.settings import load_settings
    before = load_settings().get("last_seen_version")

    r = fresh_settings.get("/api/release_notes")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["releases"]) >= 2, "should span more than one version"
    assert body["releases"][0]["version"] == body["current"]
    assert load_settings().get("last_seen_version") == before
