"""Per-request flags shared by the parse and playlist routes.

Lives in its own module so `playlist.py` doesn't have to import from
`parse.py` — routes importing each other is the start of an import cycle,
and this is the only thing they'd need from one another.
"""

# Form values that mean "off". Checkboxes, JSON booleans stringified by a
# form encoder, and hand-written clients all disagree about how to spell
# false, and the cost of getting it wrong is silently matching when the
# operator asked not to.
_FALSEY = {"off", "false", "0", "no", ""}


def matching_enabled(source) -> bool:
    """True unless the caller explicitly turned matching off.

    Backs the "Populate with media from PP" toggle. Defaults to ON so an
    operator who never touches it — and any older client that doesn't
    send the field at all — gets exactly the behaviour they had before it
    existed. `source` is a form dict or a parsed JSON body.
    """
    raw = source.get("matching")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in _FALSEY
