"""Playlist creation + ProPresenter connection test routes.

/api/create_playlist creates the playlist in PP, pushes the items
(payload built by build_playlist_payload), optionally exports a
.playlist file to the user's chosen folder, optionally creates [RB]
countdown timers, and persists the Service Mate runsheet state.

/api/test_connection is a one-call ping to PP's /v1/libraries — used
by the sidebar's "Test connection" button."""

import datetime as _dt
import logging
import shutil
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..logging_setup import log_safe
from ..propresenter.media_bin import fetch_media_bin, relink_media
from ..propresenter.net import pp_base
from ..propresenter.paths import find_playlist_dir, find_pp_root
from ..propresenter.playlist import build_playlist_payload
from ..propresenter.templates import (
    auto_detect_template_uuid, fetch_pp_playlist_items, fetch_pp_playlists,
    playlist_to_sections,
)
from ..propresenter.timers import _create_pp_timers
from ..service_mate.state import _ensure_item_cues, _write_runsheet_state


bp = Blueprint("playlist", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/api/create_playlist", methods=["POST"])
def api_create_playlist():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = pp_base(host, port)
    name = (body.get("name") or "").strip()
    matched = body.get("matched") or []
    before = time.time()

    if not name:
        return jsonify({"error": "Playlist name required."}), 200
    if not matched:
        return jsonify({"error": "No items to add to the playlist."}), 200

    try:
        # 0. Resolve template media against PP's Media bin BEFORE anything
        # else. PP's playlist PUT matches media items by NAME against the
        # Media bin and ignores the uuid (established by live bisection —
        # its 404s carry an empty body, so nothing else would have told
        # us). Media that isn't in the bin cannot be linked over the API
        # at all; relink_media drops those entries (their runsheet items
        # keep their coloured headers) and reports them so the UI can give
        # the operator the one-time fix in plain words. Bin fetch failing
        # just skips this step — worst case is the old behaviour.
        # An empty bin result is indistinguishable from a transient PP
        # hiccup (fetch_media_bin returns [] on failure), so relinking is
        # skipped rather than applied — applying it against [] would drop
        # every linked slide on a blip. If PP then refuses the template
        # identities, the safe-mode retry below still saves the create.
        bin_items = fetch_media_bin(base)
        unlinked = relink_media(matched, bin_items) if bin_items else []
        if unlinked:
            log.info("Media not in PP's Media bin, left as headers: %s",
                     log_safe(", ".join(u["media_name"] for u in unlinked)))

        # 1. Create the playlist
        r = req.post(f"{base}/v1/playlists",
                     json={"name": name, "type": "playlist"}, timeout=6)
        r.raise_for_status()
        pid = r.json().get("id", {})
        if isinstance(pid, dict):
            playlist_id = pid.get("uuid") or pid.get("name") or name
        else:
            playlist_id = str(pid) or name

        # 2. Build items list — pure function in propresenter/playlist.py
        items = build_playlist_payload(matched)

        # 3. Push items to playlist
        r2 = req.put(f"{base}/v1/playlist/{playlist_id}",
                     json=items, timeout=10)
        if r2.status_code in (400, 404):
            # Shouldn't happen now that media is bin-resolved up front —
            # but if PP still refuses, recover instead of stranding the
            # operator: strip every linked slide (headers stay), push
            # again, and say plainly which slides were left out. The old
            # message here blamed "song UUIDs" and told them to re-scan
            # the library, which was wrong on both counts and
            # unactionable for a non-developer.
            log.error("PP refused playlist items (HTTP %s, body=%r) — "
                      "retrying without linked slides",
                      r2.status_code, log_safe(r2.text, 300))
            dropped = []
            for mi in matched:
                parsed = mi.get("parsed") or {}
                lib = parsed.get("library_match")
                if isinstance(lib, dict) and lib.get("items"):
                    for entry in lib["items"]:
                        dropped.append({
                            "item_title": parsed.get("title", ""),
                            "media_name": (entry.get("name") or "").strip(),
                        })
                    parsed["library_match"] = None
            items = build_playlist_payload(matched)
            r2 = req.put(f"{base}/v1/playlist/{playlist_id}",
                         json=items, timeout=10)
            if r2.status_code in (400, 404):
                log.error("PP refused even the headers-only playlist "
                          "(HTTP %s, body=%r)",
                          r2.status_code, log_safe(r2.text, 300))
                return jsonify({"error":
                    "ProPresenter wouldn't accept the playlist items. "
                    "Try restarting ProPresenter, then click Create "
                    "again — the app will rebuild everything fresh."}), 200
            unlinked = unlinked + dropped
        r2.raise_for_status()

        songs = sum(1 for mi in matched
                    if (mi.get("parsed") or {}).get("type") == "song"
                    and mi.get("match"))
        needs_action = sum(1 for mi in matched
                           if (mi.get("parsed") or {}).get("type") == "song"
                           and not mi.get("match"))
        headers = sum(1 for mi in matched
                      if (mi.get("parsed") or {}).get("type") != "song")

        # 4. Try to export the .playlist file
        export_path = None
        export_dir = (body.get("export_dir") or "").strip()
        if export_dir:
            pdir = find_playlist_dir(find_pp_root())
            if pdir:
                time.sleep(1.0)
                candidates = [f for f in Path(pdir).iterdir()
                              if f.is_file() and f.stat().st_mtime > before]
                if candidates:
                    newest = max(candidates, key=lambda f: f.stat().st_mtime)
                    Path(export_dir).mkdir(parents=True, exist_ok=True)
                    dest = Path(export_dir) / f"{name}.playlist"
                    shutil.copy2(newest, dest)
                    export_path = str(dest)

        # 5. Optional: create duration-based countdown timers
        timer_result = {"created": 0, "deleted": 0, "no_duration": 0,
                        "total_items": 0, "errors": [], "timer_names": {}}
        if body.get("create_timers"):
            timer_result = _create_pp_timers(base, name, matched)

        # 6. Persist Service Mate runsheet state — what the GeekMagic clocks
        # display on the LAN. We strip the "match" wrappers and keep only the
        # parsed items, plus stamp each item with the exact PP timer name we
        # created for it (so auto-track can match by name later).
        try:
            timer_names = (timer_result or {}).get("timer_names") or {}
            sm_items = []
            for i, mi in enumerate(matched):
                p = dict((mi.get("parsed") or {}))
                if i in timer_names:
                    p["pp_timer_name"] = timer_names[i]
                _ensure_item_cues(p)
                sm_items.append(p)
            sm_state = {
                "service_name":       name,
                "items":              sm_items,
                "current_index":      0,
                "current_started_at": _dt.datetime.now().isoformat(),
                "auto_track":         {"enabled": True},
            }
            _write_runsheet_state(sm_state)
            log.info(f"Service Mate state written: {len(sm_items)} items")
        except Exception:
            log.exception("Service Mate state write failed (non-fatal)")

        log.info(f"Playlist created: '{name}' → {songs} songs, {headers} headers, "
                 f"{needs_action} action-needed, {timer_result['created']} timers "
                 f"(deleted {timer_result['deleted']} old, "
                 f"{timer_result['no_duration']} skipped no-duration), "
                 f"export={export_path}")

        return jsonify({
            "ok":                  True,
            "songs":               songs,
            "headers":             headers,
            "needs_action":        needs_action,
            # Template slides that couldn't be attached because their
            # media isn't in PP's Media bin — the UI turns this into a
            # plain-English "drag these into Media, then Create again".
            "unlinked":            unlinked,
            "timers_created":      timer_result["created"],
            "timers_deleted":      timer_result["deleted"],
            "timers_no_duration":  timer_result["no_duration"],
            "timers_total_items":  timer_result["total_items"],
            "timer_errors":        timer_result["errors"],
            "export_path":         export_path,
        })

    except req.exceptions.ConnectionError:
        return jsonify({"error":
            f"Cannot connect to ProPresenter at {host}:{port}. "
            "Make sure ProPresenter is running and Network is enabled in "
            "Preferences → Integrations → Network."}), 200
    except Exception as e:
        log.exception("Playlist create failed")
        return jsonify({"error": str(e)}), 200


@bp.route("/api/test_connection", methods=["POST"])
def api_test_connection():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = pp_base(host, port)
    try:
        r = req.get(f"{base}/v1/libraries", timeout=4)
        r.raise_for_status()
        libs = r.json()
        return jsonify({"ok": True,
                        "count": len(libs) if hasattr(libs, "__len__") else 0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/pp/playlists", methods=["GET"])
def api_pp_playlists():
    """List the operator's PP playlists, plus a peek at each as a "template"
    (how many sections + items it would contribute if chosen). Used by the
    sidebar dropdown so the operator can pick the right "<Service> - Library"
    playlist. Pulls host/port from the query string (the UI already knows
    them) and falls back to the standard PP defaults."""
    host = (request.args.get("host") or "localhost").strip()
    port = (request.args.get("port") or "50001").strip()
    base = pp_base(host, port)
    playlists = fetch_pp_playlists(base)
    if not playlists:
        # Common failure: PP not running, Network off, or wrong port.
        # We return ok=True with empty list so the UI can show "0
        # playlists — is PP running?" instead of an error banner.
        return jsonify({"ok": True, "playlists": [], "auto_detected": ""})
    # For every playlist, count how many sections it would give us if
    # used as a template. Operators glance at this to spot their actual
    # template playlist vs. a one-shot service playlist.
    enriched = []
    for p in playlists:
        try:
            sections = playlist_to_sections(
                fetch_pp_playlist_items(base, p["uuid"]))
        except Exception:
            log.exception(f"sections peek failed for {p.get('name')!r}")
            sections = []
        enriched.append({
            **p,
            "section_count": len(sections),
            "media_count":   sum(len(s.get("items", [])) for s in sections),
        })
    auto = auto_detect_template_uuid(playlists) or ""
    return jsonify({"ok": True, "playlists": enriched, "auto_detected": auto})
