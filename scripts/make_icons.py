#!/usr/bin/env python3
"""Generate the app icon set — assets/icon.ico, assets/icon.icns,
assets/icon_1024.png — from one programmatic design, so the icon is
reproducible and tweakable without a design tool.

Design: a deep-blue gradient squircle holding a white runsheet page —
rounded item rows with colour chips matching the app's type-tag palette
(song blue, MC teal, announcement amber, sermon purple) — and a blue
play-badge overlapping the page's corner: "a runsheet, ready to run".

Run from the repo root:  python3 scripts/make_icons.py
(.icns requires macOS's iconutil; on other platforms only ico/png emit.)
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# The app's type-tag palette — the icon speaks the UI's own language.
CHIPS = ["#60a5fa", "#5eead4", "#fbbf24", "#a78bfa"]


def _vgrad(size, top, bottom):
    """Vertical linear gradient as an RGBA image."""
    t = Image.new("RGB", (1, size))
    px = t.load()
    tr, tg, tb = Image.new("RGB", (1, 1), top).getpixel((0, 0))
    br, bg, bb = Image.new("RGB", (1, 1), bottom).getpixel((0, 0))
    for y in range(size):
        f = y / (size - 1)
        px[0, y] = (round(tr + (br - tr) * f),
                    round(tg + (bg - tg) * f),
                    round(tb + (bb - tb) * f))
    return t.resize((size, size)).convert("RGBA")


def render(n: int) -> Image.Image:
    S = 1024                      # design at 1024, downscale for crispness
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ── background squircle: deep blue gradient ──
    bg = _vgrad(S, "#4C8DFF", "#1D4ED8")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=228,
                                           fill=255)
    img.paste(bg, (0, 0), mask)

    # ── soft shadow under the page ──
    sx0, sy0, sx1, sy1 = 292, 176, 732, 812   # the page rect
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([sx0 + 8, sy0 + 22, sx1 + 8, sy1 + 22],
                                         radius=48, fill=(10, 20, 60, 110))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(26)))

    d = ImageDraw.Draw(img)

    # ── the runsheet page ──
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=48, fill="#F7F9FE")

    # item rows: colour chip + text bar (the runsheet itself)
    rows_y = [268, 388, 508, 628]
    bar_w = [292, 236, 268, 200]
    for (y, w, chip) in zip(rows_y, bar_w, CHIPS):
        d.rounded_rectangle([340, y, 340 + 56, y + 56], radius=16, fill=chip)
        d.rounded_rectangle([428, y + 12, 428 + w - 88, y + 44], radius=16,
                            fill="#C9D2E3")

    # ── play badge, overlapping the page's lower-right corner ──
    cx, cy, r = 704, 776, 148
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse([cx - r - 14, cy - r - 14, cx + r + 14, cy + r + 14],
               fill=(10, 20, 60, 90))
    img = Image.alpha_composite(img, ring.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#2563EB",
              outline="#F7F9FE", width=18)
    # play triangle, optically centred (nudged right)
    t = 74
    d.polygon([(cx - t + 16, cy - t), (cx - t + 16, cy + t), (cx + t + 8, cy)],
              fill="#FFFFFF")

    return img.resize((n, n), Image.LANCZOS) if n != S else img


def main():
    ASSETS.mkdir(exist_ok=True)
    render(1024).save(ASSETS / "icon_1024.png")
    render(256).save(ASSETS / "icon.ico",
                     sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                            (32, 32), (16, 16)])
    print(f"wrote {ASSETS/'icon.ico'} + icon_1024.png")

    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = pathlib.Path(tempfile.mkdtemp()) / "icon.iconset"
        iconset.mkdir()
        for n in (16, 32, 64, 128, 256, 512):
            render(n).save(iconset / f"icon_{n}x{n}.png")
            render(n * 2).save(iconset / f"icon_{n}x{n}@2x.png")
        render(1024).save(iconset / "icon_1024x1024.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(ASSETS / "icon.icns")], check=True)
        shutil.rmtree(iconset.parent)
        print(f"wrote {ASSETS/'icon.icns'}")


if __name__ == "__main__":
    main()
