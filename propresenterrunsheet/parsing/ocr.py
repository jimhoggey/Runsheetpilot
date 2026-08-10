"""Local OCR for screenshots and scanned runsheets.

The app sends TEXT to OpenRouter, never images. That is deliberate: of the
free models that pass our catalogue filters, exactly one accepts image
input, so an image pathway would quietly shrink the operator's model pool
to a single provider. Converting locally keeps every free model usable,
costs nothing per parse, and lets the operator correct a misread before
spending a request.

**The thing that actually decides quality is `observations_to_text`.**
Both OS engines return one observation per table CELL, in COLUMN-MAJOR
order. Real Apple Vision output on a runsheet screenshot started:

    '6:30pm'  '7:00pm'  '7:05pm' ... 'Youth Arrival + Hangout' ...

— every time, then every item name. Passed to the model in that order it
is worthless. So this module's job is to rebuild rows from the bounding
boxes, producing the same column-ish plain text pdfplumber gives us for a
text PDF, which is what DEFAULT_PROMPT is already written against.

Backends, one per platform, both returning top-left-origin observations:

    Mac      ocrmac  -> Apple Vision      (reports BOTTOM-left; we flip)
    Windows  winocr  -> Windows.Media.Ocr (already top-left)

Anything else raises OCRUnavailable, which the route turns into plain
advice. That includes Linux and CI, so the test suite injects fakes and
never touches a real engine.
"""

import logging
import sys

log = logging.getLogger("pp_runsheet")

# Gap inserted between cells of the same row. Wide enough that the model
# reads them as separate columns rather than one run-on phrase, and it
# mirrors how pdfplumber spaces columns in the text-PDF path.
CELL_GAP = "    "

# Row-clustering tolerance as a fraction of median glyph height. A fixed
# pixel value cannot work: the same runsheet arrives as a 700px phone
# screenshot or a 3000px retina grab, and a constant would merge every
# row of one and split every row of the other.
ROW_TOLERANCE = 0.6


class OCRUnavailable(RuntimeError):
    """No OS text-recognition engine on this platform.

    Carries the operator-facing remedy as its message: there is nothing
    to install, so the only useful advice is which platforms work and
    what to upload instead.
    """


def observations_to_text(observations, gap: str = CELL_GAP) -> str:
    """Rebuild reading order from positioned OCR observations.

    `observations` are dicts with `text` and a top-left-origin box
    (`x`, `y`, `w`, `h`) in any consistent unit — normalised 0..1 from
    Vision, pixels from Windows. Both work because every threshold here
    is relative to the observations themselves.

    Never raises: this runs on third-party engine output mid-service.
    """
    cells = []
    for o in observations or []:
        text = (o.get("text") or "").strip()
        if not text:
            continue
        cells.append({
            "text": text,
            "x": float(o.get("x") or 0.0),
            "y": float(o.get("y") or 0.0),
            "h": float(o.get("h") or 0.0),
        })
    if not cells:
        return ""

    heights = sorted(c["h"] for c in cells)
    median_h = heights[len(heights) // 2]
    # A degenerate engine result (all zero-height boxes) would make the
    # tolerance 0 and split every cell onto its own line. Fall back to
    # exact-y grouping in that case rather than producing nonsense.
    tolerance = median_h * ROW_TOLERANCE

    # Top-to-bottom, then left-to-right, so the greedy pass below always
    # meets a row's leftmost cell first and rows accrete in reading order.
    cells.sort(key=lambda c: (c["y"], c["x"]))

    rows: list = []
    for c in cells:
        centre = c["y"] + c["h"] / 2
        for row in rows:
            if abs(row["centre"] - centre) <= tolerance:
                row["cells"].append(c)
                # Re-average so a row anchored by a short cell still
                # accepts taller siblings on the same visual line.
                row["centre"] = sum(
                    x["y"] + x["h"] / 2 for x in row["cells"]
                ) / len(row["cells"])
                break
        else:
            rows.append({"centre": centre, "cells": [c]})

    rows.sort(key=lambda r: r["centre"])
    lines = []
    for row in rows:
        row["cells"].sort(key=lambda c: c["x"])
        lines.append(gap.join(c["text"] for c in row["cells"]))
    return "\n".join(lines)


def vision_to_observations(raw) -> list:
    """Normalise ocrmac output to top-left-origin observations.

    ocrmac yields `(text, confidence, (x, y, w, h))` with **bottom-left**
    origin, normalised 0..1 — so a box near the top of the page has a
    LARGE y. Getting this flip backwards renders the runsheet upside
    down, which is silent and very hard to spot, hence the dedicated
    test.
    """
    out = []
    for entry in raw or []:
        try:
            text, _confidence, (x, y, w, h) = entry
        except (TypeError, ValueError):
            continue
        out.append({"text": text, "x": float(x), "y": 1.0 - (float(y) + float(h)),
                    "w": float(w), "h": float(h)})
    return out


def winocr_to_observations(result) -> list:
    """Normalise winocr output to top-left-origin observations.

    Windows.Media.Ocr returns lines, each with words carrying pixel
    `bounding_rect`s, already top-left. We keep the line as one cell but
    take its box from the union of its words — the line's first word
    alone would under-report the width and break column ordering on
    wide cells.
    """
    out = []
    for line in (result or {}).get("lines") or []:
        boxes = []
        for word in line.get("words") or []:
            rect = word.get("bounding_rect") or {}
            try:
                boxes.append((float(rect["x"]), float(rect["y"]),
                              float(rect["width"]), float(rect["height"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not boxes:
            # A line with no usable geometry cannot be placed in a row;
            # dropping it beats inserting it at an arbitrary position.
            continue
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)
        out.append({"text": line.get("text") or "", "x": x0, "y": y0,
                    "w": x1 - x0, "h": y1 - y0})
    return out


def _mac_backend(source) -> list:
    # ocrmac takes a path or a PIL image, so rasterised PDF pages go
    # straight in without a temp-file round trip.
    from ocrmac import ocrmac
    return vision_to_observations(
        ocrmac.OCR(source, recognition_level="accurate").recognize())


def _windows_backend(source) -> list:
    import winocr
    from PIL import Image
    img = Image.open(source) if isinstance(source, str) else source
    return winocr_to_observations(
        winocr.recognize_pil_sync(img.convert("RGB"), "en"))


def pick_backend(platform: str = None):
    """Return this platform's OCR callable, or raise OCRUnavailable.

    `platform` defaults to `sys.platform` and exists so tests can ask
    for the unsupported branch without pretending to be another OS.
    """
    platform = platform or sys.platform
    if platform == "darwin":
        return _mac_backend
    if platform == "win32":
        return _windows_backend
    raise OCRUnavailable(
        "Reading text from images needs macOS 10.15+ or Windows 10+. "
        "Upload a PDF instead.")


def image_to_text(path: str, backend=None) -> str:
    """OCR one image file into reading-ordered text.

    Raises OCRUnavailable on an unsupported platform. Any other engine
    failure propagates — the route turns it into a plain message, and
    swallowing it here would hand the model an empty runsheet.
    """
    backend = backend or pick_backend()
    return observations_to_text(backend(path))


def images_to_text(sources, backend=None) -> str:
    """OCR several images (the pages of a scanned PDF) into one document.

    Pages are joined with a blank line so the model reads them as
    continuous content rather than one giant run-on row. A page that
    yields nothing is skipped rather than contributing a blank gap.
    """
    backend = backend or pick_backend()
    pages = [t for t in (observations_to_text(backend(s)) for s in sources or [])
             if t.strip()]
    return "\n\n".join(pages)
