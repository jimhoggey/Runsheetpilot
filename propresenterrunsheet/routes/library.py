"""ProPresenter library routes.

Two ways to load the song library:
  /api/library/scan   — read .pro files directly from the local PP folder
                        (no PP needed; works offline)
  /api/library/fetch  — call PP's REST API (requires PP running with
                        Network mode enabled)"""

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..propresenter.library import scan_library


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
        items = r2.json().get("items", [])
        log.info(f"Library fetch via API: {len(items)} items")
        return jsonify({"items": items, "count": len(items)})
    except req.exceptions.ConnectionError:
        return jsonify({"error":
            f"Cannot connect to ProPresenter at {host}:{port}. "
            "Make sure ProPresenter is running and Network is enabled."}), 200
    except Exception as e:
        log.exception("Library fetch failed")
        return jsonify({"error": str(e)}), 500
