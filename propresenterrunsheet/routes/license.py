"""Flask blueprint for /api/license — Service Mate add-on activation.

The frontend uses these to render the licence state (trial / licensed /
expired) and to activate a key the buyer pastes in Settings. The raw key
is never echoed back; only the derived status is returned."""

import logging

from flask import Blueprint, jsonify, request

from ..licensing import service_mate_status, verify_license
from ..settings import load_settings, save_settings


bp = Blueprint("license", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/api/license", methods=["GET"])
def api_license_get():
    """Return the current Service Mate entitlement for the UI."""
    status = service_mate_status()
    status["has_key"] = bool((load_settings().get("license_key") or "").strip())
    return jsonify(status)


@bp.route("/api/license", methods=["POST"])
def api_license_post():
    """Activate a licence key. Validates the signature server-side so the
    user gets immediate valid/invalid feedback, then persists it on success."""
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()

    # Empty key = "remove my licence" (revert to trial/expired state).
    if not key:
        save_settings({"license_key": ""})
        return jsonify({"ok": True, **service_mate_status(), "has_key": False})

    payload = verify_license(key)
    if not payload or payload.get("p") != "service_mate":
        log.info("Licence activation rejected (invalid or wrong product)")
        return jsonify({"ok": False,
                        "error": "That licence key wasn't recognised. "
                                 "Check you pasted the whole thing, or "
                                 "contact support."}), 400

    save_settings({"license_key": key})
    log.info("Service Mate licence activated for '%s'", payload.get("n"))
    return jsonify({"ok": True, **service_mate_status(), "has_key": True})
