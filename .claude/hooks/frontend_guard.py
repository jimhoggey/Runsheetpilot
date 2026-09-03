#!/usr/bin/env python3
"""PostToolUse guard for the front end.

`run_tests.py` returns early on anything that is not a .py file, so until
this hook existed the 4,500 lines under static/ and templates/ had no
automation at all. That is where a design audit found every one of these,
in shipped code:

  * `button:focus-visible { box-shadow: ...; outline: none }` — the focus
    ring composited to 1.18:1 against the page. WCAG needs 3:1. Every
    button in the app was effectively focus-invisible.
  * `.save-dot { color: var(--dim); background: var(--card) }` — 2.47:1,
    used for input placeholders and for the only instruction shown on a
    locked step.
  * `<img id="sm-preview" alt="preview" width="160" height="160">` with no
    src attribute — a 160x160 broken-image box in the default state.
  * `badge-pulse ... infinite` and `header-dot ... infinite`, both running
    permanently in the default view, neither covered by the four existing
    prefers-reduced-motion blocks.

Each check below exists because of one of those. This hook is advisory: it
never blocks an edit, it prints what it found. Blocking a UI edit over a
contrast warning would be worse than the warning.
"""

import json
import re
import sys
from pathlib import Path


WATCHED = (".css", ".js", ".html")

# ── contrast ────────────────────────────────────────────────────────────────

def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg, bg) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _hex_to_rgb(h: str):
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_TOKEN = re.compile(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*\)")


def _tokens(css: str) -> dict:
    """Map --name -> rgb for every hex custom property in the sheet."""
    out = {}
    for name, value in _TOKEN.findall(css):
        rgb = _hex_to_rgb(value)
        if rgb:
            out[name] = rgb
    return out


def _resolve(value: str, tokens: dict):
    value = value.strip()
    m = _VAR.search(value)
    if m:
        return tokens.get(m.group(1))
    if value.startswith("#"):
        return _hex_to_rgb(value.split()[0])
    return None


def _decl(body: str, prop: str):
    """Last declaration of `prop` in a rule body, or None."""
    found = None
    for line in body.split(";"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip() == prop:
            found = v
    return found


def check_contrast(css: str):
    """Rules that set BOTH a colour and a background we can resolve.

    Deliberately narrow. Resolving the real cascade would need a browser;
    a rule declaring both is the case we can be certain about, and it is
    the case that shipped broken.
    """
    tokens = _tokens(css)
    out = []
    for sel, body in _RULE.findall(css):
        sel = " ".join(sel.split())
        if sel.startswith("@") or not sel:
            continue
        fg_raw = _decl(body, "color")
        bg_raw = _decl(body, "background") or _decl(body, "background-color")
        if not fg_raw or not bg_raw:
            continue
        fg, bg = _resolve(fg_raw, tokens), _resolve(bg_raw, tokens)
        if not fg or not bg:
            continue
        ratio = contrast(fg, bg)
        if ratio < 4.5:
            size = _decl(body, "font-size") or ""
            out.append(f"{sel} — {ratio:.2f}:1"
                       + (f" at {size.strip()}" if size.strip() else "")
                       + " (WCAG AA wants 4.5:1 for body text)")
    return out


# ── the rest of the CSS checks ──────────────────────────────────────────────

def check_focus_visible(css: str):
    out = []
    for sel, body in _RULE.findall(css):
        if ":focus-visible" not in sel:
            continue
        outline = _decl(body, "outline")
        if outline and outline.strip().split()[0] in ("none", "0"):
            out.append(f"{' '.join(sel.split())} sets outline:{outline.strip()}"
                       " — a box-shadow ring is not a focus indicator unless "
                       "it reaches 3:1 against the surface behind it")
    return out


def check_reduced_motion(css: str):
    """Infinite animations must be answerable by prefers-reduced-motion."""
    rm_text = " ".join(
        m.group(0) for m in re.finditer(
            r"@media[^{]*prefers-reduced-motion[^{]*\{.*?\}\s*\}", css, re.S))
    out = []
    for sel, body in _RULE.findall(css):
        anim = _decl(body, "animation")
        if not anim or "infinite" not in anim:
            continue
        sel = " ".join(sel.split())
        key = sel.split()[-1].lstrip(".#").split(":")[0]
        if key and key not in rm_text:
            out.append(f"{sel} runs an infinite animation and no "
                       "prefers-reduced-motion block mentions it")
    return out


def check_hover_motion(css: str):
    """Movement on :hover outside @media (hover: hover)."""
    gated = [m.span() for m in re.finditer(
        r"@media[^{]*\(\s*hover\s*:\s*hover[^{]*\{", css)]
    out = []
    for m in _RULE.finditer(css):
        sel, body = m.group(1), m.group(2)
        if ":hover" not in sel:
            continue
        if not re.search(r"(transform|animation)\s*:", body):
            continue
        if any(start < m.start() for start, _ in gated):
            continue
        out.append(f"{' '.join(sel.split())} moves on hover without "
                   "@media (hover: hover) — touch taps trigger it")
    return out


def check_transitions(css: str):
    out = []
    for sel, body in _RULE.findall(css):
        trans = _decl(body, "transition")
        if not trans:
            continue
        sel = " ".join(sel.split())
        if re.match(r"^\s*all\b", trans.strip()):
            out.append(f"{sel} uses `transition: all` — name the properties")
        for num, unit in re.findall(r"([\d.]+)(m?s)", trans):
            ms = float(num) * (1 if unit == "ms" else 1000)
            if ms > 300:
                out.append(f"{sel} transitions for {ms:.0f}ms — over 300ms "
                           "reads as sluggish on a control")
                break
    return out


# ── markup ──────────────────────────────────────────────────────────────────

def check_markup(html: str):
    out = []
    for tag in re.findall(r"<img\b[^>]*>", html, re.I):
        if not re.search(r"\bsrc\s*=", tag, re.I):
            out.append(f"{tag[:70]} has no src — renders as a broken-image box "
                       "until script sets one")
        if not re.search(r"\balt\s*=", tag, re.I):
            out.append(f"{tag[:70]} has no alt attribute")
    # A label that neither wraps its input nor carries for= names nothing.
    for m in re.finditer(r"<label\b([^>]*)>(.*?)</label>", html, re.I | re.S):
        attrs, inner = m.group(1), m.group(2)
        if "for=" in attrs.lower() or "<input" in inner.lower():
            continue
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if text:
            out.append(f'<label>{text[:34]}</label> has no for= and wraps no '
                       "input — it names nothing for assistive tech")
    return out


def check_js(js: str):
    out = []
    for fn in ("alert", "confirm", "prompt"):
        n = len(re.findall(rf"(?<![\w.]){fn}\s*\(", js))
        if n:
            out.append(f"{n}x native {fn}() — inside a pywebview shell these "
                       "read as the app falling through to a system dialog")
    return out


# ── driver ──────────────────────────────────────────────────────────────────

CHECKS_CSS = (
    ("contrast", check_contrast),
    ("focus", check_focus_visible),
    ("reduced-motion", check_reduced_motion),
    ("hover", check_hover_motion),
    ("motion", check_transitions),
)


def review(path: Path, text: str) -> list:
    findings = []
    if path.suffix == ".css":
        # Strip comments first — otherwise a comment above a rule is
        # captured as part of its selector and the finding is unreadable.
        text = _COMMENT.sub(" ", text)
        for label, fn in CHECKS_CSS:
            findings += [f"[{label}] {f}" for f in fn(text)]
    elif path.suffix == ".html":
        findings += [f"[markup] {f}" for f in check_markup(text)]
        style = _COMMENT.sub(" ", "\n".join(
            re.findall(r"<style[^>]*>(.*?)</style>", text, re.S)))
        if style.strip():
            for label, fn in CHECKS_CSS:
                findings += [f"[{label}] {f}" for f in fn(style)]
    elif path.suffix == ".js":
        findings += [f"[js] {f}" for f in check_js(text)]
    return findings


# Claude's own config lives under .claude/hooks|agents|skills|commands.
# Do NOT test for "/.claude/" alone: git worktrees live at
# .claude/worktrees/<name>/, so every source file in a worktree contains
# that substring and the guard silently disabled this hook for the whole
# worktree. Match the config subdirectories instead — that stays correct
# even for a hook file nested inside a worktree.
_CFG = ("/.claude/hooks/", "/.claude/agents/",
        "/.claude/skills/", "/.claude/commands/")


def _is_claude_config(path_str: str) -> bool:
    return any(seg in path_str for seg in _CFG)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path_str = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path_str.endswith(WATCHED) or _is_claude_config(path_str):
        return 0
    path = Path(path_str)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return 0

    findings = review(path, text)
    if not findings:
        return 0

    shown = findings[:12]
    more = len(findings) - len(shown)
    body = (f"frontend-guard — {path.name}\n  "
            + "\n  ".join(shown)
            + (f"\n  ...and {more} more" if more else "")
            + "\n\nAdvisory, not a blocker. Pre-existing findings are worth "
              "fixing but are not this edit's fault — check whether the line "
              "you touched is among them.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": body,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
