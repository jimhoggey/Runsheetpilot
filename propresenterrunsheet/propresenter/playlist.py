"""Playlist payload assembly for ProPresenter's REST API.

`build_playlist_payload` is a pure function — it takes the operator's
`matched` list (each item is `{parsed, match}` where `parsed` is the
LLM-extracted runsheet item and `match` is the fuzzy-match against the
song library) and returns the JSON-serialisable list PP's
`PUT /v1/playlist/{uuid}` expects.

Three branches inside the loop:
  song + match  → presentation item pointing at the matched .pro file
  song, no match → red ACTION-NEEDED header so the volunteer notices
  any other type → coloured header (green for scripture, blue for song,
                   etc.) with optional notes appended

The actual HTTP calls + .playlist export still live in the route
handler — this module only builds the body."""


# RGBA (0-1 floats) for ProPresenter playlist header items, by type.
# Mirrors the tag colours in the UI table so volunteers see the same colour
# in the builder and in PP.
TYPE_COLORS = {
    "song":         (0.06, 0.24, 0.55, 1.0),  # blue
    "mc_on_stage":  (0.05, 0.45, 0.50, 1.0),  # teal
    "announcement": (0.85, 0.50, 0.05, 1.0),  # amber
    "sermon":       (0.45, 0.20, 0.65, 1.0),  # purple
    "prayer":       (0.55, 0.35, 0.75, 1.0),  # lavender
    "scripture":    (0.15, 0.50, 0.30, 1.0),  # green
    "offering":     (0.70, 0.55, 0.10, 1.0),  # gold
    "video":        (0.55, 0.20, 0.20, 1.0),  # rust
    "other":        (0.30, 0.30, 0.40, 1.0),  # gray
}
ACTION_NEEDED_COLOR = (0.86, 0.15, 0.15, 1.0)  # bright red


def _color_for_type(t: str) -> dict:
    r, g, b, a = TYPE_COLORS.get(t or "other", TYPE_COLORS["other"])
    return {"red": r, "green": g, "blue": b, "alpha": a}


def _color_dict(rgba: tuple) -> dict:
    r, g, b, a = rgba
    return {"red": r, "green": g, "blue": b, "alpha": a}


def _coloured_header_for(p: dict) -> dict:
    """Build a single coloured-header entry from a parsed runsheet item.
    Pulled out so both the no-match fallback path AND the section-expansion
    path can use the runsheet's own labelling without duplicating the
    title/notes/scripture-emoji logic."""
    title = p.get("title", "") or ""
    if p.get("type") == "scripture":
        title = f"📖 {title}"
    label = title
    if p.get("notes"):
        label += f"  —  {p['notes']}"
    return {
        "id":           {"uuid": "", "name": label, "index": 0},
        "type":         "header",
        "target_uuid":  "",
        "is_hidden":    False, "is_pco": False,
        "header_color": _color_for_type(p.get("type")),
    }


def build_playlist_payload(matched: list) -> list:
    """Assemble the items list to PUT to PP's /v1/playlist/{uuid}.

    Pure function — no I/O, no globals — so it's straightforward to test.
    Each input element is a dict like:

        {"parsed": {"type": "...", "title": "...", "notes": "...",
                    "library_match": <section-dict | single-pres-dict | None>},
         "match":  {"uuid": "...", "name": "...", "index": 0}}    # may be None

    `library_match` can be either:

      • A SECTION dict — `{"header": {"name", "uuid", "color"},
                            "items": [{"name","uuid","target_uuid",...}, ...]}`
        The LLM tagged the runsheet item with a section name from the
        operator's template playlist. We emit the runsheet's own
        coloured header (preserving its title + notes + colour) and
        then the template's media items underneath. Operators stop
        dragging the same "Culture" / "Welcome" slides into every
        week's playlist.

      • A SINGLE-PRESENTATION dict — `{"uuid", "name", "index"}`. Older
        shape from the library-scan path; still honoured (emits one
        presentation entry).

      • None / missing → fall through to the original logic.

    Original resolution order (unchanged):
      • `match` set on a song → presentation pointing at the matched .pro
      • song with no `match` → red ACTION NEEDED placeholder
      • anything else → coloured header from the runsheet's own labelling."""
    items = []
    for mi in matched:
        p = mi.get("parsed") or {}
        m = mi.get("match")
        lib = p.get("library_match")

        # Section expansion — runsheet's own header (so "Culture: Lauren
        # and Fynn — 6:45 PM" keeps its context) followed by the template
        # section's items echoed in PP's native shape.
        #
        # PP-API rules locked in by live testing:
        #   • `id.uuid` mirrors the ASSET UUID (not the template's
        #     playlist-item UUID — PP 404s on that). Asset UUID location
        #     depends on `type`:
        #       media        → top-level `target_uuid`
        #       presentation → `presentation_info.presentation_uuid`
        #       (loops are presentation + `duration`)
        #   • Preserve the original `type` — media UUID sent as
        #     `type=presentation` gets looked up in the .pro library
        #     and 404s.
        #   • For loops, preserve `presentation_info` AND `duration` so
        #     PP keeps the loop behaviour in the new playlist.
        if (isinstance(lib, dict)
                and lib.get("header")
                and isinstance(lib.get("items"), list)):
            items.append(_coloured_header_for(p))
            for ti in lib["items"]:
                pinfo = ti.get("presentation_info") or {}
                # Asset UUID: presentation_info.presentation_uuid for
                # presentation + loop items, target_uuid for media. Fall
                # back to the bare uuid only for synthetic test fixtures
                # that lack both.
                asset = (pinfo.get("presentation_uuid")
                         or ti.get("target_uuid")
                         or ti.get("uuid", ""))
                item_type = ti.get("type") or "presentation"
                entry = {
                    "id":          {"uuid":  asset,
                                    "name":  ti.get("name", ""),
                                    "index": ti.get("index", 0)},
                    "type":        item_type,
                    # PP's PUT 400s with "missing field `target_uuid`"
                    # when this key is absent — even on presentation /
                    # loop items where PP's own GET omits it. Always
                    # emit; empty string is fine for non-media.
                    "target_uuid": ti.get("target_uuid", "") or "",
                    "is_hidden":   False, "is_pco": False,
                }
                # Echo the optional PP-shape fields when present so loops
                # keep their behaviour + presentation items keep their
                # .pro UUID + arrangement reference. Note: PP's REST GET
                # sometimes omits `duration` on the read-back even when
                # the loop is correctly set in the PP UI — verified that
                # loops do work end-to-end in the desktop app despite the
                # API not echoing the value back.
                if pinfo:
                    entry["presentation_info"] = pinfo
                if ti.get("duration") is not None:
                    entry["duration"] = ti["duration"]
                if ti.get("destination"):
                    entry["destination"] = ti["destination"]
                items.append(entry)
            continue

        # Single-presentation reuse (legacy shape) — one presentation entry.
        if isinstance(lib, dict) and lib.get("uuid"):
            eff = lib
        elif p.get("type") == "song" and m and m.get("uuid"):
            eff = m
        else:
            eff = None

        if eff:
            pres_uuid = eff.get("uuid", "")
            # is_hidden / is_pco are required by the PP API. We never produce
            # hidden items and don't integrate with Planning Center Online —
            # both stay False on every item we send.
            items.append({
                "id":          {"uuid":  pres_uuid,
                                "name":  eff.get("name", ""),
                                "index": eff.get("index", 0)},
                "type":        "presentation",
                "target_uuid": pres_uuid,
                "is_hidden":   False, "is_pco": False,
            })
        elif p.get("type") == "song":
            # Unmatched song → red ACTION NEEDED placeholder so the volunteer
            # notices and can manually add the song in PP.
            label = f"⚠ ACTION NEEDED — {p.get('title', '')}"
            if p.get("notes"):
                label += f"  ({p['notes']})"
            items.append({
                "id":           {"uuid": "", "name": label, "index": 0},
                "type":         "header",
                "target_uuid":  "",
                "is_hidden":    False, "is_pco": False,
                "header_color": _color_dict(ACTION_NEEDED_COLOR),
            })
        else:
            # Non-song, no match → coloured header from the runsheet's own
            # labelling. Make scripture items visually distinctive in PP —
            # operator uses the green colour + book emoji to spot them, then
            # opens PP's built-in Bible feature manually (PP's REST API has
            # no scripture endpoints in v7.21).
            items.append(_coloured_header_for(p))
    return items
