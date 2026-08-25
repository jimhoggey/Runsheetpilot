"""User-settings routes — /api/settings (GET/POST) and /api/prompt (GET/POST).

GET /api/settings tacks on the discovered ProPresenter root and library
folders so the UI can pre-fill its dropdowns; the POST is a partial
update that merges into whatever's already on disk."""

import sys

from flask import Blueprint, jsonify, request

from ..config import DATA_DIR, VERSION, WHATS_NEW
from ..parsing.ai import DEFAULT_PROMPT
from ..parsing.models import (
    fetch_catalogue, fetch_key_info, pick_default_model,
    recommended_models, usable_models,
)
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
    # force=True: the catalogue is cached for hours, which is right for
    # the parse path but wrong here — this is the moment the operator is
    # LOOKING at the list, and a model withdrawn since launch must not
    # still be offered. One request, fails soft to the cache.
    catalogue = fetch_catalogue(force=True)
    # Is the key funded? Paid models are offered only when it is —
    # showing them to someone who can't pay produces a 402 on their first
    # parse, which is a far worse first impression than a shorter list.
    # Unknown (offline, bad key) is treated as not funded.
    key = (load_settings().get("or_key") or "").strip()
    info = fetch_key_info(key)
    funded = bool(info.get("funded"))

    if not catalogue:
        return jsonify({"models": [], "auto": None, "available": False,
                        "recommended": [], "funded": funded})
    models = [{"id": m["id"],
               "name": m.get("name") or m["id"],
               "context_length": m.get("context_length") or 0}
              for m in usable_models(catalogue)]
    return jsonify({"models": models,
                    # Automatic is FREE-ONLY, by design: it must work on
                    # a fresh install with an unfunded key, and it must
                    # never start spending without being asked.
                    "auto": pick_default_model(catalogue),
                    "recommended": recommended_models(catalogue) if funded
                                   else [],
                    "funded": funded,
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
    # Read BEFORE writing — this is the only moment the old values still
    # exist, and diffing here rather than in the browser means every writer
    # is covered, including the ones that bypass the UI's autosave (the
    # model dropdown, and the port-discovery writeback that saves without
    # anyone touching the keyboard).
    before = load_settings()
    save_settings(body)
    # Apply the analytics toggle immediately — waiting for a restart to
    # honour "turn this off" is not a real opt-out. Deliberately before
    # track(): someone who just switched analytics OFF must not have that
    # very action phoned home.
    if "stats_enabled" in body:
        stats.set_enabled(bool(body.get("stats_enabled")))
    # keys= is kept so existing charts don't go blank; it is now the least
    # informative thing in the event.
    stats.track("settings_saved", keys=len(body),
                **stats.settings_change_props(before, body))
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
