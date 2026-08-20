"""Routes for the downloaded-media assist panel.

Behind the `media_assist` setting, off by default: it lists filenames
from the operator's Downloads folder, which is not something to switch
on for someone without asking.

The panel exists because the last step of getting media into
ProPresenter is irreducibly manual — PP's media API is read-only and its
Media bin is a registry, so a file copied into the media folder stays
invisible (verified against a real install). What the app CAN do is find
the file, say which runsheet item it belongs to, and notice the moment
it lands in the bin.
"""

import logging
import subprocess
import sys

from flask import Blueprint, jsonify, request

from .. import media_assist
from .. import service_visuals
from .. import stats
from ..logging_setup import log_safe
from ..propresenter.media_bin import fetch_media_bin
from ..propresenter.net import pp_base
from ..settings import load_settings

bp = Blueprint("media_assist", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/api/media_assist", methods=["POST"])
def api_media_assist():
    """Recent downloads, each with a suggested runsheet item and whether
    ProPresenter already has it.

    POST (not GET) because the parsed runsheet is the input — the
    suggestions are worthless without it.
    """
    settings = load_settings()
    if not settings.get("media_assist"):
        return jsonify({"enabled": False, "files": []})

    body = request.get_json(silent=True) or {}
    items = body.get("items") or []

    # The bin is best-effort: with PP closed the panel still lists files
    # and suggestions, it just can't say which are already imported.
    # Built outside the try: an unreachable host is a refusal the caller
    # must see, not something to hide behind an empty bin.
    base = pp_base(body.get("host") or settings.get("pp_host"),
                   body.get("port") or settings.get("pp_port"))
    bin_names, bin_ok = [], False
    try:
        bin_names = [b.get("name") or "" for b in fetch_media_bin(base)]
        # fetch_media_bin returns [] for BOTH "PP unreachable" and "bin
        # is empty", so ask PP directly whether it is there at all —
        # otherwise files imported last week render as "to import".
        import requests as _rq
        bin_ok = _rq.get(f"{base}/version", timeout=2).ok
    except Exception:
        log.debug("media assist: could not read the Media bin")

    dirs = settings.get("media_assist_dirs") or None
    try:
        files = media_assist.build(items, dirs, bin_names=bin_names)
    except Exception:
        log.exception("media assist scan failed")
        return jsonify({"enabled": True, "files": [], "timers": [],
                        "bin_ok": bin_ok,
                        "error": "Couldn't read your downloads folder."})

    # Countdowns the runsheet asked for that nobody has yet. Only rows
    # that explicitly say so, and only states needing a human — a Sunday
    # service whose Countdown already lives in the template is silent,
    # because telling someone to render a timer they have reused every
    # week for a year is how a panel earns its way into being ignored.
    timers = []
    try:
        timers = service_visuals.review(items, bin_names=bin_names)
    except Exception:
        log.exception("service visuals timer review failed")

    pending = [f for f in files if not f["in_bin"]]
    # Not on polls: the panel refreshes every few seconds while open, and
    # counting each tick would turn a per-view metric into a measure of
    # how long a window stayed on screen — and flood the 128-slot queue.
    if not body.get("poll"):
        stats.track("media_assist_shown", files=len(files),
                    pending=len(pending),
                    suggested=sum(1 for f in files if f["suggested"]),
                    timers_missing=sum(1 for t in timers
                                       if t["state"] == "missing"),
                    timers_unimported=sum(1 for t in timers
                                          if t["state"] == "rendered"))
    log.info("Media assist: %d recent file(s), %d not yet in PP",
             len(files), len(pending))
    return jsonify({"enabled": True, "bin_ok": bin_ok, "files": files,
                    "timers": timers})


@bp.route("/api/media_assist/reveal", methods=["POST"])
def api_media_assist_reveal():
    """Open the OS file manager with the file selected, ready to drag.

    The path must be one the scan just returned — never whatever the
    caller sends. This opens a file manager window, so accepting an
    arbitrary path from the request would be handing out a file browser.
    """
    settings = load_settings()
    if not settings.get("media_assist"):
        return jsonify({"ok": False, "error": "Media assist is off."}), 403

    wanted = str((request.get_json(silent=True) or {}).get("path") or "")
    allowed = {f["path"] for f in media_assist.scan(
        settings.get("media_assist_dirs") or None)}
    if wanted not in allowed:
        log.warning("media assist: refused reveal of %s", log_safe(wanted))
        return jsonify({"ok": False,
                        "error": "That file isn't in the list any more."}), 400

    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", wanted], timeout=5, check=False)
        elif sys.platform == "win32":
            # ONE token: Explorer ignores the selection when the switch
            # and the path arrive as separate arguments, and silently
            # opens the default folder instead.
            subprocess.run(["explorer", f"/select,{wanted}"], timeout=5,
                           check=False)
        else:
            return jsonify({"ok": False,
                            "error": "Only supported on macOS and Windows."}), 400
    except Exception:
        log.exception("could not reveal file")
        return jsonify({"ok": False, "error": "Couldn't open the folder."}), 200
    stats.track("media_assist_reveal")
    return jsonify({"ok": True})
