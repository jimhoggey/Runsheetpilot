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

from flask import Blueprint, jsonify, request

from .. import media_assist
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
    bin_names, bin_ok = [], False
    try:
        base = pp_base(body.get("host") or settings.get("pp_host"),
                       body.get("port") or settings.get("pp_port"))
        bin_names = [b.get("name") or "" for b in fetch_media_bin(base)]
        bin_ok = bool(bin_names)
    except Exception:
        log.debug("media assist: could not read the Media bin")

    dirs = settings.get("media_assist_dirs") or None
    try:
        files = media_assist.build(items, dirs, bin_names=bin_names)
    except Exception:
        log.exception("media assist scan failed")
        return jsonify({"enabled": True, "files": [], "bin_ok": bin_ok,
                        "error": "Couldn't read your downloads folder."})

    pending = [f for f in files if not f["in_bin"]]
    stats.track("media_assist_shown", files=len(files), pending=len(pending),
                suggested=sum(1 for f in files if f["suggested"]))
    log.info("Media assist: %d recent file(s), %d not yet in PP",
             len(files), len(pending))
    return jsonify({"enabled": True, "bin_ok": bin_ok, "files": files})


@bp.route("/api/media_assist/reveal", methods=["POST"])
def api_media_assist_reveal():
    """Open the OS file manager with the file selected, ready to drag.

    The path must be one the scan just returned — never whatever the
    caller sends. This opens a file manager window, so accepting an
    arbitrary path from the request would be handing out a file browser.
    """
    import subprocess
    import sys

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
            subprocess.run(["explorer", "/select,", wanted], timeout=5,
                           check=False)
        else:
            return jsonify({"ok": False,
                            "error": "Only supported on macOS and Windows."}), 400
    except Exception:
        log.exception("could not reveal file")
        return jsonify({"ok": False, "error": "Couldn't open the folder."}), 200
    stats.track("media_assist_reveal")
    return jsonify({"ok": True})
