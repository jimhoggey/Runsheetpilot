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


def build_playlist_payload(matched: list) -> list:
    """Assemble the items list to PUT to PP's /v1/playlist/{uuid}.

    Pure function — no I/O, no globals — so it's straightforward to test.
    Each input element is a dict like:

        {"parsed": {"type": "song", "title": "...", "notes": "..."},
         "match":  {"uuid": "...", "name": "...", "index": 0}}

    `match` is None when the AI parsed a song but the fuzzy library lookup
    didn't find a confident hit — we emit a red ACTION-NEEDED header so the
    volunteer adds it manually."""
    items = []
    for mi in matched:
        p = mi.get("parsed") or {}
        m = mi.get("match")
        if p.get("type") == "song" and m:
            pres_uuid = m.get("uuid", "")
            # is_hidden / is_pco are required by the PP API. We never produce
            # hidden items and don't integrate with Planning Center Online —
            # both stay False on every item we send.
            items.append({
                "id":          {"uuid":  pres_uuid,
                                "name":  m.get("name", ""),
                                "index": m.get("index", 0)},
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
            title = p.get("title", "") or ""
            # Make scripture items visually distinctive in PP — operator
            # uses the green colour + book emoji to spot them, then opens
            # PP's built-in Bible feature manually for the actual verses
            # (PP's REST API has no scripture endpoints in v7.21).
            if p.get("type") == "scripture":
                title = f"📖 {title}"
            label = title
            if p.get("notes"):
                label += f"  —  {p['notes']}"
            items.append({
                "id":           {"uuid": "", "name": label, "index": 0},
                "type":         "header",
                "target_uuid":  "",
                "is_hidden":    False, "is_pco": False,
                "header_color": _color_for_type(p.get("type")),
            })
    return items
