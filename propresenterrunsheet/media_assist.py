"""Find recently-downloaded media and say which runsheet item it belongs to.

The gap this closes: a leader emails "updated scores for youth games"
an hour before the service. The file lands in Downloads. ProPresenter
cannot see it until a human drags it into the Media bin — established by
testing, not assumption:

  * every media endpoint in PP's API is GET, so nothing can be added
    over the network;
  * the Media bin is a protobuf REGISTRY, not a folder listing, so
    copying a file into Media/Assets does nothing. Proven twice — three
    orphan files sat in that folder invisible to PP, and a deliberate
    drop of a PDF there never appeared in the bin.

So the drag stays manual. Everything AROUND it does not: this module
finds the new files, works out which runsheet item each one is for, and
the UI then watches the bin — PP writes the registry the instant you
drop a file in, so the link lands about a second later.

Names only. File contents are never read.
"""

import logging
import re
import time
from pathlib import Path

log = logging.getLogger("pp_runsheet")

MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".heic", ".webp",
              ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv"}

# A service is prepared over days, not weeks — beyond this the list is
# noise the operator has to scroll past.
DEFAULT_MAX_AGE_H = 48
MAX_FILES = 40

# Words that carry no signal about which item a file belongs to; without
# this "Youth Service Screen.png" matches every item in a youth runsheet.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "with", "screen", "slide", "image", "video", "final", "new", "updated",
    "update", "copy", "version", "draft", "png", "jpg", "jpeg", "mp4",
    "mov", "service", "church", "youth", "sunday", "am", "pm",
}


def default_watch_dirs() -> list:
    """Downloads, plus the Desktop — where files actually land."""
    home = Path.home()
    return [str(p) for p in (home / "Downloads", home / "Desktop")
            if p.is_dir()]


def _tokens(text: str) -> set:
    words = re.split(r"[^\w]+", (text or "").casefold())
    return {w for w in words
            if len(w) > 2 and not w.isdigit() and w not in _STOPWORDS}


def scan(dirs=None, max_age_h: int = DEFAULT_MAX_AGE_H, now=None) -> list:
    """Recent media files, newest first.

    Returns `[{"name", "path", "size", "age_h"}]`. Anything unreadable is
    skipped rather than raising — this feeds a panel, not a pipeline.
    """
    now = now if now is not None else time.time()
    cutoff = now - max_age_h * 3600
    found = []
    for d in (dirs if dirs is not None else default_watch_dirs()):
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except Exception:
            log.debug("could not list %s", d)
            continue
        for p in entries:
            try:
                if not p.is_file() or p.name.startswith("."):
                    continue
                if p.suffix.lower() not in MEDIA_EXTS:
                    continue
                stat = p.stat()
                # When the file ARRIVED, not when it was authored.
                # AirDrop, saved mail attachments, unzip and curl -R all
                # preserve the original mtime, so a photo made in May and
                # emailed this morning would otherwise be filtered out —
                # exactly the case this feature exists for. st_birthtime
                # is macOS; st_ctime carries creation on Windows and
                # metadata-change on Unix, both of which beat mtime here.
                arrived = max(stat.st_mtime,
                              getattr(stat, "st_birthtime", 0) or 0,
                              stat.st_ctime)
                if arrived < cutoff:
                    continue
                found.append({"name": p.name, "path": str(p),
                              "size": stat.st_size,
                              "age_h": round((now - arrived) / 3600, 1),
                              "_mtime": arrived})
            except Exception:
                continue
    found.sort(key=lambda f: -f["_mtime"])
    for f in found:
        f.pop("_mtime", None)
    return found[:MAX_FILES]


def suggest_item(filename: str, items) -> dict:
    """Which runsheet item is this file most likely for?

    Scores on shared meaningful words between the file's stem and each
    item's title. Returns `{"index", "title", "score"}` or None — None is
    the honest answer for "Screenshot 2026-08-20.png", and a wrong
    confident guess is worse than no guess.
    """
    stem_tokens = _tokens(Path(filename).stem)
    if not stem_tokens:
        return None
    best, best_score, tied = None, 0.0, False
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        title_tokens = _tokens(item.get("title") or "")
        if not title_tokens:
            continue
        overlap = stem_tokens & title_tokens
        if not overlap:
            continue
        # Denominator is the SMALLER set. Scoring against the item's full
        # word count looked more rigorous and was wrong: "Games Fun Month
        # Amos & Ethan" carries two names no filename will ever contain,
        # so "Updated scores for youth games.png" scored 1/5 and was
        # rejected — the exact case this feature exists for. What matters
        # is how much of the shorter side is accounted for.
        score = len(overlap) / min(len(title_tokens), len(stem_tokens))
        if score > best_score:
            best, best_score, tied = i, score, False
        elif score == best_score and best is not None:
            tied = True
    # A tie is ambiguity, and the honest answer to ambiguity is silence.
    # "Ollie and Elliot.png" matches both "MC Welcome: Ollie & Elliot"
    # and "Culture Moment: Ollie & Elliot" at 1.0; picking whichever came
    # first is a maximally-confident wrong guess, which is the one thing
    # this must not do.
    if best is None or best_score < 0.5 or tied:
        return None
    return {"index": best, "title": (items[best].get("title") or ""),
            "score": round(best_score, 2)}


def build(items=None, dirs=None, bin_names=None, max_age_h=DEFAULT_MAX_AGE_H,
          now=None) -> list:
    """The panel's payload: recent media, each tagged with a suggestion
    and whether ProPresenter already has it.

    `bin_names` is the Media bin as PP reports it; matching is on the
    stem, because PP strips the extension for the display name.
    """
    have = {Path(n).stem.strip().casefold() for n in (bin_names or [])}
    out = []
    for f in scan(dirs, max_age_h=max_age_h, now=now):
        f = dict(f)
        f["in_bin"] = Path(f["name"]).stem.strip().casefold() in have
        f["suggested"] = suggest_item(f["name"], items)
        out.append(f)
    return out
