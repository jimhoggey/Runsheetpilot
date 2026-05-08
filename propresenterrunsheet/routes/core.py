"""Core routes — index page, health check, quit.

These don't fit in any of the topic-specific blueprints — they're the
machinery that makes the app feel like an app. `/` returns the rendered
index template (HTML/CSS/JS lives in templates/ + static/). `/api/quit`
exits the process so the UI's Quit button works."""

import logging
import os
import sys
import threading
import time

from flask import Blueprint, jsonify, render_template

from ..config import VERSION


bp = Blueprint("core", __name__)
log = logging.getLogger("pp_runsheet")


@bp.route("/")
def index():
    # The HTML / CSS / JS for the UI lives in templates/index.html and
    # static/app.{css,js} so editors give us syntax highlighting and the
    # browser can cache the static assets. PyInstaller bundles these via
    # --add-data flags in build_mac.sh / build_win.bat.
    return render_template("index.html")


@bp.route("/api/health")
def api_health():
    return jsonify({"ok": True, "version": VERSION, "platform": sys.platform})


@bp.route("/api/quit", methods=["POST"])
def api_quit():
    log.info("Quit requested via UI")
    def _bye():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True})
