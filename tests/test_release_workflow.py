"""Guards the macOS release artifact against the drift that broke v2.12.1.

The incident: Service Mate could not reach any GeekMagic clock on a Mac.
`curl` to the same IP in the same second returned 200 while the app got
[Errno 65] No route to host - macOS was denying Local Network access, and
it reports a denial with the same errno as a dead device, so the failure
looked like flaky hardware for weeks.

The fix (NSLocalNetworkUsageDescription in the app bundle's Info.plist)
was added to build_mac.sh and released as v2.12.1. It changed nothing,
because .github/workflows/release.yml does NOT run build_mac.sh - it
inlines its own PyInstaller + hdiutil calls, with a comment asking that
the flags "stay in sync with build_mac.sh". They had not stayed in sync.
The DMG users downloaded never carried the key.

So: two builds, two places to assert. The workflow additionally verifies
the key on the BUILT bundle with PlistBuddy before packaging, which is the
check that actually catches a plutil call that ran but did nothing - this
test only catches the step being deleted.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
KEY = "NSLocalNetworkUsageDescription"


def _read(rel: str) -> str:
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} not present (running outside a source checkout)")
    # Explicit utf-8: a bare read_text() picks up cp1252 on the Windows
    # runner and blew up a release once already.
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", [
    "build_mac.sh",
    ".github/workflows/release.yml",
])
def test_mac_build_sets_local_network_usage_description(rel):
    """Every path that produces a .app must stamp the key. Miss one and
    that artifact is silently unable to reach the local network."""
    text = _read(rel)
    assert KEY in text, (
        f"{rel} no longer sets {KEY}. A Mac build without it cannot reach "
        f"ProPresenter or the Service Mate clocks, and macOS never shows a "
        f"permission prompt - it just fails with 'No route to host'.")


def test_workflow_verifies_the_key_on_the_built_bundle():
    """Asserting on the built Info.plist, not on the script, is what makes
    this real - the v2.12.1 build_mac.sh contained a correct plutil call
    the whole time and the shipped DMG still lacked the key."""
    text = _read(".github/workflows/release.yml")
    assert "PlistBuddy" in text and KEY in text, (
        "release.yml must read the key back off the built bundle and fail "
        "the build if it is missing.")
