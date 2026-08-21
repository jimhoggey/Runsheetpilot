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

import re
import shlex
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


# ── Build-flag drift ────────────────────────────────────────────────────────
#
# The second half of the same incident. release.yml builds BOTH platforms by
# inlining PyInstaller rather than calling build_mac.sh / build_win.bat, and
# both jobs carried a comment asking that the flags "stay in sync". Neither
# did. At the time this test was written the CI builds were missing, on both
# platforms, certifi (the CA bundle every OpenRouter call needs), pypdfium2
# (scanned-PDF rescue) and the whole OS-native OCR stack — ocrmac/Vision/
# CoreML on Mac, winocr/winrt on Windows.
#
# None of it was caught, because the smoke test only polls /api/health, which
# makes no HTTPS call and no OCR call. A binary missing all three answers
# "ok" and then fails in front of an operator.
#
# We compare only the DEPENDENCY-COLLECTION flags. Those are the ones whose
# absence silently omits code from the binary. --name/--icon/entry point are
# excluded deliberately: the local scripts use shell variables (%APP_NAME%,
# $ENTRY) where CI uses literals, so they can never compare equal and are
# not what breaks.
_DEP_FLAGS = ("--hidden-import", "--collect-all", "--collect-submodules",
              "--collect-data", "--add-data")

# (label, local script, how to find the pyinstaller call in each source)
_PAIRS = [
    ("macOS", "build_mac.sh", r"^pyinstaller\b", r"propresenter_app\.py",
     r"Build \.app \+ \.dmg", r"test -d"),
    ("Windows", "build_win.bat", r"^pyinstaller\s", r"%ENTRY%",
     r"Build \.exe with PyInstaller", r"Stable-named exe"),
]


def _slice(text, start_pat, end_pat):
    lines = text.splitlines()
    out, on = [], False
    for line in lines:
        if not on and re.search(start_pat, line.strip()):
            on = True
        if on:
            out.append(line)
            if re.search(end_pat, line):
                break
    return "\n".join(out)


def _dep_flags(block):
    """Flag/value pairs from a PyInstaller invocation, order-insensitive.

    Strips the three line-continuation characters this repo uses across its
    three shells - backslash (sh), caret (cmd) and backtick (pwsh) - then
    tokenises. Tokenising rather than reading line-by-line is what makes
    `--windowed --onedir --noconfirm --clean` on one line in CI compare equal
    to the same four flags on four lines in build_mac.sh.
    """
    cleaned = re.sub(r"[\\^`]\s*$", " ", block, flags=re.MULTILINE)
    try:
        toks = shlex.split(cleaned, comments=True)
    except ValueError:
        toks = cleaned.split()
    pairs = set()
    for i, tok in enumerate(toks):
        if tok in _DEP_FLAGS and i + 1 < len(toks):
            pairs.add(f"{tok} {toks[i + 1]}")
    return pairs


@pytest.mark.parametrize(
    "label,script,s_start,s_end,ci_start,ci_end", _PAIRS,
    ids=[p[0] for p in _PAIRS])
def test_ci_build_matches_the_local_build_script(
        label, script, s_start, s_end, ci_start, ci_end):
    """Every dependency the local build collects, CI must collect too.

    One-directional on purpose: CI may legitimately collect something extra,
    but anything the local script knows it needs and CI omits is a binary
    that ships without it.
    """
    local = _dep_flags(_slice(_read(script), s_start, s_end))
    ci = _dep_flags(_slice(_read(".github/workflows/release.yml"),
                           ci_start, ci_end))
    assert local, f"could not parse the PyInstaller call out of {script}"
    assert ci, f"could not parse the {label} PyInstaller call out of release.yml"

    missing = sorted(local - ci)
    assert not missing, (
        f"{label}: release.yml is missing {len(missing)} flag(s) that "
        f"{script} passes:\n  " + "\n  ".join(missing) +
        f"\n\nThe released {label} binary will be built without them. "
        f"Add them to the {label} job in .github/workflows/release.yml.")
