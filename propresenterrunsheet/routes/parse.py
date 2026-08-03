"""Runsheet parsing routes.

/api/upload_and_parse takes the operator's PDF, extracts text, sends it
to OpenRouter with the (user-customised or default) prompt, parses the
JSON response, fills any per-role cue gaps, and seeds the Service Mate
runsheet state so the clocks start showing the items even before
playlist creation.

/api/match runs each parsed song title through fuzzy_match against the
library."""

import datetime as _dt
import json
import logging
import re
import time

from flask import Blueprint, jsonify, request

from ..config import APP_NAME, UPLOAD_FOLDER
from ..parsing.ai import (
    DEFAULT_PROMPT, assemble_prompt, canonicalize_item_type,
    parse_ai_response,
)
from ..parsing.models import fetch_catalogue, is_router, resolve_model
from ..parsing.pdf import extract_pdf_text
from ..propresenter.library import fuzzy_match
from ..propresenter.templates import (
    auto_detect_template_uuid, fetch_pp_playlist_items, fetch_pp_playlists,
    playlist_to_objects, playlist_to_sections, resolve_object, resolve_section,
)
from ..service_mate.state import _ensure_item_cues, _write_runsheet_state
from ..logging_setup import log_safe
from ..settings import load_settings


bp = Blueprint("parse", __name__)
log = logging.getLogger("pp_runsheet")


def _unusable_reply_message(used_model: str, snippet: str, what: str) -> str:
    """Explain that a model answered but not with a runsheet.

    Names the model that actually replied and quotes it, because the two ways
    this fails are indistinguishable otherwise: a model that simply isn't up to
    the job, versus a router that happened to pick one that isn't. The router
    case gets an extra line, since "it worked last time" is the confusing part
    — `openrouter/free` chooses a different model on every request, so the same
    settings genuinely do succeed and fail at random.
    """
    msg = f"The model '{used_model}' {what}"
    if snippet:
        msg += f' — it replied: "{snippet}"'
    msg += ". "
    if is_router(model_id=used_model):
        msg += ("That id picks a different model at random each time, so it "
                "will keep failing intermittently. ")
    msg += "Open Settings and choose a model from the list."
    return msg


@bp.route("/api/upload_and_parse", methods=["POST"])
def api_upload_and_parse():
    import requests as req

    # 1. Validate request
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF uploaded"}), 400
    pdf_file = request.files["pdf"]
    if not pdf_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # 2. Save upload to a temp path (we delete it after extraction either way)
    tmp_path = UPLOAD_FOLDER / f"runsheet_{int(time.time()*1000)}.pdf"
    pdf_file.save(str(tmp_path))

    # 3. Resolve API key + model (form values override saved settings)
    settings = load_settings()
    or_key = (request.form.get("or_key") or settings.get("or_key") or "").strip()
    configured = (request.form.get("or_model")
                  or settings.get("or_model") or "").strip()
    # Blank means "pick one for me". Also rescues installs still holding a
    # model id that OpenRouter has since retired. The catalogue is cached for
    # hours and the fetch fails soft, so this costs one HTTP round-trip on the
    # first parse after launch and nothing afterwards.
    model = resolve_model(configured, fetch_catalogue())

    if not or_key:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error": "OpenRouter API key required."}), 400

    if not model:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error":
            "No AI model is set, and the list of free models could not be "
            "reached. Check your internet connection, or set a model "
            "manually in Settings."}), 400

    # Bound before the try so the error handlers can name the model that
    # actually answered and quote what it said. `used_model` diverges from
    # `model` whenever the operator points at a router id like
    # `openrouter/free`, which dispatches to a different underlying model on
    # every request — without this the logs only ever showed "openrouter/free"
    # and a misbehaving model was impossible to identify.
    used_model = model
    content = ""

    try:
        # 4. Extract text from the PDF (always clean up the temp file)
        try:
            raw = extract_pdf_text(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)

        if not raw.strip():
            return jsonify({"error":
                "Could not extract text from PDF. "
                "Make sure it is a text-based PDF (not a scanned image)."}), 400

        # 5. Assemble the prompt — user-customised or default, plus the
        # Service Mate cue addendum so the model also emits per-role cues.
        prompt_template = (settings.get("ai_prompt") or "").strip() or DEFAULT_PROMPT
        runsheet_text = raw[:7000]

        # 5a. If the operator has (or we can auto-detect) a "template
        # playlist" in PP — typically named "<Service> - Library" —
        # fetch it and feed the section header names into the prompt.
        # The model can then tag runsheet items with a section name; at
        # playlist-build time we expand each tagged item into that
        # section's full media list, so the operator doesn't drag the
        # same "Culture" / "Welcome" / "Worship" slides in every week.
        # Best-effort: any failure here (PP not running, no playlists,
        # template gone) drops back to parse-without-template.
        sections: list = []
        objects: list = []
        pp_host = (settings.get("pp_host") or "localhost").strip()
        pp_port = (settings.get("pp_port") or "50001").strip()
        base = f"http://{pp_host}:{pp_port}"
        tmpl_uuid = (settings.get("template_playlist_uuid") or "").strip()
        if not tmpl_uuid:
            # Auto-pick the template based on runsheet content. The hint
            # combines filename + the start of the extracted text — both
            # usually say "youth" / "sunday" / "wednesday" / etc., which
            # lets the picker route a youth runsheet to "Youth Service -
            # Library" and a sunday runsheet to "Sunday Morning Library"
            # automatically. Fall back to the first template-named
            # playlist on tie or no signal.
            detect_hint = " ".join(filter(None, [
                pdf_file.filename or "", raw[:500]]))
            try:
                tmpl_uuid = auto_detect_template_uuid(
                    fetch_pp_playlists(base),
                    hint=detect_hint) or ""
            except Exception:
                log.exception("template auto-detect failed; continuing without")
        if tmpl_uuid:
            try:
                raw_items = fetch_pp_playlist_items(base, tmpl_uuid)
                sections = playlist_to_sections(raw_items)
                # Item-level repository view of the same playlist. Real
                # operators often build the template FLAT — one named object
                # per reusable thing (Welcome slide, Countdown loop) with no
                # headers at all — which yields zero sections above. Objects
                # are matched per runsheet item further down.
                objects = playlist_to_objects(raw_items)
            except Exception:
                log.exception("template playlist fetch failed; "
                              "continuing without template context")
        section_names = [s["header"]["name"] for s in sections
                         if s.get("header") and s["header"].get("name")]

        prompt = assemble_prompt(prompt_template, runsheet_text,
                                 library_names=section_names)

        # 6. Call OpenRouter
        # Specific 4xx responses become friendly JSON errors (HTTP 200 so the
        # JS reads the message); everything else falls through to raise_for_status
        # and surfaces as a generic 500.
        log.info(f"OpenRouter request: model={log_safe(model)}, raw_chars={len(raw)}")
        resp = req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization":  f"Bearer {or_key}",
                "HTTP-Referer":   "runsheet-pilot",
                "X-Title":        APP_NAME,
                "Content-Type":   "application/json",
            },
            json={
                "model":       model,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=90,
        )
        if resp.status_code == 401:
            return jsonify({"error":
                "OpenRouter rejected the API key (401). "
                "Check the key in the sidebar."}), 200
        if resp.status_code == 402:
            return jsonify({"error":
                "OpenRouter says this account has no credit / model is paid (402). "
                "Try a different model — a free one is in the sidebar by default."}), 200
        if resp.status_code == 404:
            return jsonify({"error":
                f"OpenRouter says model '{model}' not found (404). "
                "Check the model id at openrouter.ai/models."}), 200
        resp.raise_for_status()

        # 7. Parse the AI response — strips markdown fences, accepts either
        # {service_name, items} (preferred) or a bare items array.
        body = resp.json()
        # OpenRouter echoes the model that actually served the request. For a
        # plain model id it matches what we asked for; for a router it names
        # the model the router chose.
        used_model = body.get("model") or model
        if used_model != model:
            log.info(f"OpenRouter routed {log_safe(model)} -> {log_safe(used_model)}")
        content = (body["choices"][0]["message"].get("content") or "")
        items, service_name = parse_ai_response(content)

        # A reply can be perfectly valid JSON and still not be a runsheet —
        # `{"safety": "safe"}` parses fine and yields zero items. Without this
        # guard the route treated that as success and fell through to the
        # Service Mate state seed below, which is an unconditional overwrite:
        # a junk parse silently wiped the live clock state mid-service.
        if not items:
            snippet = content.strip().replace("\n", " ")[:160]
            log.error(f"AI returned no runsheet items. model={log_safe(used_model)} "
                      f"reply={log_safe(snippet)!r}")
            return jsonify({"error": _unusable_reply_message(
                used_model, snippet, "returned no runsheet items")}), 200

        # 8. If the AI didn't supply a service name, derive one from the filename
        if not service_name and pdf_file.filename:
            stem = re.sub(r"\.pdf$", "", pdf_file.filename, flags=re.IGNORECASE)
            service_name = re.sub(r"[_]+", " ", stem).strip()

        # Fill any per-role cue gaps from the rule table so every item has
        # cues for the Service Mate clocks. Also resolve any `library_match`
        # name the model emitted back to a real section dict (header +
        # media items), so build_playlist_payload can expand it into the
        # template's slides. Hallucinated names (no section hit) get
        # dropped to None and the item falls back to existing paths.
        resolved_section_hits = 0
        resolved_object_hits = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            # Clamp the type to the fixed list FIRST — everything after
            # this point (cue lookup, song-vs-object routing, tag colours,
            # timer creation) keys off it, and the model demonstrably
            # invents types no matter what the prompt says.
            raw_type = it.get("type")
            it["type"] = canonicalize_item_type(raw_type)
            if raw_type != it["type"]:
                log.info(f"Item type clamped: {log_safe(raw_type, 60)!r} -> "
                         f"{it['type']!r} ({log_safe(it.get('title'), 40)!r})")
            _ensure_item_cues(it)
            raw_match = it.get("library_match")
            # The model sometimes returns the full dict, sometimes a bare
            # string, sometimes null, sometimes the literal "null" str.
            # We only care about the string case — resolve it to a section.
            name = ""
            if isinstance(raw_match, str):
                name = raw_match.strip()
                if name.lower() in ("", "null", "none"):
                    name = ""
            elif isinstance(raw_match, dict):
                # Defensive: some models try to be helpful and return a
                # {"name": "Culture"} dict instead of the bare string.
                name = (raw_match.get("name") or "").strip()
            section = resolve_section(name, sections) if name else None
            if section:
                it["library_match"] = section
                resolved_section_hits += 1
                continue
            # Item-level fallback: match the runsheet title against the
            # template's named objects ("Welcome and Connection Cards" →
            # the "Welcome" slide). Wrapped in the SECTION shape — header
            # + one item — because everything downstream (the /api/match
            # passthrough, the ♻ render in the UI, build_playlist_payload's
            # expander with its PP asset-UUID rules) already handles that
            # shape; the generated playlist keeps the runsheet's own
            # coloured header with the template object underneath.
            # Songs are deliberately excluded: they belong to the
            # fuzzy-match + Pick flow, and a template slide named
            # "Worship" must not hijack a song titled "Worship Medley".
            obj = (resolve_object(it.get("title", ""), objects)
                   if it.get("type") != "song" else None)
            if obj:
                it["library_match"] = {
                    "header": {"name": obj["name"], "uuid": obj["uuid"],
                               "color": {}},
                    "items":  [obj],
                }
                resolved_object_hits += 1
            else:
                it["library_match"] = None
        if sections or objects:
            log.info(f"Template-context parse: "
                     f"{resolved_section_hits} section + "
                     f"{resolved_object_hits} object links across "
                     f"{len(items)} items (template: {len(sections)} "
                     f"sections, {len(objects)} objects)")

        # Also seed the Service Mate runsheet state on parse — so the user can
        # test the clock cue flow without going through Create Playlist (which
        # requires ProPresenter to be running). Create Playlist later overwrites
        # this with the timer-name-stamped version for auto-track.
        try:
            sm_state = {
                "service_name":       service_name or pdf_file.filename or "Runsheet",
                "items":              items,
                "current_index":      0,
                "current_started_at": _dt.datetime.now().isoformat(),
                "auto_track":         {"enabled": True},
            }
            _write_runsheet_state(sm_state)
            log.info(f"Service Mate state seeded from parse: {len(items)} items")
        except Exception:
            log.exception("Service Mate parse-time state write failed")

        log.info(f"AI parsed {len(items)} runsheet items, "
                 f"suggested name: {log_safe(service_name)!r}")
        return jsonify({
            "items":          items,
            "filename":       pdf_file.filename,
            "suggested_name": service_name,
        })

    except json.JSONDecodeError:
        # The operator used to see the raw decoder message here ("Expecting
        # value: line 1 column 1 (char 0)"), which told them nothing. What
        # they need is which model answered and what it actually said.
        snippet = (content or "").strip().replace("\n", " ")[:160]
        log.error(f"AI returned non-JSON. model={log_safe(used_model)} "
                  f"reply={log_safe(snippet)!r}")
        return jsonify({"error": _unusable_reply_message(
            used_model, snippet, "didn't return a runsheet")}), 200
    except req.exceptions.Timeout:
        return jsonify({"error":
            "OpenRouter request timed out. Try again, or pick a faster model."}), 200
    except Exception as e:
        log.exception("Parse failed")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/match", methods=["POST"])
def api_match():
    body = request.get_json(silent=True) or {}
    parsed = body.get("parsed", [])
    library = body.get("library", [])
    threshold = float(body.get("threshold", 0.55))
    results = []
    for item in parsed:
        # Priority 1: the parse step already linked this item to a template
        # section via the LLM (`library_match` is a section dict with
        # `header` + `items` after parse-time resolution). Surface it as
        # the match so the UI can show ♻ + slide count and so
        # build_playlist_payload can expand it. Confidence 1.0 — the LLM
        # had the section names in front of it and we already validated.
        lib = item.get("library_match")
        if isinstance(lib, dict) and lib.get("header") and lib.get("items") is not None:
            results.append({"parsed": item, "match": lib, "confidence": 1.0})
            continue
        # Priority 2: existing song-only fuzzy match against whatever
        # library the UI sent in this request — unchanged behaviour for
        # songs the LLM didn't pre-link.
        if item.get("type") == "song" and library:
            match, conf = fuzzy_match(item.get("title", ""), library, threshold)
        else:
            match, conf = None, 0.0
        results.append({"parsed": item, "match": match,
                        "confidence": round(conf, 3)})
    return jsonify({"items": results})
