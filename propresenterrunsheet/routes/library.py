"""ProPresenter library routes.

Three ways to load the song library:
  /api/library/scan   — read .pro files directly from the local PP folder
                        (no PP needed; works offline)
  /api/library/fetch  — call PP's REST API (requires PP running with
                        Network mode enabled)
  /api/library/auto   — try API first, fall back to disk. Called silently
                        by the UI on launch + before each parse so the
                        operator never has to touch the library settings."""

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..propresenter.library import scan_library
from ..settings import load_settings


bp = Blueprint("library", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/api/library/scan", methods=["POST"])
def api_scan_library():
    body = request.get_json(silent=True) or {}
    d = (body.get("directory") or "").strip()
    if not d:
        return jsonify({"error": "Library folder not set."}), 400
    if not Path(d).exists():
        return jsonify({"error": f"Folder not found: {d}"}), 400
    items = scan_library(d)
    log.info(f"Library scan: {len(items)} items from {d}")
    return jsonify({"items": items, "count": len(items)})


@bp.route("/api/library/fetch", methods=["POST"])
def api_fetch_library():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    items = _fetch_library_via_api(host, port)
    if items is None:
        return jsonify({"error":
            f"Cannot connect to ProPresenter at {host}:{port}. "
            "Make sure ProPresenter is running and Network is enabled."}), 200
    log.info(f"Library fetch via API: {len(items)} items")
    return jsonify({"items": items, "count": len(items)})


def _fetch_library_via_api(host: str, port: str):
    """Shared by /api/library/fetch and /api/library/auto. Returns the
    library items list on success, None when PP is unreachable, or
    raises for unexpected errors. Picks the FIRST library returned by
    /v1/libraries — operators with multiple libraries can still use
    /api/library/scan to point at a specific disk path."""
    import requests as req
    base = f"http://{host}:{port}"
    try:
        r = req.get(f"{base}/v1/libraries", timeout=6)
        r.raise_for_status()
        libs = r.json()
        if isinstance(libs, dict):
            lib_id = next(iter(libs))
            v = libs[lib_id]
            lib_id = v.get("uuid") or v.get("name") or lib_id
        else:
            lib_id = libs[0].get("uuid") or libs[0].get("name")
        r2 = req.get(f"{base}/v1/library/{lib_id}", timeout=12)
        r2.raise_for_status()
        return r2.json().get("items", [])
    except req.exceptions.ConnectionError:
        return None
    except Exception:
        log.exception("Library API fetch failed")
        return None


@bp.route("/api/library/auto", methods=["GET"])
def api_library_auto():
    """Best-effort library load — used by the UI on launch + before parse
    so the operator doesn't have to manually scan / fetch.

    Order of attempts:
      1. PP REST API (live, accurate, requires PP running).
      2. Local disk scan of `library_dir` from settings.
      3. Empty list with source="none" so the UI can show "no library".

    Query params override settings:
      ?host=  / ?port=     PP REST host/port (defaults to settings)
      ?dir=                disk scan path (defaults to settings.library_dir)
      ?mode=auto|api|disk  which source(s) to try (defaults to
                           settings.lib_source, falling back to "auto").
                           - auto: try API, fall back to disk on failure
                           - api:  API only (skip disk fallback)
                           - disk: disk only (skip API)

    Response shape:
      {"items": [...], "source": "api"|"disk"|"none", "count": N}
    The `source` field lets the UI surface where the library came from
    (e.g. "423 songs · loaded from PP" vs "423 songs · loaded from disk")."""
    settings = load_settings()
    host = (request.args.get("host") or settings.get("pp_host")
            or "localhost").strip()
    port = (request.args.get("port") or settings.get("pp_port")
            or "50001").strip()
    disk_dir = (request.args.get("dir") or settings.get("library_dir")
                or "").strip()
    mode = (request.args.get("mode") or settings.get("lib_source")
            or "auto").strip().lower()
    if mode not in ("auto", "api", "disk"):
        mode = "auto"

    # 1. Try PP REST (unless mode locks to disk only).
    if mode in ("auto", "api"):
        items = _fetch_library_via_api(host, port)
        if items:
            log.info(f"Library auto: {len(items)} items from PP API "
                     f"({host}:{port}, mode={mode})")
            return jsonify({"items": items, "source": "api",
                            "count": len(items)})
        if mode == "api":
            # API-only mode and PP didn't answer — don't fall back to disk.
            log.info(f"Library auto: PP unreachable in api-only mode "
                     f"({host}:{port})")
            return jsonify({"items": [], "source": "none", "count": 0})

    # 2. Try disk scan (unless mode locks to api only — already returned above).
    if mode in ("auto", "disk") and disk_dir and Path(disk_dir).exists():
        try:
            items = scan_library(disk_dir)
            log.info(f"Library auto: {len(items)} items from disk "
                     f"({disk_dir}, mode={mode})")
            return jsonify({"items": items, "source": "disk",
                            "count": len(items)})
        except Exception:
            log.exception(f"Library auto: disk scan of {disk_dir} failed")

    # 3. Nothing worked — UI will show "no library, open Settings".
    log.info(f"Library auto: no source available (mode={mode})")
    return jsonify({"items": [], "source": "none", "count": 0})
