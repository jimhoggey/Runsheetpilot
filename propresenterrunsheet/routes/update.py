"""Flask blueprint for /api/update — self-update status + trigger.

GET  /api/update           -> current updater state (safe to poll)
GET  /api/update?refresh=1 -> re-check GitHub synchronously, then state
POST /api/update/apply     -> start download+verify+swap in a worker thread

Dev mode (running from source) reports state="dev" and refuses apply —
the updater only ever operates on the frozen bundle."""

import logging
import sys
import threading

from flask import Blueprint, jsonify, request

from .. import updater


bp = Blueprint("update", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/api/update", methods=["GET"])
def api_update_get():
    if not getattr(sys, "frozen", False):
        return jsonify({"state": "dev", "current": updater.VERSION})
    if request.args.get("refresh"):
        updater.check_for_update()
    return jsonify(updater.get_state())


@bp.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    if not getattr(sys, "frozen", False):
        return jsonify({"ok": False,
                        "error": "Self-update only runs in the installed "
                                 "app, not from source."}), 409
    state = updater.get_state()
    if state["state"] not in ("available", "error"):
        return jsonify({"ok": False,
                        "error": "No update available to apply."}), 409
    threading.Thread(target=updater.apply_update, daemon=True,
                     name="update-apply").start()
    return jsonify({"ok": True})
