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

from .constants import SM_FILENAME, SM_ULTRA_IMAGE_THEME


log = logging.getLogger("pp_runsheet")


# Track which IPs we've already set theme=3 on this process — saves an HTTP
# call per push. Cleared on restart. Tests reach in to inspect/clear it.
_CLOCK_THEME_SET: set = set()


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
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
