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
from ..parsing.ai import DEFAULT_PROMPT, assemble_prompt, parse_ai_response
from ..parsing.pdf import extract_pdf_text
from ..propresenter.library import fuzzy_match
from ..service_mate.state import _ensure_item_cues, _write_runsheet_state
from ..settings import _default_settings, load_settings


bp = Blueprint("parse", __name__)
log = logging.getLogger("pp_runsheet")


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
    model = (request.form.get("or_model")
             or settings.get("or_model")
             or _default_settings()["or_model"]).strip()

    if not or_key:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error": "OpenRouter API key required."}), 400

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
        prompt = assemble_prompt(prompt_template, runsheet_text)

        # 6. Call OpenRouter
        # Specific 4xx responses become friendly JSON errors (HTTP 200 so the
        # JS reads the message); everything else falls through to raise_for_status
        # and surfaces as a generic 500.
        log.info(f"OpenRouter request: model={model}, raw_chars={len(raw)}")
        resp = req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization":  f"Bearer {or_key}",
                "HTTP-Referer":   "propresenter-runsheet-builder",
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
        content = resp.json()["choices"][0]["message"]["content"]
        items, service_name = parse_ai_response(content)

        # 8. If the AI didn't supply a service name, derive one from the filename
        if not service_name and pdf_file.filename:
            stem = re.sub(r"\.pdf$", "", pdf_file.filename, flags=re.IGNORECASE)
            service_name = re.sub(r"[_]+", " ", stem).strip()

        # Fill any per-role cue gaps from the rule table so every item has
        # cues for the Service Mate clocks.
        for it in items:
            if isinstance(it, dict):
                _ensure_item_cues(it)

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
                 f"suggested name: {service_name!r}")
        return jsonify({
            "items":          items,
            "filename":       pdf_file.filename,
            "suggested_name": service_name,
        })

    except json.JSONDecodeError as e:
        log.exception("AI returned invalid JSON")
        return jsonify({"error": f"AI response was not valid JSON: {e}"}), 500
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
        if item.get("type") == "song" and library:
            match, conf = fuzzy_match(item.get("title", ""), library, threshold)
        else:
            match, conf = None, 0.0
        results.append({"parsed": item, "match": match,
                        "confidence": round(conf, 3)})
    return jsonify({"items": results})
