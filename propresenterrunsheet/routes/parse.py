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
from ..parsing.models import (
    fetch_catalogue, is_router, next_usable_model, resolve_model,
)
from .flags import matching_enabled
from ..parsing.ocr import (
    OCRUnavailable, image_to_text, images_to_text,
)
from ..parsing.pdf import extract_pdf_text, pdf_text_or_images, render_pdf_pages
from ..parsing.timed_rows import rescue_missing_rows
from ..propresenter.library import fuzzy_match
from ..propresenter.net import pp_base
from ..propresenter.templates import (
    auto_detect_template_uuid, fetch_pp_playlist_items, fetch_pp_playlists,
    link_items_to_template, playlist_to_objects, playlist_to_sections,
    resolve_object, resolve_section, resolve_with_aliases,
)
from ..service_mate.state import _ensure_item_cues, _write_runsheet_state
from ..logging_setup import log_safe
from ..settings import load_settings


bp = Blueprint("parse", __name__)
log = logging.getLogger("pp_runsheet")

# What the upload accepts. PDFs go through pdfplumber; images (and PDFs
# pdfplumber can't read) go through local OCR. Deliberately NOT here:
# .docx and .doc — Word runsheets are almost always tables, which is a
# separate extraction problem, and HEIC, which needs another dependency
# for a case screenshots already cover.
PDF_EXTS = (".pdf",)
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
ALLOWED_EXTS = PDF_EXTS + IMAGE_EXTS


class UploadError(ValueError):
    """A failure whose message was written FOR the operator.

    Exists so `_extracted_or_error` can tell OUR messages apart from a
    third-party library's. ocrmac raises ValueError too ("Invalid image
    format…"), and passing that straight to the UI leaks internals while
    telling the operator nothing they can act on.
    """


def _safe_ext(filename: str) -> str:
    """Return the whitelisted extension this filename ends with, or "".

    The return value is always a literal from `ALLOWED_EXTS` — never a
    slice of the caller's string. That matters because it is concatenated
    into a temp-file path: deriving it from user input is a path-injection
    hole, and the whitelist check alone leaves the safety implicit.
    """
    lowered = (filename or "").lower()
    for ext in ALLOWED_EXTS:
        if lowered.endswith(ext):
            return ext
    return ""


def _display_ext(filename: str) -> str:
    """A sanitised extension to quote back in an error message."""
    tail = (filename or "").rsplit(".", 1)[-1] if "." in (filename or "") else ""
    return "." + re.sub(r"[^A-Za-z0-9]", "", tail)[:10] if tail else ""


def _upload_to_text(upload):
    """Extract text from one uploaded file. Returns `(text, source)`.

    `source` is "pdf" when pdfplumber read embedded text, or "ocr" when
    the text came from a screenshot or a rasterised scan — the caller
    turns that into `needs_review`, because OCR is the only path where
    the operator should check the result before spending a request.

    Raises ValueError with an operator-facing message for anything that
    cannot be read, and OCRUnavailable on a platform with no OS engine.
    """
    ext = _safe_ext(upload.filename)
    if not ext:
        raise UploadError(
            f"{_display_ext(upload.filename) or 'That file'} isn't supported. "
            "Upload a PDF, or a PNG or JPG screenshot of the runsheet.")

    # Keep the real extension: ocrmac opens by path, and a .pdf suffix on
    # a PNG is a trap for whoever debugs this next.
    tmp_path = UPLOAD_FOLDER / f"runsheet_{int(time.time() * 1000)}{ext}"
    upload.save(str(tmp_path))
    try:
        if ext in PDF_EXTS:
            # extract_pdf_text is passed explicitly rather than left to
            # default, so tests (and the parse_client fixture) can swap
            # the module-level name and still be honoured here.
            text, pages = pdf_text_or_images(
                str(tmp_path), extract=extract_pdf_text,
                render=render_pdf_pages)
            if (text or "").strip():
                return text, "pdf"
            if not pages:
                raise UploadError(
                    "Couldn't read any text from that PDF. If it's a scan, "
                    "try a clearer copy or upload a screenshot instead.")
            return images_to_text(pages), "ocr"
        return image_to_text(str(tmp_path)), "ocr"
    finally:
        tmp_path.unlink(missing_ok=True)


def _extracted_or_error(upload):
    """`_upload_to_text` with every failure mapped to a plain message.

    Returns `(text, source, error)`. An engine crash must never reach the
    operator as a stack trace — "Vision framework exploded" is not
    actionable at 9am on a Sunday.
    """
    try:
        text, source = _upload_to_text(upload)
    except (OCRUnavailable, UploadError) as e:
        # Both carry messages we wrote for this exact moment.
        return "", "", str(e)
    except Exception:
        # Everything else — including a bare ValueError from ocrmac or
        # Pillow — is logged in full and replaced. Library text is not
        # actionable at 9am on a Sunday, and echoing it back leaks
        # internals to whoever can reach the port.
        log.exception("extraction failed for %s", log_safe(upload.filename))
        return "", "", ("Something went wrong reading that file. Try a PDF, "
                        "or a PNG screenshot of the runsheet.")
    if not (text or "").strip():
        return "", "", ("Couldn't read any text from that file. Try a bigger "
                        "or clearer screenshot.")
    return text, source, ""


@bp.route("/api/extract_text", methods=["POST"])
def api_extract_text():
    """Turn an upload into text, without spending an OpenRouter request.

    Split out of /api/upload_and_parse so the operator can SEE what was
    read off a screenshot and fix a misread before parsing. A free
    OpenRouter account gets 50 requests a day; burning one on a garbled
    OCR result is the failure this prevents.
    """
    upload = request.files.get("file") or request.files.get("pdf")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file uploaded."}), 400

    text, source, error = _extracted_or_error(upload)
    if error:
        return jsonify({"error": error}), 400

    log.info(f"Extracted {len(text)} chars from "
             f"{log_safe(upload.filename)} via {source}")
    return jsonify({
        "text":         text,
        "source":       source,
        # Only OCR output is worth a human's eyes. A text PDF is exact,
        # so showing a review panel for it would add a click to the
        # path every Sunday runsheet takes.
        "needs_review": source == "ocr",
        "filename":     upload.filename,
    })


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


def _provider_failure(resp):
    """Spot OpenRouter relaying an *upstream provider's* failure.

    OpenRouter fronts other companies' inference. When the provider it
    dispatched to fails, OpenRouter echoes the provider's status code with
    the provider named in the body:

        {"error": {"code": 401, "message": "Provider returned error",
                   "metadata": {"provider_name": "Darkbloom", ...}}}

    Confirmed live 2026-08-03: a Darkbloom credentials outage surfaced
    exactly like that — as a 401 — while the operator's own key verified
    fine at the same moment, and the handler below sent them off to rotate
    it. `provider_name` is the discriminator: a genuine key rejection never
    carries one, because the request dies at OpenRouter's own door before
    any provider is involved.

    Returns {"provider": ..., "code": ...} for provider-side failures, None
    for everything else (2xx, real key/credit/model errors, bodies that
    aren't even JSON).
    """
    if resp.status_code < 400:
        return None
    try:
        err = resp.json().get("error") or {}
        provider = (err.get("metadata") or {}).get("provider_name")
    except Exception:
        return None
    if not provider:
        return None
    return {"provider": provider, "code": err.get("code") or resp.status_code}


def _provider_failure_message(model: str, failure: dict,
                              backup: str = None,
                              backup_failure: dict = None) -> str:
    """Tell the operator the truth: the model's provider broke, not their key.

    Sending someone to rotate a working key is the worst kind of wrong — the
    "fix" changes nothing, so they conclude the app itself is broken. Name
    whose fault it is, and when the automatic backup failed too, name that
    as well so "pick a different model" doesn't send them straight to the
    one we already tried.
    """
    msg = (f"The service behind '{model}' is having problems right now "
           f"(provider {failure['provider']} returned {failure['code']}). ")
    if backup and backup_failure:
        msg += (f"A backup model '{backup}' failed too (provider "
                f"{backup_failure['provider']} returned "
                f"{backup_failure['code']}). ")
    msg += ("Your API key is fine — try again in a minute, or pick a "
            "different model in Settings.")
    return msg


def _rate_limit_message(resp) -> str:
    """Turn OpenRouter's 429 into instructions a volunteer can act on.

    Measured live 2026-08-03: the free tier allows 50 free-model requests
    per DAY per ACCOUNT (metadata.limit_source
    "openrouter_free_tier_daily"). Because it is account-wide, swapping to
    a different API key on the same account — the operator's natural first
    move — changes nothing, and neither does picking a different free
    model. The raw "429 Client Error" invited exactly that wasted effort.
    """
    import datetime as dt
    limit_source, reset_ms = "", None
    try:
        meta = (resp.json().get("error") or {}).get("metadata") or {}
        limit_source = meta.get("limit_source") or ""
        reset_ms = int((meta.get("headers") or {}).get("X-RateLimit-Reset"))
    except Exception:
        pass
    if "daily" in limit_source:
        when = "tomorrow"
        if reset_ms:
            try:
                when = dt.datetime.fromtimestamp(reset_ms / 1000).strftime(
                    "%-I:%M %p tomorrow" if dt.datetime.fromtimestamp(
                        reset_ms / 1000).date() != dt.date.today()
                    else "%-I:%M %p today")
            except Exception:
                pass
        return ("You've used all 50 free AI requests for today — OpenRouter's "
                "free tier daily limit, shared across every "
                "API key on your account, so a different key or model "
                f"won't help. The counter resets at {when}.")
    if "min" in limit_source:
        return ("OpenRouter is rate-limiting free models right now — "
                "wait a minute, then click Parse again.")
    return ("OpenRouter is receiving too many requests at the moment "
            "(rate limited). Wait a little and try again.")


@bp.route("/api/upload_and_parse", methods=["POST"])
def api_upload_and_parse():
    import requests as req

    # 1. Validate request. Two ways in: a file, or text the operator has
    #    already reviewed and corrected in the OCR panel. Reviewed text
    #    wins when both arrive — it IS the corrected version of the file.
    reviewed_text = (request.form.get("runsheet_text") or "")
    upload = request.files.get("pdf") or request.files.get("file")
    upload_name = (request.form.get("filename") or "").strip()
    if upload is not None and upload.filename and not upload_name:
        upload_name = upload.filename

    if "runsheet_text" in request.form and not reviewed_text.strip():
        # The operator cleared the textarea. Parsing an empty runsheet
        # would spend one of a free account's 50 daily requests to be
        # told there is nothing in it.
        return jsonify({"error":
            "There's no runsheet text to parse. Paste or re-upload the "
            "runsheet and try again."}), 400

    if not reviewed_text.strip() and (upload is None or not upload.filename):
        return jsonify({"error": "No runsheet uploaded"}), 400

    # Whether to link items to ProPresenter at all. Off means headers
    # only — see matching_enabled().
    do_matching = matching_enabled(request.form)

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
        return jsonify({"error": "OpenRouter API key required."}), 400

    if not model:
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
        # 4. Get the runsheet text. Either the operator already reviewed
        # it (screenshot / scan, corrected in the panel) or we extract it
        # from the upload now — which for a text PDF is the same
        # pdfplumber call this route has always made.
        if reviewed_text.strip():
            raw = reviewed_text
        else:
            raw, _source, error = _extracted_or_error(upload)
            if error:
                return jsonify({"error": error}), 400

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
        #
        # Skipped entirely when the operator turned "Populate with media
        # from PP" off: a brand-new event has no template and no reusable
        # media, so this whole block is round-trips for nothing. Skipping
        # also means parse works with ProPresenter closed, and on a
        # 1,261-item library that is a real speed difference.
        sections: list = []
        objects: list = []
        pp_host = (settings.get("pp_host") or "localhost").strip()
        pp_port = (settings.get("pp_port") or "50001").strip()
        base = f"http://{pp_host}:{pp_port}"
        tmpl_uuid = (settings.get("template_playlist_uuid") or "").strip()
        if not do_matching:
            tmpl_uuid = ""
        elif not tmpl_uuid:
            # Auto-pick the template based on runsheet content. The hint
            # combines filename + the start of the extracted text — both
            # usually say "youth" / "sunday" / "wednesday" / etc., which
            # lets the picker route a youth runsheet to "Youth Service -
            # Library" and a sunday runsheet to "Sunday Morning Library"
            # automatically. Fall back to the first template-named
            # playlist on tie or no signal.
            detect_hint = " ".join(filter(None, [
                upload_name, raw[:500]]))
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
        # and surfaces as a generic 500. But first: any error status can be
        # OpenRouter relaying its *provider's* failure (see _provider_failure)
        # — that is not the operator's key/credit/model-id problem, so it gets
        # one retry on the next-ranked free model and an honest message,
        # before the per-status mapping below gets a chance to misdiagnose it.
        def _openrouter_post(model_id):
            log.info(f"OpenRouter request: model={log_safe(model_id)}, "
                     f"raw_chars={len(raw)}")
            return req.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization":  f"Bearer {or_key}",
                    "HTTP-Referer":   "runsheet-pilot",
                    "X-Title":        APP_NAME,
                    "Content-Type":   "application/json",
                },
                json={
                    "model":       model_id,
                    "messages":    [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                },
                timeout=90,
            )

        resp = _openrouter_post(model)
        failure = _provider_failure(resp)
        if failure:
            backup = next_usable_model(model, fetch_catalogue())
            if not backup:
                return jsonify({"error":
                    _provider_failure_message(model, failure)}), 200
            log.warning(f"Provider behind {log_safe(model)} failed "
                        f"({log_safe(failure['provider'])} returned "
                        f"{failure['code']}) — retrying with {log_safe(backup)}")
            resp = _openrouter_post(backup)
            backup_failure = _provider_failure(resp)
            if backup_failure:
                return jsonify({"error": _provider_failure_message(
                    model, failure, backup, backup_failure)}), 200
            # The backup answered; from here on it is the model of record —
            # any later error message must name the model that actually
            # produced the response.
            model = used_model = backup

        if resp.status_code == 429:
            return jsonify({"error": _rate_limit_message(resp)}), 200
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

        # 7b. The timed-row guard. On the 14 Aug 2026 runsheet the model
        # dropped the three pre-service rows (their notes held a
        # volunteer roster, which looks like the credits block the prompt
        # says to skip) — with the prompt ALREADY forbidding exactly
        # that, so firmer wording is not a fix. Every timed row in the
        # raw text must come back; any the model lost is synthesized and
        # slotted in by time. Runs BEFORE the matching loop below so a
        # rescued "Youth Arrival + Hangout" still picks up its template
        # link like any parsed item.
        items, rescued_rows = rescue_missing_rows(items, raw)
        if rescued_rows:
            log.warning(f"Model dropped {rescued_rows} timed row(s); "
                        f"restored from raw text. model={log_safe(used_model)}")

        # 8. If the AI didn't supply a service name, derive one from the filename
        if not service_name and upload_name:
            stem = re.sub(r"\.(pdf|png|jpe?g)$", "", upload_name,
                          flags=re.IGNORECASE)
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
            obj = (resolve_with_aliases(it.get("title", ""), objects,
                                        settings.get("template_aliases"))
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
                "service_name":       service_name or upload_name or "Runsheet",
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
            "rescued_rows":   rescued_rows,
            "filename":       upload_name,
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

    # "Populate with media from PP" is off — nothing here applies. The
    # front end already skips this call, so this is the belt to its
    # braces: a stale client must not resurrect matching the operator
    # turned off. Re-match is exempt below because pressing Re-match IS
    # an explicit request to match now.
    if not matching_enabled(body) and not body.get("rematch_template"):
        return jsonify({"matches": [{"index": i, "match": None,
                                     "confidence": 0.0}
                                    for i, _it in enumerate(parsed)]})

    # Re-match: recompute template links against ProPresenter as it is
    # RIGHT NOW, without re-parsing. The operator renamed a slide and
    # wants the links refreshed; the runsheet text hasn't changed, and a
    # free-tier account only gets 50 AI parses a day, so spending one to
    # pick up a rename in PP would be the wrong trade.
    if body.get("rematch_template"):
        settings = load_settings()
        base = pp_base(body.get("host") or settings.get("pp_host"),
                       body.get("port") or settings.get("pp_port"))
        tmpl = (body.get("template_playlist_uuid")
                or settings.get("template_playlist_uuid") or "").strip()
        if not tmpl:
            # "Auto" — pick the template the same way parse does, from
            # the runsheet's own wording.
            hint = " ".join((it.get("title") or "") for it in parsed)
            try:
                tmpl = auto_detect_template_uuid(fetch_pp_playlists(base),
                                                 hint=hint) or ""
            except Exception:
                tmpl = ""
        n = link_items_to_template(parsed, base, tmpl,
                                   aliases=settings.get("template_aliases"),
                                   force=True)
        log.info("Re-match: %d/%d items linked to the template", n,
                 len(parsed))

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
