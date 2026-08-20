"""Find ProPresenter's network port instead of assuming it.

The app has always defaulted to 50001. That number is not a standard —
ProPresenter assigns its own port and the operator can change it, and a
real machine in this project turned out to be running on **55416**, which
meant every library and template lookup silently failed while the UI
showed nothing obviously wrong. "Cannot connect" with the wrong port in
the box is indistinguishable from "ProPresenter is closed".

ProPresenter records the answer locally:

  macOS   ~/Library/Preferences/com.renewedvision.propresenter.plist
          -> networkEnabled (bool), networkPort (int)
  Windows %APPDATA%/RenewedVision/ProPresenter/... a preferences file
          carrying the same two keys

That only helps when ProPresenter is on THIS machine. For a remote host
there is nothing to read, so the honest move is a precise error naming
the port we tried and where to find the real one — not a port scan of
someone else's computer.
"""

import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("pp_runsheet")

DEFAULT_PORT = "50001"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def is_local(host: str) -> bool:
    return (host or "").strip().lower() in LOCAL_HOSTS


def _mac_prefs() -> dict:
    """Read the two network keys from PP's preferences plist."""
    path = (Path.home() / "Library" / "Preferences"
            / "com.renewedvision.propresenter.plist")
    if not path.exists():
        return {}
    try:
        import plistlib
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        log.debug("could not read ProPresenter preferences plist")
        return {}
    return {"port": data.get("networkPort"),
            "enabled": data.get("networkEnabled")}


def _windows_prefs() -> dict:
    """Same two keys from whichever preferences file Windows PP writes.

    The exact filename has moved between versions, so rather than pin one
    we scan the small preferences directory for the keys themselves. Not
    verifiable from a Mac, hence the deliberately forgiving parsing —
    worst case this returns {} and the caller falls back to the message.
    """
    import os
    root = Path(os.environ.get("APPDATA") or "") / "RenewedVision" / "ProPresenter"
    if not root.exists():
        return {}
    for path in sorted(root.glob("*"))[:40]:
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "networkPort" not in text:
            continue
        try:                                   # JSON is the likely shape
            data = json.loads(text)
            if isinstance(data, dict) and "networkPort" in data:
                return {"port": data.get("networkPort"),
                        "enabled": data.get("networkEnabled")}
        except Exception:
            pass
        m = re.search(r'networkPort"?\s*[:=]\s*"?(\d{2,5})', text)
        if m:
            enabled = re.search(r'networkEnabled"?\s*[:=]\s*"?(true|1)', text,
                                re.IGNORECASE)
            return {"port": int(m.group(1)), "enabled": bool(enabled)}
    return {}


def local_pp_settings(platform: str = None) -> dict:
    """`{"port": int|None, "enabled": bool|None}` for a same-machine PP.

    Empty dict when ProPresenter isn't installed here or its preferences
    can't be read. Never raises — this runs on the connection path.
    """
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            return _mac_prefs()
        if platform == "win32":
            return _windows_prefs()
    except Exception:
        log.exception("ProPresenter preference lookup failed")
    return {}


def resolve_port(host: str, configured: str, probe=None,
                 platform: str = None) -> tuple:
    """Return `(port, note)` — the port to use, and what happened.

    `probe(host, port) -> bool` answers "is ProPresenter listening
    there?"; the caller supplies it so this module stays free of HTTP.

    Order matters: the configured port is tried FIRST and wins whenever
    it works, because an operator who typed a port meant it. Discovery
    exists to rescue the default, not to override a deliberate choice.
    """
    configured = str(configured or "").strip() or DEFAULT_PORT
    if probe and probe(host, configured):
        return configured, ""

    if not is_local(host):
        # Nothing to read on someone else's machine, and scanning their
        # ports would be both slow and rude.
        return configured, (
            f"ProPresenter didn't answer on {host}:{configured}. Check the "
            f"port in ProPresenter -> Preferences -> Integrations -> Network "
            f"on that computer — it is not always 50001.")

    prefs = local_pp_settings(platform)
    found = prefs.get("port")
    if prefs.get("enabled") is False:
        return configured, (
            "ProPresenter is installed here but Network is switched off. "
            "Turn on ProPresenter -> Preferences -> Integrations -> Network.")
    if found and str(found) != configured:
        if probe and not probe(host, str(found)):
            return configured, (
                f"ProPresenter's own settings say port {found}, but nothing "
                f"answered there. Is ProPresenter running?")
        log.info("ProPresenter found on port %s (configured was %s)",
                 found, configured)
        return str(found), (
            f"Found ProPresenter on port {found} and switched to it "
            f"(it was set to {configured}).")

    return configured, (
        f"ProPresenter didn't answer on {host}:{configured}. Make sure it's "
        f"running with Network enabled in Preferences -> Integrations.")
