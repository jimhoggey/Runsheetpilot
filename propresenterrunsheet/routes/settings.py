"""User-settings routes — /api/settings (GET/POST) and /api/prompt (GET/POST).

GET /api/settings tacks on the discovered ProPresenter root and library
folders so the UI can pre-fill its dropdowns; the POST is a partial
update that merges into whatever's already on disk."""

import sys

from flask import Blueprint, jsonify, request

from ..config import DATA_DIR, VERSION, WHATS_NEW
from ..parsing.ai import DEFAULT_PROMPT
from ..parsing.models import fetch_catalogue, pick_default_model, usable_models
from ..propresenter.paths import find_library_dirs, find_pp_root
from ..settings import load_settings, save_settings
from .. import stats


bp = Blueprint("settings", __name__)


@bp.route("/api/models", methods=["GET"])
def get_models():
    """Free models that can return the JSON the parser needs, best first.

    Fetched live from OpenRouter rather than hardcoded — a baked-in list goes
    stale the moment a model is retired, which is exactly how the old default
    (`google/gemini-2.0-flash-exp:free`) ended up 404ing for everyone.

    `auto` is what an unset model setting resolves to, so the dropdown can
    label the automatic option without re-implementing the ranking. Both go
    empty rather than erroring when OpenRouter can't be reached, so Settings
    still opens offline.
    """
    catalogue = fetch_catalogue()
    if not catalogue:
        return jsonify({"models": [], "auto": None, "available": False})
    models = [{"id": m["id"],
               "name": m.get("name") or m["id"],
               "context_length": m.get("context_length") or 0}
              for m in usable_models(catalogue)]
    return jsonify({"models": models,
                    "auto": pick_default_model(catalogue),
                    "available": True})


@bp.route("/api/whats_new", methods=["GET"])
def get_whats_new():
    """Whether to show the once-per-version what's-new popup, and its notes.

    Shows when the running VERSION differs from the last one this install
    recorded — which catches the in-app updater's relaunch AND a manually
    installed DMG, because the trigger is the version change itself, not
    the update action.

    A fresh install (no recorded version) never shows: nothing is "new"
    to someone seeing the app for the first time — their first sight
    should be the welcome greeter, not a changelog. Recording the current
    version here is what arms the popup for their NEXT update.

    Deliberately does NOT mark the version seen — only the dismiss POST
    does. If the app dies between this call and the popup rendering, the
    notes survive to the next launch instead of being silently eaten.
    """
    last = (load_settings().get("last_seen_version") or "").strip()
    if not last:
        save_settings({"last_seen_version": VERSION})
        return jsonify({"show": False, "version": VERSION, "notes": []})
    if last != VERSION:
        stats.track("whats_new_shown", from_version=last)
    return jsonify({
        "show":    last != VERSION,
        "version": VERSION,
        "notes":   list(WHATS_NEW)[:3],
    })


@bp.route("/api/whats_new/seen", methods=["POST"])
def post_whats_new_seen():
    """The popup was dismissed — never show these notes again.

    save_settings merges into the file, so the operator's key/host are
    untouched.
    """
    save_settings({"last_seen_version": VERSION})
    return jsonify({"ok": True})


@bp.route("/api/settings", methods=["GET"])
def get_settings():
    # The UI has successfully reached the backend, so this launch is not
    # a "won't open" case — clear the boot marker. This is the first call
    # the front end makes, and doing it here (not on every poll) keeps it
    # to one filesystem touch per run.
    stats.boot_ok()
    s = load_settings()
    pp_root = find_pp_root()
    s["pp_root"] = pp_root
    s["library_dirs"] = find_library_dirs(pp_root)
    s["platform"] = sys.platform
    s["version"] = VERSION
    s["data_dir"] = str(DATA_DIR)
    return jsonify(s)


@bp.route("/api/settings", methods=["POST"])
def post_settings():
    body = request.get_json(silent=True) or {}
    save_settings(body)
    # Apply the analytics toggle immediately — waiting for a restart to
    # honour "turn this off" is not a real opt-out.
    if "stats_enabled" in body:
        stats.set_enabled(bool(body.get("stats_enabled")))
    stats.track("settings_saved", keys=len(body))
    return jsonify({"ok": True})


@bp.route("/api/prompt", methods=["GET"])
def get_prompt():
    saved = (load_settings().get("ai_prompt") or "").strip()
    return jsonify({
        "prompt":     saved or DEFAULT_PROMPT,
        "is_default": not saved,
        "default":    DEFAULT_PROMPT,
    })


@bp.route("/api/prompt", methods=["POST"])
def post_prompt():
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    # Empty string is meaningful → "revert to default".
    save_settings({"ai_prompt": prompt if isinstance(prompt, str) else ""})
    return jsonify({"ok": True})
