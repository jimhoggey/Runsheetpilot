"""GeekMagic SmallTV-Ultra HTTP client.

Speaks the device's stock-firmware HTTP surface — image upload, theme
switch, brightness, health probe. Three quirks the v9.0.39 firmware has,
all worked around here:

1. Photo Album mode displays JPG/GIF only. PNG uploads succeed silently
   but never render. Callers should pass JPEG bytes (the renderer in
   render.py does); the content-type sniff here is just a safety net.
2. POST /doUpload returns malformed HTTP (duplicate Content-Length header).
   urllib3 / requests raise InvalidHeader / ChunkedEncodingError even when
   the upload succeeded — we catch and treat as success (HACS does the same).
3. Theme=3 (Photo Album) must be set once per device per process before
   image display works. We track which IPs have been switched in
   `_CLOCK_THEME_SET` so we don't pay for the round trip every push."""

import logging
import sys

from .constants import SM_FILENAME, SM_ULTRA_IMAGE_THEME


from ..logging_setup import log_safe

log = logging.getLogger("pp_runsheet")


# Track which IPs we've already set theme=3 on this process — saves an HTTP
# call per push. Cleared on restart. Tests reach in to inspect/clear it.
_CLOCK_THEME_SET: set = set()


# Which clocks run the custom ESP32 firmware, as {ip: (is_custom, checked_at)}.
#
# Runsheet Pilot supports both firmwares at once and decides per clock, per
# push. That is a hard requirement, not a convenience: it lets clocks be
# migrated one at a time, keeps a clock reflashed back to stock working, and
# means a half-finished migration on a Friday afternoon is not three dead
# clocks. Breaking this fallback bricks every clock not yet reflashed.
_CUSTOM_FW_CACHE: dict = {}

# The daemon ticks every 500ms. Probing on every tick would hit each clock 120
# times a minute, so the answer is cached — but it must expire, or a clock
# reflashed either direction needs an app restart to be noticed.
_CUSTOM_FW_TTL_S = 60.0

# Deliberately short. This runs inside the daemon loop, and an unplugged clock
# must not stall the pushes to the other two.
_PROBE_TIMEOUT_S = 0.3


def _probe_custom(ip: str) -> bool:
    """True when the clock at `ip` speaks the JSON state protocol.

    Detection is by presence of `GET /api/state`: stock GeekMagic firmware has
    no such route, so a connection error or a non-2xx means "stock, push
    JPEGs". Any failure answers False — the image path is the safe default,
    because it is what works today.
    """
    import time
    import requests as req
    if not ip:
        return False
    cached = _CUSTOM_FW_CACHE.get(ip)
    if cached and (time.time() - cached[1]) < _CUSTOM_FW_TTL_S:
        return cached[0]
    ok = False
    try:
        r = req.get(f"http://{ip}/api/state", timeout=_PROBE_TIMEOUT_S)
        ok = bool(r.ok)
    except Exception:
        # Expected constantly for stock clocks and for any clock that is off.
        # Debug, not warning: this is the normal path for half the estate.
        log.debug("Clock %s did not answer /api/state — treating as stock",
                  log_safe(ip))
    _CUSTOM_FW_CACHE[ip] = (ok, time.time())
    return ok


def _push_state(ip: str, payload: dict) -> bool:
    """POST the JSON state to a custom-firmware clock. Any 2xx is success.

    Mirrors how `_push_to_clock` judges the stock path, so the daemon can treat
    both branches identically.
    """
    import requests as req
    if not ip:
        return False
    try:
        r = req.post(f"http://{ip}/api/state", json=payload, timeout=4)
        return bool(r.ok)
    except Exception as e:
        log.info("Clock %s state push failed: %s: %s",
                 log_safe(ip), type(e).__name__, log_safe(str(e), 200))
        return False


def _identify_clock(ip: str) -> dict:
    """What kind of clock is at `ip`, for the UI's probe button.

    Deliberately a READ-OUT, not a setting. A manual device-type toggle can be
    wrong -- point it at the wrong firmware and that clock silently stops
    working until a human notices -- whereas detection re-checks on a timer and
    self-corrects when a clock is reflashed in either direction.

    Returns {ok, kind, label, detail}. `kind` is "esp32" | "geekmagic" | "".
    """
    import requests as req
    if not ip:
        return {"ok": False, "kind": "", "label": "",
                "error": "no IP set"}
    # Custom firmware first: it is the one we can identify precisely.
    try:
        r = req.get(f"http://{ip}/api/state", timeout=1.5)
        if r.ok:
            fw = ""
            try:
                fw = (r.json() or {}).get("firmware") or ""
            except Exception:
                pass
            return {"ok": True, "kind": "esp32",
                    "label": "Service Mate ESP32",
                    "detail": fw or "custom firmware"}
    except Exception:
        pass          # not custom, or not reachable -- try the stock probe

    stock = _probe_clock(ip)
    if stock.get("ok"):
        return {"ok": True, "kind": "geekmagic",
                "label": "GeekMagic (stock firmware)",
                "detail": "image push"}
    return {"ok": False, "kind": "", "label": "",
            "error": stock.get("error") or "no response"}


def _push_test_state(ip: str, role: str) -> bool:
    """Test card for a custom-firmware clock.

    The stock path uploads a rendered JPEG; that route does not exist on the
    ESP firmware, so the Test button pushed an image at a device with nowhere
    to put it and reported failure. Send state instead.
    """
    import datetime as _dt
    now = _dt.datetime.now()
    payload = {
        "now":        now.isoformat(timespec="milliseconds"),
        "starts_at":  now.isoformat(timespec="milliseconds"),
        "ends_at":    (now + _dt.timedelta(seconds=30)).isoformat(
                          timespec="milliseconds"),
        "role":       role,
        "layout":     "compact",
        "title":      "Service Mate test",
        "cue":        f"This is the {role} clock",
        "type":       "other",
        "next_title": ip,
        "next_type":  "other",
        "next_cue":   "",
        "notes":      "",
        "next_duration_s": 60,
    }
    return _push_state(ip, payload)


def _set_clock_standby(ip: str) -> bool:
    """Clear a custom-firmware clock to its waiting screen."""
    import requests as req
    try:
        r = req.post(f"http://{ip}/api/standby", json={}, timeout=4)
        return bool(r.ok)
    except Exception:
        log.debug("Clock %s standby failed", log_safe(ip))
        return False


def _push_to_clock(ip: str, image_bytes: bytes,
                   filename: str = SM_FILENAME) -> bool:
    """Upload an image to a GeekMagic Ultra and switch its display to it.
    Returns True on success. Treats the firmware's malformed-HTTP-after-POST
    quirk as success (HACS does the same).

    `image_bytes` should be JPEG or GIF — v9.0.39 firmware does not display
    PNG in Photo Album mode (uploads succeed silently but never render)."""
    import requests as req
    if not ip:
        return False
    base = f"http://{ip}"
    fl = (filename or SM_FILENAME).lower()
    if fl.endswith(".gif"):
        ctype = "image/gif"
    elif fl.endswith(".png"):
        ctype = "image/png"
    else:
        ctype = "image/jpeg"
    files = {"file": (filename, image_bytes, ctype)}
    try:
        try:
            r = req.post(f"{base}/doUpload", params={"dir": "/image/"},
                         files=files, timeout=8)
            if not r.ok:
                log.warning(f"Clock {ip} upload returned {r.status_code}")
        except (req.exceptions.ChunkedEncodingError,
                req.exceptions.InvalidHeader,
                req.exceptions.ContentDecodingError,
                req.exceptions.ConnectionError) as e:
            # Ultra firmware (v9.0.39) sends malformed HTTP on POST —
            # specifically, it returns a response with two unmatching
            # Content-Length headers (e.g. "3888, 11"). urllib3 / requests
            # raise InvalidHeader (newer) or ChunkedEncodingError (older)
            # even though the upload itself succeeded. Verified by checking
            # that GET /image/<filename> returns 200 after such errors.
            log.debug(f"Clock {ip} POST raised {type(e).__name__} (ignored — "
                      "Ultra firmware quirk)")
        # Switch to custom-image mode (only first time per process)
        if ip not in _CLOCK_THEME_SET:
            try:
                r2 = req.get(f"{base}/set", params={"theme": SM_ULTRA_IMAGE_THEME},
                             timeout=4)
                r2.raise_for_status()
                _CLOCK_THEME_SET.add(ip)
            except Exception:
                log.exception(f"Clock {ip} theme set failed")
                return False
        # Display the image
        r3 = req.get(f"{base}/set", params={"img": f"/image/{filename}"}, timeout=4)
        r3.raise_for_status()
        return True
    except Exception:
        log.exception(f"Clock {ip} push failed")
        return False


def _set_clock_brightness(ip: str, brt: int) -> bool:
    import requests as req
    try:
        r = req.get(f"http://{ip}/set", params={"brt": int(brt)}, timeout=4)
        r.raise_for_status()
        return True
    except Exception:
        log.exception(f"Clock {ip} brightness failed")
        return False


def clock_error_message(exc, ip: str) -> str:
    """Turn a requests failure into something a volunteer can act on.

    The raw text is a wall — "HTTPConnectionPool(host='192.168.1.119',
    port=80): Max retries exceeded with url: /app.json (Caused by
    NewConnectionError(... [Errno 65] No route to host))" — which reads
    like a crash and says nothing about what to do.

    The distinctions that actually matter to the person standing at the
    desk, in the order they'd try them:

      no route / unreachable  the clock is asleep or off the Wi-Fi.
                              These devices drop off and come back, so
                              this is usually "wait a moment or power
                              cycle", NOT a wrong IP.
                              ON macOS THIS IS AMBIGUOUS: when the OS
                              denies Local Network access it fails the
                              connect with the SAME [Errno 65], so a
                              perfectly healthy clock looks dead. We
                              cannot tell the two apart from the errno,
                              so on darwin we name both — silently
                              blaming the clock sent people to power
                              cycle hardware that was never the problem.
      refused                 something IS at that address but nothing
                              is listening — usually the wrong device.
      timeout                 on the network but not answering.
    """
    import requests as req

    text = str(exc).lower()
    if isinstance(exc, req.exceptions.ConnectTimeout) or "timed out" in text:
        return (f"The clock at {ip} didn't answer in time. It's on the "
                f"network but not responding — try again, or power cycle it.")
    if "no route to host" in text or "unreachable" in text:
        base = (f"Can't reach the clock at {ip}. Either it's asleep or off "
                f"the Wi-Fi — these clocks drop off and come back, so give "
                f"it a moment or power cycle it")
        if sys.platform == "darwin":
            # The tell: a denied permission fails EVERY clock, every time.
            # A sleeping clock is intermittent and one-at-a-time.
            return (base + " — or macOS is blocking this app from reaching "
                    f"your network. Check System Settings \u203a Privacy & "
                    f"Security \u203a Local Network and turn on Runsheet "
                    f"Pilot. If every clock fails and none of them ever come "
                    f"back, it's this, not the hardware.")
        return (base + ". If it never comes back, check the IP on the "
                "clock's own screen.")
    if "refused" in text:
        return (f"{ip} refused the connection. Something is at that address "
                f"but it isn't the clock — check the IP on the clock's screen.")
    if isinstance(exc, req.exceptions.ConnectionError):
        return (f"Couldn't connect to the clock at {ip}. Check it's powered "
                f"on and on the same Wi-Fi as this computer.")
    return (f"The clock at {ip} didn't respond as expected. Check it's "
            f"powered on and on the same Wi-Fi as this computer.")


def _probe_clock(ip: str) -> dict:
    import requests as req
    try:
        r = req.get(f"http://{ip}/app.json", timeout=4)
        r.raise_for_status()
        # Some firmware returns text/plain — accept any.
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:200]}
        return {"ok": True, "data": data}
    except Exception as e:
        # Full detail to app.log for whoever debugs it; a plain sentence
        # to the person who just wants their clock working.
        log.info("Clock probe failed for %s: %s: %s",
                 log_safe(ip), type(e).__name__, log_safe(str(e), 200))
        return {"ok": False, "error": clock_error_message(e, ip)}
