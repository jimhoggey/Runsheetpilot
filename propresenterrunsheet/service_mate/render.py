"""Service Mate cue rendering.

Produces 240×240 JPEGs ready to push to the GeekMagic clocks. Three public
renderers — _render_cue (live cue, with verbosity), _render_test_card
(coloured card for the Test button), _render_standby (pre-service waiting
page). _render_cue dispatches to compact (less words, big fonts) or detailed
(more words, smaller fonts) by verbosity.

JPEG (not PNG) because the v9.0.39 firmware only displays JPG/GIF in Photo
Album mode — PNG uploads succeed silently but never render."""

import datetime as _dt

from .constants import (
    ROLE_ACCENT, SM_FONTS, SM_H, SM_JPEG_QUALITY, SM_VERBOSITIES,
    SM_VERBOSITY_DEFAULT, SM_W,
)
from .state import _cue_for, _next_visible_item


def _format_mmss(seconds) -> str:
    if seconds is None:
        return "--:--"
    sign = "-" if seconds < 0 else ""
    s = abs(int(seconds))
    return f"{sign}{s // 60:02d}:{s % 60:02d}"


def _compute_remaining_seconds(state: dict):
    """Best-effort countdown: prefer pp_timer remaining if known, else compute
    from current_started_at + duration_min."""
    from ..parsing.duration import _extract_duration_min
    pp_remaining = state.get("pp_remaining_seconds")
    if isinstance(pp_remaining, (int, float)):
        return int(pp_remaining)
    items = state.get("items") or []
    idx = int(state.get("current_index") or 0)
    if not (0 <= idx < len(items)):
        return None
    item = items[idx]
    dur_min = _extract_duration_min(item)
    if dur_min <= 0:
        return None
    started_at = state.get("current_started_at")
    if not started_at:
        return None
    try:
        started = _dt.datetime.fromisoformat(started_at)
    except Exception:
        return None
    elapsed = (_dt.datetime.now() - started).total_seconds()
    return int(dur_min * 60 - elapsed)


def _sm_font(size: int):
    """Pick the first available proportional font on this OS, else default."""
    from PIL import ImageFont
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_role_strip(draw, role: str, accent: tuple, type_label: str,
                     height: int, font):
    """Top role-coloured strip: ROLE on the left, item TYPE on the right."""
    draw.rectangle([(0, 0), (SM_W, height)], fill=accent)
    text_y = max(0, (height - font.size) // 2 - 1)
    draw.text((8, text_y), role.upper(), fill=(255, 255, 255), font=font)
    tw = draw.textlength(type_label, font=font)
    draw.text((SM_W - 8 - tw, text_y), type_label, fill=(255, 255, 255), font=font)


def _draw_cue_band(draw, accent: tuple, cue_text: str, font):
    """Bottom accent-tinted band with the cue line for this role."""
    if not cue_text:
        return
    band_color = tuple(min(255, int(c * 0.35)) for c in accent)
    band_h = font.size + 11
    draw.rectangle([(0, SM_H - band_h), (SM_W, SM_H)], fill=band_color)
    ct = cue_text
    while draw.textlength(ct, font=font) > SM_W - 16 and len(ct) > 4:
        ct = ct[:-2]
    if ct != cue_text:
        ct = ct[:-1] + "…"
    draw.text((8, SM_H - band_h + 5), ct, fill=(255, 255, 255), font=font)


def _new_canvas():
    from PIL import Image
    return Image.new("RGB", (SM_W, SM_H), (16, 16, 28))


def _save_jpeg(img) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=SM_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _resolve_current(state: dict):
    """Return (cur_item, next_item, remaining_seconds) for the active state."""
    items = state.get("items") or []
    idx = int(state.get("current_index") or 0)
    if not items:
        return None, None, None
    idx = max(0, min(idx, len(items) - 1))
    return items[idx], _next_visible_item(items, idx), \
        _compute_remaining_seconds(state)


def _text_width(font, text: str) -> int:
    """Truer width than draw.textlength — uses the glyph bounding box so the
    rightmost character's side-bearing is counted. Without this, long titles
    can bleed past the canvas edge because Pillow's textlength only reports
    the advance width."""
    if not text:
        return 0
    try:
        l, _, r, _ = font.getbbox(text)
        return max(int(r - l), 0)
    except Exception:
        # ImageFont.load_default() doesn't have getbbox in older Pillow
        return int(font.getlength(text)) if hasattr(font, "getlength") else 0


def _draw_wrapped(draw, text, xy, max_w, font, fill, max_lines: int = 2):
    """Greedy word-wrap to fit max_w; ellipsizes only when words got dropped.
    Uses _text_width (bbox-based) so bearings are accounted for."""
    if not text:
        return
    words = str(text).split()
    lines: list = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if _text_width(font, test) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            if len(lines) >= max_lines:
                break
            cur = w
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # Only ellipsize when we actually dropped words. Otherwise the previous
    # bug fired: every wrapped block was speculatively trimmed to fit
    # last + "…", chopping legitimate trailing characters off perfectly-fitting
    # text.
    rendered_word_count = sum(len(line.split()) for line in lines)
    if lines and rendered_word_count < len(words):
        last = lines[-1]
        while _text_width(font, last + "…") > max_w and len(last) > 2:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"

    y = xy[1]
    line_h = font.size + 4
    for line in lines:
        draw.text((xy[0], y), line, fill=fill, font=font)
        y += line_h


def _render_cue(role: str, state: dict, verbosity: str = SM_VERBOSITY_DEFAULT) -> bytes:
    """Render a 240×240 JPEG for a given role + verbosity. JPEG (not PNG)
    because the v9.0.39 firmware only displays JPG/GIF in Photo Album mode.
    Verbosity dispatches to the compact (less words, large fonts) or detailed
    (more words, smaller fonts) layout."""
    if verbosity not in SM_VERBOSITIES:
        verbosity = SM_VERBOSITY_DEFAULT
    if verbosity == "detailed":
        return _render_cue_detailed(role, state)
    return _render_cue_compact(role, state)


def _render_cue_compact(role: str, state: dict) -> bytes:
    """Glance-from-across-the-room layout. Big title, huge countdown, just
    the next item TYPE (no full title), single cue line at the bottom."""
    from PIL import ImageDraw
    f = SM_FONTS["compact"]
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    cur, nxt, remaining = _resolve_current(state)

    img = _new_canvas()
    draw = ImageDraw.Draw(img)

    f_label = _sm_font(f["label"])
    f_title = _sm_font(f["title"])
    f_clock = _sm_font(f["clock"])
    f_next  = _sm_font(f["next"])
    f_cue   = _sm_font(f["cue"])

    type_label = (cur.get("type") if cur else "").upper().replace("_", " ") or "—"
    _draw_role_strip(draw, role, accent, type_label, height=28, font=f_label)

    cur_title = (cur.get("title") if cur else "(no runsheet)") or "(empty)"
    _draw_wrapped(draw, cur_title, (12, 36), SM_W - 24, f_title,
                  (236, 236, 243), max_lines=2)

    mmss = _format_mmss(remaining) if remaining is not None else "--:--"
    tw = draw.textlength(mmss, font=f_clock)
    cd_color = (239, 68, 68) if (remaining is not None and remaining < 30) \
        else (255, 255, 255)
    draw.text(((SM_W - tw) / 2, 92), mmss, fill=cd_color, font=f_clock)

    draw.line([(12, 168), (SM_W - 12, 168)], fill=(60, 60, 80), width=1)

    if nxt:
        nxt_type = (nxt.get("type") or "").upper().replace("_", " ")
        draw.text((12, 174), f"NEXT — {nxt_type}", fill=(140, 140, 170),
                  font=f_next)
        nxt_title = nxt.get("title") or ""
        _draw_wrapped(draw, nxt_title, (12, 190), SM_W - 24, f_next,
                      (220, 220, 235), max_lines=1)
    else:
        draw.text((12, 174), "END OF SERVICE", fill=(140, 140, 170), font=f_next)

    _draw_cue_band(draw, accent, _cue_for(role, cur) if cur else "", f_cue)
    return _save_jpeg(img)


def _render_cue_detailed(role: str, state: dict) -> bytes:
    """Standing-right-next-to-the-screen layout. Smaller fonts, includes notes,
    full next-item title, and a 'then:' hint with the next role cue."""
    from PIL import ImageDraw
    f = SM_FONTS["detailed"]
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    cur, nxt, remaining = _resolve_current(state)

    img = _new_canvas()
    draw = ImageDraw.Draw(img)

    f_label = _sm_font(f["label"])
    f_title = _sm_font(f["title"])
    f_notes = _sm_font(f["notes"])
    f_clock = _sm_font(f["clock"])
    f_next  = _sm_font(f["next"])
    f_next_t = _sm_font(f["next_t"])
    f_then  = _sm_font(f["then"])
    f_cue   = _sm_font(f["cue"])

    type_label = (cur.get("type") if cur else "").upper().replace("_", " ") or "—"
    _draw_role_strip(draw, role, accent, type_label, height=22, font=f_label)

    # Title + notes — leave 12 px each side so glyphs don't bleed past the
    # canvas edge (PIL's textlength under-reports the right side bearing for
    # the last character, which can let a string render wider than the value
    # we measured).
    cur_title = (cur.get("title") if cur else "(no runsheet)") or "(empty)"
    _draw_wrapped(draw, cur_title, (12, 28), SM_W - 24, f_title,
                  (236, 236, 243), max_lines=2)
    notes = (cur.get("notes") if cur else "") or ""
    if notes:
        _draw_wrapped(draw, notes, (12, 28 + (f["title"] + 4) * 2 + 2),
                      SM_W - 24, f_notes, (140, 140, 170), max_lines=1)

    # Countdown — slightly smaller, centred
    mmss = _format_mmss(remaining) if remaining is not None else "--:--"
    tw = draw.textlength(mmss, font=f_clock)
    cd_color = (239, 68, 68) if (remaining is not None and remaining < 30) \
        else (255, 255, 255)
    draw.text(((SM_W - tw) / 2, 96), mmss, fill=cd_color, font=f_clock)

    # NEXT — full title + a 'then:' hint pulled from the next item's role cue
    next_y = 148
    if nxt:
        nxt_type = (nxt.get("type") or "").upper().replace("_", " ")
        draw.text((12, next_y), f"NEXT — {nxt_type}", fill=(140, 140, 170),
                  font=f_next)
        _draw_wrapped(draw, nxt.get("title") or "",
                      (12, next_y + f["next"] + 3), SM_W - 24, f_next_t,
                      (220, 220, 235), max_lines=1)
        then_cue = _cue_for(role, nxt)
        if then_cue:
            then_y = next_y + f["next"] + 3 + f["next_t"] + 4
            then_text = f"then: {then_cue}"
            tt = then_text
            while draw.textlength(tt, font=f_then) > SM_W - 24 and len(tt) > 8:
                tt = tt[:-2]
            if tt != then_text:
                tt = tt[:-1] + "…"
            draw.text((12, then_y), tt, fill=(120, 120, 150), font=f_then)
    else:
        draw.text((12, next_y), "END OF SERVICE", fill=(140, 140, 170), font=f_next)

    _draw_cue_band(draw, accent, _cue_for(role, cur) if cur else "", f_cue)
    return _save_jpeg(img)


def _render_test_card(role: str, ip: str = "") -> bytes:
    """A simple coloured card with the role label — used by the Test button."""
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    img = Image.new("RGB", (SM_W, SM_H), accent)
    draw = ImageDraw.Draw(img)
    try:
        f_big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
        f_sm  = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except OSError:
        f_big = ImageFont.load_default()
        f_sm  = ImageFont.load_default()
    label = role.upper()
    tw = draw.textlength(label, font=f_big)
    draw.text(((SM_W - tw) / 2, 60), label, fill=(255, 255, 255), font=f_big)
    sub = "Service Mate test"
    tw2 = draw.textlength(sub, font=f_sm)
    draw.text(((SM_W - tw2) / 2, 130), sub, fill=(240, 240, 255), font=f_sm)
    if ip:
        tw3 = draw.textlength(ip, font=f_sm)
        draw.text(((SM_W - tw3) / 2, 156), ip, fill=(220, 220, 235), font=f_sm)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=SM_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _render_standby(role: str) -> bytes:
    """Pre-service waiting page — shown when the operator has reset the clocks
    or when no runsheet is loaded. Same role-coloured strip as the live cue
    so the device looks visually consistent. Shows "STANDBY", a friendly
    sub-line, and the current wall-clock time so anyone glancing at the screen
    knows the device is alive and waiting."""
    from PIL import ImageDraw
    accent = ROLE_ACCENT.get(role, (120, 120, 140))
    img = _new_canvas()
    draw = ImageDraw.Draw(img)

    f_label = _sm_font(14)
    f_big   = _sm_font(40)
    f_sub   = _sm_font(14)
    f_clock = _sm_font(28)

    _draw_role_strip(draw, role, accent, "STANDBY", height=28, font=f_label)

    big = "STANDBY"
    tw = _text_width(f_big, big)
    draw.text(((SM_W - tw) / 2, 70), big, fill=(236, 236, 243), font=f_big)

    sub = "Awaiting service start"
    tw2 = _text_width(f_sub, sub)
    draw.text(((SM_W - tw2) / 2, 122), sub, fill=(140, 140, 170), font=f_sub)

    now = _dt.datetime.now().strftime("%H:%M")
    tw3 = _text_width(f_clock, now)
    draw.text(((SM_W - tw3) / 2, 158), now, fill=(200, 200, 220), font=f_clock)

    band_color = tuple(min(255, int(c * 0.35)) for c in accent)
    band_h = f_sub.size + 11
    draw.rectangle([(0, SM_H - band_h), (SM_W, SM_H)], fill=band_color)
    return _save_jpeg(img)
