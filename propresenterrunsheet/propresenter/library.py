"""ProPresenter library scanning + fuzzy title matching.

`.pro` files are protobufs with a UUID near the start of the binary; we
read the first 64 KB and grep out the UUID rather than parsing the whole
file. `fuzzy_match` is a thin SequenceMatcher wrapper used everywhere
song titles meet user-typed strings."""

import difflib
import re
from pathlib import Path


_UUID_RE = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _uuid_from_binary(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)  # UUID is in the header; no need to read whole file
        m = _UUID_RE.search(data)
        return m.group().decode() if m else ""
    except Exception:
        return ""


def scan_library(directory: str) -> list:
    items = []
    for i, pro in enumerate(sorted(Path(directory).rglob("*.pro"))):
        items.append({"name": pro.stem, "uuid": _uuid_from_binary(pro), "index": i})
    return items


def _norm(s: str) -> str:
    """Lowercase + strip punctuation. Used both by fuzzy_match here and by
    the Service Mate's PP auto-track to normalise titles before comparison."""
    return re.sub(r"[^\w\s]", "", (s or "").lower().strip())


def fuzzy_match(name: str, items: list, threshold: float = 0.55):
    best_score, best = 0.0, None
    nn = _norm(name)
    for item in items:
        s = difflib.SequenceMatcher(None, nn, _norm(item.get("name", ""))).ratio()
        if s > best_score:
            best_score, best = s, item
    return (best, best_score) if best_score >= threshold else (None, best_score)


# Strict threshold for the LLM-name resolver — the model already had the
# library names in its prompt, so a low score here means it hallucinated
# rather than mis-spelled. Set high so we drop hallucinations on the floor
# instead of binding them to a random presentation.
LIBRARY_NAME_RESOLVE_THRESHOLD = 0.85


def resolve_library_name(name: str, items: list):
    """Take the EXACT (or near-exact) name the LLM emitted as `library_match`
    and resolve it back to a real library item dict, or None.

    Two-pass:
      1. exact normalised-string lookup (covers copy-paste from the prompt)
      2. tight fuzzy fallback at LIBRARY_NAME_RESOLVE_THRESHOLD — catches the
         occasional LLM dropped-comma / changed-casing without admitting
         genuinely wrong names ("Welcome" ≠ "Welcome to Christ").

    Returns the matched item dict (with uuid/name/index) or None."""
    if not name or not items:
        return None
    needle = _norm(name)
    if not needle:
        return None
    for item in items:
        if _norm(item.get("name", "")) == needle:
            return item
    match, score = fuzzy_match(name, items, threshold=LIBRARY_NAME_RESOLVE_THRESHOLD)
    return match
