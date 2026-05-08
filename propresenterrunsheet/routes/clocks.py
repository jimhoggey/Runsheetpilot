"""Flask blueprint for /api/clocks/* endpoints.

Configure clock IPs / brightness / verbosity, probe + test individual
devices, render an inline preview, and reset everything to standby."""

import datetime as _dt

from flask import Blueprint, Response, jsonify, request

from ..service_mate.constants import (
    ROLE_ACCENT, SM_TESTCARD_FILENAME, SM_VERBOSITIES, SM_VERBOSITY_DEFAULT,
)
from ..service_mate.daemon import _CLOCKS_LOOP_LAST_PUSHED
from ..service_mate.geekmagic import (
    _probe_clock, _push_to_clock, _set_clock_brightness,
)
from ..service_mate.render import _render_cue, _render_standby, _render_test_card
from ..service_mate.state import (
    _read_clocks_config, _read_runsheet_state, _write_clocks_config,
    _write_runsheet_state,
)


bp = Blueprint("clocks", __name__)


@bp.route("/api/clocks", methods=["GET"])
def api_clocks_get():
    return jsonify(_read_clocks_config())


@bp.route("/api/clocks", methods=["POST"])
def api_clocks_post():
    body = request.get_json(silent=True) or {}
    cfg = _read_clocks_config()
    if "clocks" in body and isinstance(body["clocks"], list):
        cleaned = []
        for c in body["clocks"]:
            if not isinstance(c, dict):
                continue
            verbosity = (c.get("verbosity") or SM_VERBOSITY_DEFAULT).strip().lower()
            if verbosity not in SM_VERBOSITIES:
                verbosity = SM_VERBOSITY_DEFAULT
            cleaned.append({
                "id":        (c.get("id") or c.get("role") or "").strip().lower(),
                "ip":        (c.get("ip") or "").strip(),
                "role":      (c.get("role") or c.get("id") or "screen").strip().lower(),
                "name":      (c.get("name") or "").strip(),
                "verbosity": verbosity,
            })
        cfg["clocks"] = cleaned
    if "brightness" in body:
        try:
            cfg["brightness"] = max(1, min(100, int(body["brightness"])))
        except Exception:
            pass
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    _write_clocks_config(cfg)
    return jsonify({"ok": True, "config": cfg})


@bp.route("/api/clocks/<clock_id>/probe", methods=["POST"])
def api_clock_probe(clock_id: str):
    cfg = _read_clocks_config()
    clock = next((c for c in cfg.get("clocks", [])
                  if c.get("id") == clock_id), None)
    if not clock:
        return jsonify({"error": "unknown clock"}), 404
    ip = (clock.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "no IP set"}), 200
    return jsonify(_probe_clock(ip))


@bp.route("/api/clocks/<clock_id>/test", methods=["POST"])
def api_clock_test(clock_id: str):
    cfg = _read_clocks_config()
    clock = next((c for c in cfg.get("clocks", [])
                  if c.get("id") == clock_id), None)
    if not clock:
        return jsonify({"error": "unknown clock"}), 404
    ip = (clock.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "no IP set"}), 200
    role = clock.get("role") or "screen"
    jpg = _render_test_card(role, ip)
    if cfg.get("brightness"):
        _set_clock_brightness(ip, int(cfg["brightness"]))
    ok = _push_to_clock(ip, jpg, filename=SM_TESTCARD_FILENAME)
    # Test card and live cue are different files on the device — clearing the
    # last-pushed hash for this clock means the next loop tick re-pushes the
    # cue image, returning the device to the live view within ~1 s. Otherwise
    # the test card would stay until the cue content next changed.
    _CLOCKS_LOOP_LAST_PUSHED.pop(clock_id, None)
    return jsonify({"ok": ok})


@bp.route("/api/clocks/standby", methods=["POST"])
def api_clocks_standby():
    """Reset all clocks to the pre-service waiting page. Persists `standby:true`
    in the runsheet state so the daemon keeps pushing the standby image; the
    flag is cleared automatically on the next runsheet load (parse / create
    playlist / explicit POST /api/runsheet/state with items)."""
    state = {
        "standby": True,
        "items": [],
        "current_index": 0,
        "current_started_at": _dt.datetime.now().isoformat(),
    }
    _write_runsheet_state(state)
    # Force every clock to re-push on the next loop tick (~500 ms) instead of
    # waiting for the next content change. Without this, clocks that are
    # already showing the standby image (e.g. after a server restart) wouldn't
    # tick refresh until the 40 s anti-bitrot push.
    _CLOCKS_LOOP_LAST_PUSHED.clear()
    return jsonify({"ok": True})


@bp.route("/api/clocks/preview", methods=["GET"])
def api_clocks_preview():
    """Return the rendered JPEG for a given role + verbosity — used by the UI
    for an inline preview without the device, and for development."""
    role = (request.args.get("role") or "screen").lower()
    if role not in ROLE_ACCENT:
        role = "screen"
    verbosity = (request.args.get("verbosity") or SM_VERBOSITY_DEFAULT).lower()
    if verbosity not in SM_VERBOSITIES:
        verbosity = SM_VERBOSITY_DEFAULT
    state = _read_runsheet_state() or {}
    # If the operator hit Standby (or saved an explicit standby flag), preview
    # mirrors what the device is actually showing right now.
    if state.get("standby"):
        return Response(_render_standby(role), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    # No runsheet at all → fall through to demo data so first-time users can
    # see what a live cue will look like before loading a PDF.
    if not state.get("items"):
        state = {
            "items": [
                {"type": "song", "title": "Build My Life", "duration_min": 5,
                 "notes": "9:30 AM",
                 "cues": {"screen": "Cue song slides",
                          "sound":  "Band mics live · MC mute",
                          "lights": "Stage wash — band"}},
                {"type": "sermon", "title": "King Jesus — Ps Nick", "duration_min": 30,
                 "notes": "10:14 AM",
                 "cues": {"screen": "Sermon slides",
                          "sound":  "Mic on for Ps Nick",
                          "lights": "Spot — preacher"}}
            ],
            "current_index": 0,
            "current_started_at": _dt.datetime.now().isoformat(),
        }
    jpg = _render_cue(role, state, verbosity=verbosity)
    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})
