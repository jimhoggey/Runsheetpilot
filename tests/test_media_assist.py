"""Tests for the downloaded-media assist panel and port discovery.

Both features exist because of things established by testing against a
real ProPresenter, not from documentation:

  * PP's Media bin is a protobuf REGISTRY, not a folder listing, so a
    file copied into Media/Assets is invisible to PP. Verified twice —
    three orphan files, and a deliberate drop that never appeared. The
    drag is therefore irreducible, and the assist panel is everything
    around it.
  * PP does not always listen on 50001. A real machine here ran on
    55416, which made every lookup fail silently while the UI looked
    fine. PP records the port in its own preferences file.

The judgement worth protecting in tests is when NOT to guess: a wrong
confident suggestion is worse than none, because the operator stops
reading them.
"""
import time

import pytest

from propresenterrunsheet import media_assist as ma
from propresenterrunsheet.propresenter import discovery


ITEMS = [
    {"title": "MC Welcome: Ollie & Elliot"},
    {"title": "Games Fun Month Amos & Ethan"},
    {"title": "Culture Moment: Ollie & Elliot"},
    {"title": "Connect Groups"},
]


# ── suggest_item: the guess, and the refusal to guess ────────────────────

@pytest.mark.parametrize("filename,expected", [
    # The case the feature was asked for: a leader's file named in their
    # words, not the runsheet's.
    ("Updated scores for youth games.png", "Games Fun Month Amos & Ethan"),
    ("games fun month prizes.mp4", "Games Fun Month Amos & Ethan"),
    ("Culture Moment slide.png", "Culture Moment: Ollie & Elliot"),
    ("Welcome.png", "MC Welcome: Ollie & Elliot"),
    ("connect groups list.png", "Connect Groups"),
])
def test_a_meaningful_name_finds_its_item(filename, expected):
    hit = ma.suggest_item(filename, ITEMS)
    assert hit and hit["title"] == expected, filename


@pytest.mark.parametrize("filename", [
    "Screenshot 2026-08-20 at 14.05.png",
    "IMG_4471.HEIC",
    "invoice-2026.pdf",
    "download (3).png",
    "",
])
def test_a_meaningless_name_gets_no_guess(filename):
    """A wrong confident guess is worse than none — the operator stops
    trusting the column."""
    assert ma.suggest_item(filename, ITEMS) is None, filename


def test_generic_words_alone_never_match():
    """'Youth Service Screen.png' shares 'youth', 'service' and 'screen'
    with everything in a youth runsheet; without stopwords it would match
    whichever item happened to be first."""
    assert ma.suggest_item("Youth Service Screen.png", ITEMS) is None


def test_no_items_means_no_guess():
    assert ma.suggest_item("games.png", []) is None
    assert ma.suggest_item("games.png", None) is None


def test_malformed_items_do_not_raise():
    assert ma.suggest_item("games.png", [None, "nope", {}, {"title": None}]) is None


# ── scan ─────────────────────────────────────────────────────────────────

def _touch(dirpath, name, age_h=1.0, now=None):
    p = dirpath / name
    p.write_bytes(b"x")
    t = (now or time.time()) - age_h * 3600
    import os
    os.utime(p, (t, t))
    return p


def test_scan_finds_recent_media_newest_first(tmp_path):
    now = time.time()
    _touch(tmp_path, "old.png", age_h=5, now=now)
    _touch(tmp_path, "new.png", age_h=1, now=now)
    names = [f["name"] for f in ma.scan([str(tmp_path)], now=now)]
    assert names == ["new.png", "old.png"]


def test_scan_ignores_files_older_than_the_window(tmp_path):
    now = time.time()
    _touch(tmp_path, "ancient.png", age_h=200, now=now)
    assert ma.scan([str(tmp_path)], max_age_h=48, now=now) == []


def test_scan_ignores_non_media_and_dotfiles(tmp_path):
    now = time.time()
    for name in ("notes.txt", "runsheet.pdf", ".hidden.png", "app.zip"):
        _touch(tmp_path, name, now=now)
    _touch(tmp_path, "real.png", now=now)
    assert [f["name"] for f in ma.scan([str(tmp_path)], now=now)] == ["real.png"]


def test_scan_survives_a_missing_directory():
    assert ma.scan(["/definitely/not/here"]) == []


def test_scan_is_capped(tmp_path):
    now = time.time()
    for i in range(ma.MAX_FILES + 15):
        _touch(tmp_path, f"f{i:03d}.png", now=now)
    assert len(ma.scan([str(tmp_path)], now=now)) == ma.MAX_FILES


# ── build: the panel payload ─────────────────────────────────────────────

def test_build_flags_what_propresenter_already_has(tmp_path):
    now = time.time()
    _touch(tmp_path, "Culture Moment slide.png", now=now)
    _touch(tmp_path, "brand new.png", now=now)
    # PP strips the extension for the bin's display name.
    rows = ma.build(ITEMS, [str(tmp_path)],
                    bin_names=["Culture Moment slide"], now=now)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Culture Moment slide.png"]["in_bin"] is True
    assert by_name["brand new.png"]["in_bin"] is False


def test_build_attaches_the_suggestion(tmp_path):
    now = time.time()
    _touch(tmp_path, "Updated scores for youth games.png", now=now)
    row = ma.build(ITEMS, [str(tmp_path)], now=now)[0]
    assert row["suggested"]["title"] == "Games Fun Month Amos & Ethan"
    assert row["suggested"]["index"] == 1


def test_build_never_reads_file_contents(tmp_path, monkeypatch):
    """Names only. Opening an operator's downloads would be a different
    product with a different privacy story."""
    _touch(tmp_path, "anything.png")

    def explode(*a, **k):
        raise AssertionError("media_assist must not open files")
    monkeypatch.setattr("pathlib.Path.read_bytes", explode)
    monkeypatch.setattr("pathlib.Path.read_text", explode)
    ma.build(ITEMS, [str(tmp_path)])


# ── port discovery ───────────────────────────────────────────────────────

def test_a_working_configured_port_is_never_second_guessed():
    """An operator who typed a port meant it."""
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: True)
    assert port == "50001" and note == ""


def test_the_real_port_is_found_when_the_default_is_wrong(monkeypatch):
    """The actual failure: PP on 55416 while the app assumed 50001."""
    monkeypatch.setattr(discovery, "local_pp_settings",
                        lambda platform=None: {"port": 55416, "enabled": True})
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: p == "55416")
    assert port == "55416"
    assert "55416" in note


def test_network_switched_off_is_named_as_the_cause(monkeypatch):
    monkeypatch.setattr(discovery, "local_pp_settings",
                        lambda platform=None: {"port": 55416, "enabled": False})
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: False)
    assert "Network" in note and "switched off" in note


def test_a_remote_host_is_told_where_to_look_not_scanned(monkeypatch):
    """Port-scanning someone else's machine is slow and rude; the useful
    output is telling them where the number lives."""
    def boom(platform=None):
        raise AssertionError("must not read local prefs for a remote host")
    monkeypatch.setattr(discovery, "local_pp_settings", boom)
    port, note = discovery.resolve_port(
        "192.168.1.153", "50001", probe=lambda h, p: False)
    assert port == "50001"
    assert "Integrations" in note and "not always 50001" in note


def test_prefs_port_that_does_not_answer_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(discovery, "local_pp_settings",
                        lambda platform=None: {"port": 55416, "enabled": True})
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: False)
    assert port == "50001"
    assert "is propresenter running" in note.lower()


@pytest.mark.parametrize("host,expected", [
    ("localhost", True), ("127.0.0.1", True), ("", True),
    ("192.168.1.153", False), ("Fynns-MacBook-Air.local", False),
])
def test_is_local(host, expected):
    assert discovery.is_local(host) is expected


def test_local_pp_settings_never_raises_on_an_unknown_platform():
    assert discovery.local_pp_settings(platform="linux") == {}
