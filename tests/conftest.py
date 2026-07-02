"""Shared pytest fixtures for the Runsheet Pilot tests.

We import the app module once and let individual tests redirect its on-disk
state files into a per-test tmpdir so route tests can roundtrip safely
without touching the user's real settings/runsheet/clocks files.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate the app's data dir BEFORE importing it. config.DATA_DIR (and the
# LOG_FILE handler) are resolved at import time, so this must be set first —
# otherwise `pytest` writes into the real app.log, which previously polluted
# the CI Windows smoke-test's app.log dump and made frozen-exe failures
# undiagnosable. One throwaway dir for the whole test session is fine.
os.environ.setdefault(
    "RUNSHEET_PILOT_DATA_DIR", tempfile.mkdtemp(prefix="rp-test-datadir-"))

# Make the project root importable so `import propresenter_app` works when
# pytest is invoked from anywhere (CI, repo root, IDE).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import propresenter_app as ppa  # noqa: E402  — path tweak above is intentional


@pytest.fixture
def app_module():
    """The imported `propresenter_app` module."""
    return ppa


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect the module-level state-file paths into a tmpdir for tests
    that hit routes which read or write `runsheet_state.json` / `clocks.json`.
    Returns the tmpdir path so tests can poke files directly if they need to.

    Patches on `propresenterrunsheet.service_mate.state` (where the path
    constants actually live) — patching on `propresenter_app` would only
    rebind the re-exported alias, not the value the SM code reads.
    """
    from propresenterrunsheet.service_mate import state as sm_state
    from propresenterrunsheet import settings as pp_settings

    rs = tmp_path / "runsheet_state.json"
    cl = tmp_path / "clocks.json"
    monkeypatch.setattr(sm_state, "RUNSHEET_STATE_FILE", rs)
    monkeypatch.setattr(sm_state, "CLOCKS_CONFIG_FILE", cl)
    # Redirect settings.json too — the Service Mate licence gate (added with
    # the paid add-on) reads license_key / sm_trial_start from settings, so
    # route tests must not depend on (or write to) the real user settings.
    monkeypatch.setattr(pp_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    # The clock theme cache is process-global and would be polluted by other
    # tests if we don't reset it.
    ppa._CLOCK_THEME_SET.clear()
    ppa._CLOCKS_LOOP_LAST_PUSHED.clear()
    return tmp_path


@pytest.fixture
def client(isolated_state):
    """Flask test client with isolated state files."""
    ppa.app.config["TESTING"] = True
    with ppa.app.test_client() as c:
        yield c


@pytest.fixture
def sm_enabled(client):
    """Pre-flips the Service Mate master switch ON for tests that exercise
    the clock action routes (standby / preview / probe / test). Fresh
    installs default to enabled=False (UI redesign — opt-in for users who
    don't own a GeekMagic clock), so those routes 409 unless the master
    switch is on. Tests that explicitly check the disabled path should
    use the plain `client` fixture instead."""
    client.post("/api/clocks", json={"enabled": True})
    return client
