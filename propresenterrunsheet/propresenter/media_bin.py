"""Resolve template media against ProPresenter's Media bin before a PUT.

Established by bisecting live PUTs against ProPresenter 7 (its 404s carry
an empty body, so this took actual probing): **the playlist PUT resolves
media items by NAME against the Media bin and ignores the uuid field
entirely.** The template Countdown's real uuid with the name "Welcome"
was accepted; a random uuid with "Welcome" was accepted; every uuid with
"Countdown " was rejected — because "Welcome" is in the operator's Media
bin and "Countdown" is not (it was dragged straight into the template
playlist, never into Media).

Two consequences drive this module:

  1. Media references must be resolved by name against the bin BEFORE the
     PUT — that is the only identity PP honours.
  2. Media that isn't in the bin CANNOT be linked over the API, full
     stop. No refresh or retry changes that. The only honest handling is
     to drop the entry (the runsheet item keeps its coloured header) and
     tell the operator the one-time fix in plain words: drag that file
     into ProPresenter's Media area, then create again.

Presentation-type items are untouched here — they PUT correctly by uuid
(verified live, 204).
"""

import logging

log = logging.getLogger("pp_runsheet")


def _norm_name(name: str) -> str:
    return (name or "").strip().casefold()


def fetch_media_bin(base: str, http_get=None) -> list:
    """Every media asset in every Media-bin playlist: [{"uuid","name"},…].

    Returns [] on any failure — bin resolution is an upgrade, and a PP
    hiccup here must not block playlist creation (the caller just skips
    relinking, which is the pre-fix behaviour)."""
    if http_get is None:
        import requests
        http_get = requests.get
    out = []
    try:
        r = http_get(f"{base}/v1/media/playlists", timeout=6)
        r.raise_for_status()
        for mp in r.json() or []:
            uuid = ((mp.get("id") or {}).get("uuid")) or ""
            if not uuid:
                continue
            r2 = http_get(f"{base}/v1/media/playlist/{uuid}", timeout=6)
            r2.raise_for_status()
            for m in (r2.json() or {}).get("items") or []:
                mid = m.get("id") or {}
                # Keep the name EXACTLY as PP stores it — trailing spaces
                # and all. PP matches media by byte-for-byte name, so
                # stripping here silently 404s any media the operator
                # named with stray whitespace (e.g. "Countdown ").
                if mid.get("uuid") and (mid.get("name") or "").strip():
                    out.append({"uuid": mid["uuid"], "name": mid["name"]})
    except Exception as e:
        log.warning("Could not read PP media bin (%s: %s) — media linking "
                    "will be skipped this run", type(e).__name__, e)
        return []
    return out


def relink_media(matched: list, bin_items: list) -> list:
    """Swap every matched media entry to its Media-bin identity, in place.

    For each `matched[i].parsed.library_match` section: media-type entries
    whose name (trimmed, case-insensitive) exists in the bin get the bin's
    uuid AND the bin's exact name — the name is what PP actually matches
    on, so echoing the bin's spelling guarantees the PUT lands. Entries
    with no bin counterpart are removed and reported; a section left empty
    collapses to None so the runsheet item falls back to a plain header
    and the create as a whole still succeeds.

    Returns [{"item_title", "media_name"}, …] for everything dropped."""
    by_name = {_norm_name(b["name"]): b for b in bin_items or []}
    unlinked = []
    for mi in matched or []:
        parsed = mi.get("parsed") or {}
        lib = parsed.get("library_match")
        if not (isinstance(lib, dict) and isinstance(lib.get("items"), list)):
            continue
        kept = []
        for entry in lib["items"]:
            if (entry.get("type") or "").lower() != "media":
                kept.append(entry)
                continue
            hit = by_name.get(_norm_name(entry.get("name")))
            if hit:
                entry["uuid"] = hit["uuid"]
                entry["target_uuid"] = hit["uuid"]
                entry["name"] = hit["name"]
                kept.append(entry)
            else:
                unlinked.append({
                    "item_title": parsed.get("title", ""),
                    "media_name": (entry.get("name") or "").strip(),
                })
        lib["items"] = kept
        if not kept:
            parsed["library_match"] = None
    return unlinked
