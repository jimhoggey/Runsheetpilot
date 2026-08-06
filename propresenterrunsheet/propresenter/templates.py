"""Read template playlists from ProPresenter's REST API.

A "template playlist" is a playlist the operator has built in PP as a
reusable source of content for a recurring service type. Typical naming
convention is "<Service> - Library" — e.g. "Youth Service - Library",
"Sunday Morning Library". Each header inside the template defines a
section (e.g. "Culture") followed by media items that belong to it.

When we build a NEW playlist from a parsed runsheet, the LLM can tag a
runsheet item with a section name; we then expand that one runsheet
item into the section's full media list. Operators stop dragging the
same slides into every week's playlist by hand.

This module is read-only — it fetches the template via the REST API and
groups it into sections. The outgoing-payload builder in playlist.py
turns the sections into the items list we PUT back to PP."""

import logging
import re
from typing import Optional

from .library import resolve_library_name


log = logging.getLogger("pp_runsheet")


# Words that appear in EVERY template playlist name and therefore can't
# be used to distinguish one template from another. Stripped before
# token-scoring against the runsheet hint.
_TEMPLATE_NAME_FILLERS = {
    "library", "libary",   # common typo we've seen in the wild
    "template", "templates",
    "service", "services",
    "the", "a", "an", "of", "and",
}


def _template_signal_tokens(name: str) -> set:
    """Return the distinctive lowercased tokens in a playlist name —
    the ones that identify which service type it's for. The shared
    "library" / "service" words are stripped so they don't dilute the
    overlap score.

    "Youth Service - Library"  → {"youth"}
    "Sunday Morning Library"   → {"sunday", "morning"}
    "Wednesday Prayer Library" → {"wednesday", "prayer"}
    """
    cleaned = re.sub(r"[^\w\s]", " ", (name or "").lower())
    return {w for w in cleaned.split()
            if w and w not in _TEMPLATE_NAME_FILLERS}


def fetch_pp_playlists(base: str) -> list:
    """List the operator's playlists as [{"uuid": ..., "name": ...,
    "index": ...}]. Strips trailing whitespace from names because PP's
    UI lets you save names with stray spaces and we don't want to match
    "Library " (with trailing space) against "Library" the user typed.
    Returns [] on any failure — caller decides whether to surface that."""
    import requests as req
    try:
        r = req.get(f"{base}/v1/playlists", timeout=4)
        r.raise_for_status()
        out = []
        for p in r.json() or []:
            pid = p.get("id") or {}
            name = (pid.get("name") or "").strip()
            uuid = pid.get("uuid") or ""
            if name and uuid:
                out.append({"name": name, "uuid": uuid,
                            "index": pid.get("index", 0)})
        return out
    except Exception:
        log.debug("fetch_pp_playlists failed", exc_info=True)
        return []


def fetch_pp_playlist_items(base: str, playlist_uuid: str) -> list:
    """Return the raw items of a PP playlist (each is a header or media
    entry). Empty list on any failure — caller treats absent template as
    "no augmentation, parse normally". Caller is responsible for caching."""
    import requests as req
    try:
        r = req.get(f"{base}/v1/playlist/{playlist_uuid}", timeout=6)
        r.raise_for_status()
        return r.json().get("items") or []
    except Exception:
        log.debug(f"fetch_pp_playlist_items({playlist_uuid}) failed",
                  exc_info=True)
        return []


def playlist_to_sections(items: list) -> list:
    """Group a raw PP playlist into [{header, items}, ...] sections.

    PP playlists are flat: a "header" item visually opens a section and
    every "media" item after it belongs to that section until the next
    header. We mirror that — walk in order, open a new section on every
    header, append media to the current one.

    Sections with no header (leading orphan media items before the first
    header — rare) are dropped, because the LLM can only reference a
    section by its header name."""
    sections: list = []
    cur = {"header": None, "items": []}
    for it in items:
        t = (it.get("type") or "").lower()
        idd = it.get("id") or {}
        if t == "header":
            if cur["header"] or cur["items"]:
                sections.append(cur)
            cur = {"header": {"name":  idd.get("name", "").strip(),
                              "uuid":  idd.get("uuid", ""),
                              "color": it.get("header_color") or {}},
                   "items": []}
        elif t in ("media", "presentation"):
            cur["items"].append(_capture_item(it))
    if cur["header"] or cur["items"]:
        sections.append(cur)
    return [s for s in sections if s["header"] and s["header"].get("name")]


def _capture_item(it: dict) -> dict:
    """Capture every PP field the expander needs to faithfully reproduce
    a playlist item in the new playlist. Three kinds of item today:

      media        video/image file. Asset UUID lives at the top level
                   as `target_uuid`.
      presentation .pro file played once. Asset UUID lives inside
                   `presentation_info.presentation_uuid`.
      loop         A media/presentation item PP sets `duration` on —
                   the duration (seconds) is the only thing telling PP
                   "loop this for N seconds".

    The asset-UUID location differs between media and presentation, so
    both raw fields are captured and the expander picks. `destination`
    is per-item routing PP wants echoed back."""
    idd = it.get("id") or {}
    return {
        "name":               (idd.get("name") or "").strip(),
        "uuid":               idd.get("uuid", ""),
        "index":              idd.get("index", 0),
        "type":               (it.get("type") or "").lower(),
        "target_uuid":        it.get("target_uuid", ""),
        "presentation_info":  it.get("presentation_info") or {},
        "duration":           it.get("duration"),
        "destination":        it.get("destination") or "",
    }


def playlist_to_objects(items: list) -> list:
    """Every named media/presentation item in the playlist, in order —
    ignoring section structure entirely.

    This is the item-level counterpart to playlist_to_sections, and it
    exists because real operators build their template as a FLAT list
    ("template SUNDAY": Welcome, Countdown, PRESERVICE LOOP, …) with no
    headers at all. playlist_to_sections deliberately drops header-less
    items, so a flat template yielded zero sections and nothing ever
    matched. Objects don't need headers: each one is addressable by its
    own name.

    Unnamed entries are dropped — matching is by name, so a nameless
    object can never be referenced."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if (it.get("type") or "").lower() not in ("media", "presentation"):
            continue
        obj = _capture_item(it)
        if obj["name"]:
            out.append(obj)
    return out


def _title_tokens(text: str) -> set:
    """Lowercased word tokens with punctuation stripped, for containment
    matching. "Countdown - Start 9:27am" → {"countdown","start","9","27am"};
    handles PP names saved with stray trailing spaces for free."""
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return {w for w in cleaned.replace("_", " ").split() if w}


def resolve_object(title: str, objects: list):
    """Match a runsheet item title to a template object by name.

    Rule: every word of the object's name must appear in the title
    ("Welcome" ⊆ "Welcome and Connection Cards"; "Preach - Beyond" ⊆
    "Preach - Beyond - Ps Cathie Green"). Requiring ALL of the object's
    words keeps generic overlap from misfiring — "Post Service Media"
    shares only "service" with "END SERVICE LOOP" and must not drag a
    loop video into the playlist.

    When several objects fit, the one whose name explains more of the
    title wins ("Welcome Kids" beats "Welcome" for "Welcome Kids
    Moment"); ties keep playlist order. Deliberately NOT an LLM call —
    it's deterministic, instant, testable, and costs none of the
    operator's free-tier request quota."""
    title_tokens = _title_tokens(title)
    if not title_tokens:
        return None
    best, best_size = None, 0
    for obj in objects or []:
        name_tokens = _title_tokens(obj.get("name", ""))
        if not name_tokens or not name_tokens <= title_tokens:
            continue
        if len(name_tokens) > best_size:
            best, best_size = obj, len(name_tokens)
    return best


def resolve_with_aliases(title: str, objects: list, aliases=None):
    """resolve_object, but operator-taught aliases win first.

    The word rule (every word of the object's name appears in the title)
    cannot bridge names that share no words — the real case being a youth
    runsheet line "Youth Arrival + Hangout" whose slide is called "PreLoop
    Youth". The operator's workaround was renaming the template header to
    copy the runsheet's wording, which silently breaks the moment the
    wording changes.

    An alias is `{"match": <phrase found in the runsheet line>,
                  "template": <exact template object name>}`. If the phrase
    appears anywhere in the title (case-insensitive) and an object by that
    name exists, it wins outright. Otherwise — including when the alias
    names an object that isn't in this template — we fall through to the
    normal rule, so a stale alias degrades instead of blocking.
    """
    t = (title or "").casefold()
    for a in aliases or []:
        if not isinstance(a, dict):
            continue
        phrase = (a.get("match") or "").strip().casefold()
        target = (a.get("template") or "").strip().casefold()
        if not phrase or not target or phrase not in t:
            continue
        for obj in objects or []:
            if (obj.get("name") or "").strip().casefold() == target:
                return obj
    return resolve_object(title, objects)


def auto_detect_template_uuid(playlists: list,
                              hint: str = "") -> Optional[str]:
    """Pick a template playlist UUID when the operator hasn't set one
    explicitly. Considers only playlists whose name flags them as
    templates — must contain "library" or "template" (case-insensitive).

    When `hint` is non-empty (typically the parsed runsheet text +
    filename), each candidate playlist is scored by how many of its
    distinctive name tokens appear in the hint. The highest-scoring
    candidate wins. So a runsheet mentioning "Youth Service" picks
    "Youth Service - Library" over "Sunday Morning Library" even though
    both names contain "library".

    With no hint, OR no candidate scoring above zero, falls back to the
    first candidate in playlist order (preserves previous behaviour).

    Returns the matched UUID string or None when no candidate exists at all.
    """
    candidates = [p for p in playlists
                  if "library" in (p.get("name") or "").lower()
                  or "template" in (p.get("name") or "").lower()]
    if not candidates:
        return None
    hint_tokens = _template_signal_tokens(hint) if hint else set()
    if hint_tokens:
        # Sort by overlap-count desc, preserving original order on ties.
        scored = sorted(
            enumerate(candidates),
            key=lambda ix: (
                -len(_template_signal_tokens(ix[1].get("name", ""))
                     & hint_tokens),
                ix[0],
            ),
        )
        best_idx, best = scored[0]
        best_overlap = len(_template_signal_tokens(best.get("name", ""))
                           & hint_tokens)
        if best_overlap > 0:
            return best.get("uuid") or None
    # Fallback: first candidate (preserves prior behaviour for callers
    # that never supply a hint).
    return candidates[0].get("uuid") or None


def resolve_section(name: str, sections: list):
    """The LLM emits a section header NAME as `library_match`. Map it
    back to the actual section dict (so the payload builder has the
    media items to expand). Exact normalised match first, tight fuzzy
    fallback for minor LLM drift — built on resolve_library_name so the
    threshold + casing/punctuation rules match the existing helper."""
    if not name or not sections:
        return None
    # resolve_library_name expects a list of {name, ...} dicts and
    # returns the matched dict. We piggyback an extra `_section` field
    # so we can map back to the full section after the resolve call.
    library = [{"name": s["header"]["name"],
                "uuid": s["header"]["uuid"],
                "_section": s} for s in sections if s.get("header")]
    hit = resolve_library_name(name, library)
    return hit["_section"] if hit else None
