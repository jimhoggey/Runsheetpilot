"""User-settings routes — /api/settings (GET/POST) and /api/prompt (GET/POST).

GET /api/settings tacks on the discovered ProPresenter root and library
folders so the UI can pre-fill its dropdowns; the POST is a partial
update that merges into whatever's already on disk."""

import sys

from flask import Blueprint, jsonify, request

from ..config import DATA_DIR, VERSION
from ..parsing.ai import DEFAULT_PROMPT
from ..parsing.models import fetch_catalogue, pick_default_model, usable_models
from ..propresenter.paths import find_library_dirs, find_pp_root
from ..settings import load_settings, save_settings


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


@bp.route("/api/settings", methods=["GET"])
def get_settings():
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
