"""Shared pytest fixtures for the ProPresenter Runsheet Builder tests.

We import the app module once and let individual tests redirect its on-disk
state files into a per-test tmpdir so route tests can roundtrip safely
without touching the user's real settings/runsheet/clocks files.
"""
import sys
from pathlib import Path

import pytest

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

    rs = tmp_path / "runsheet_state.json"
    cl = tmp_path / "clocks.json"
    monkeypatch.setattr(sm_state, "RUNSHEET_STATE_FILE", rs)
    monkeypatch.setattr(sm_state, "CLOCKS_CONFIG_FILE", cl)
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
