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


def test_scan_ignores_files_that_arrived_long_ago(tmp_path):
    """Age is measured from ARRIVAL. The file here really is on disk
    now, so the window is moved forward instead of backdating mtime —
    backdating mtime alone no longer makes a file old, which is the
    whole point of the change below."""
    _touch(tmp_path, "ancient.png")
    later = time.time() + 200 * 3600
    assert ma.scan([str(tmp_path)], max_age_h=48, now=later) == []


def test_a_file_authored_months_ago_but_just_received_still_shows(tmp_path):
    """The case the feature exists for: a leader emails a graphic made
    in May. AirDrop, saved mail attachments, unzip and curl -R all keep
    the original mtime, so filtering on mtime would hide it."""
    now = time.time()
    p = _touch(tmp_path, "old but just arrived.png", age_h=24 * 90, now=now)
    assert p.stat().st_mtime < now - 48 * 3600      # mtime really is old
    assert [f["name"] for f in ma.scan([str(tmp_path)], now=now)] == \
        ["old but just arrived.png"]


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


# ── the SSRF boundary on the ProPresenter host ───────────────────────────
# The app fetches a host taken from the request body, which is an SSRF
# sink: without a limit, anything that can reach this local API could use
# the app to probe arbitrary internet addresses. ProPresenter is never on
# the public internet, so the boundary is loopback + LAN.

from propresenterrunsheet.propresenter.net import is_reachable_pp_host


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "::1", "",
    "192.168.1.153",              # the operator's own LAN address
    "10.0.0.5", "172.16.4.9",
    "Fynns-MacBook-Air.local",    # mDNS, what PP advertises itself as
    "macbook",                    # bare LAN name
    "169.254.1.1",                # direct ethernet between two machines
])
def test_real_propresenter_hosts_are_allowed(host):
    assert is_reachable_pp_host(host) is True, host


@pytest.mark.parametrize("host", [
    "8.8.8.8", "1.1.1.1", "example.com",
    "169.254.169.254",            # cloud metadata — the classic SSRF target
    "169.254.169.254.nip.io",     # …and the DNS trick that reaches it
])
def test_public_and_metadata_hosts_are_refused(host):
    assert is_reachable_pp_host(host) is False, host


def test_an_unresolvable_name_is_treated_as_unreachable():
    """Failing closed: 'we couldn't check' must not mean 'allowed'."""
    assert is_reachable_pp_host(
        "no-such-host-anywhere.invalid") is False


# ── the routes themselves ────────────────────────────────────────────────
# Everything above tests the modules directly, which left the two HTTP
# endpoints — including the reveal whitelist, the feature's security
# control — with no coverage at all.

@pytest.fixture
def assist_client(client, monkeypatch):
    import propresenterrunsheet.settings as settings_mod
    real = settings_mod.load_settings

    def enabled():
        s = dict(real())
        s["media_assist"] = True
        return s
    monkeypatch.setattr(settings_mod, "load_settings", enabled)
    import propresenterrunsheet.routes.media_assist as mod
    monkeypatch.setattr(mod, "load_settings", enabled)
    return client


def test_reveal_refuses_a_path_the_scan_did_not_return(assist_client,
                                                       monkeypatch):
    """The whitelist is the security control: without it this endpoint
    is a file browser for anything that can reach the port."""
    import propresenterrunsheet.routes.media_assist as mod
    monkeypatch.setattr(mod.media_assist, "scan",
                        lambda *a, **k: [{"path": "/tmp/allowed.png"}])
    called = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: called.append(a))

    r = assist_client.post("/api/media_assist/reveal",
                           json={"path": "/etc/passwd"})
    assert r.status_code == 400
    assert called == [], "a refused path must never reach subprocess"


def test_reveal_allows_a_path_the_scan_returned(assist_client, monkeypatch):
    import propresenterrunsheet.routes.media_assist as mod
    monkeypatch.setattr(mod.media_assist, "scan",
                        lambda *a, **k: [{"path": "/tmp/allowed.png"}])
    called = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: called.append(a[0]))
    r = assist_client.post("/api/media_assist/reveal",
                           json={"path": "/tmp/allowed.png"})
    assert r.get_json()["ok"] is True
    assert called and "/tmp/allowed.png" in " ".join(called[0])


def test_reveal_is_refused_when_the_feature_is_off(client):
    r = client.post("/api/media_assist/reveal", json={"path": "/tmp/x.png"})
    assert r.status_code == 403


def test_media_assist_returns_nothing_when_off(client):
    body = client.post("/api/media_assist", json={"items": []}).get_json()
    assert body == {"enabled": False, "files": []}


def test_a_public_host_is_refused_by_every_route(client):
    """pp_base raises for anything outside loopback/LAN, and the
    app-level handler turns that into a refusal — so the guard covers
    every ProPresenter call, not just the connection test."""
    for path, payload in [
        ("/api/create_playlist", {"host": "8.8.8.8", "port": "1",
                                  "name": "x",
                                  "matched": [{"parsed": {"title": "t"}}]}),
        ("/api/match", {"host": "8.8.8.8", "port": "1", "parsed": [],
                        "rematch_template": True}),
    ]:
        err = str((client.post(path, json=payload).get_json() or {}))
        assert "isn't an address" in err, path
