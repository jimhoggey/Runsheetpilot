"""Server bootstrap — port discovery, browser open, waitress, main()."""

import logging
import socket
import sys
import threading
import time
import webbrowser

from .config import APP_NAME, DATA_DIR, DEFAULT_PORT, LOG_FILE, PORT_RANGE
from .config import SETTINGS_FILE, UPLOAD_FOLDER, VERSION
from .service_mate.daemon import start_clocks_loop


log = logging.getLogger("pp_runsheet")


def _find_free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + PORT_RANGE)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in {preferred}–{preferred + PORT_RANGE - 1}")


def _cleanup_old_uploads(max_age_hours: int = 24) -> None:
    cutoff = time.time() - max_age_hours * 3600
    try:
        for f in UPLOAD_FOLDER.glob("*.pdf"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _open_browser(port: int) -> None:
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        log.exception("Could not open browser")


def _show_startup_error(title: str, message: str) -> None:
    log.error(f"{title}: {message}")
    print(f"\n!!! {title}\n{message}\n", file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def _serve(app, port: int) -> None:
    try:
        from waitress import serve
        log.info(f"Serving with waitress on http://127.0.0.1:{port}")
        serve(app, host="127.0.0.1", port=port, threads=8, _quiet=True)
    except ImportError:
        log.warning("waitress not installed — using Flask dev server "
                    "(install waitress for production use)")
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def main() -> None:
    """Boot the app: discover a free port, kick off the Service Mate
    daemon, open the browser, and serve via waitress (or Flask's dev
    server as a fallback)."""
    # Lazy import to avoid an import cycle at package-load time:
    # propresenter_app imports server, server would import propresenter_app
    # for `app` if done at module level.
    from propresenter_app import app

    try:
        log.info(f"=== {APP_NAME} v{VERSION} ===")
        log.info(f"Platform: {sys.platform}  Frozen: {getattr(sys, 'frozen', False)}")
        log.info(f"Data dir: {DATA_DIR}")
        _cleanup_old_uploads()

        port = _find_free_port(DEFAULT_PORT)
        if port != DEFAULT_PORT:
            log.warning(f"Port {DEFAULT_PORT} taken — using {port} instead")

        platform_name = "Mac" if sys.platform == "darwin" else (
                        "Windows" if sys.platform == "win32" else sys.platform)
        banner = "=" * 56
        print(banner)
        print(f"  {APP_NAME} — v{VERSION} ({platform_name})")
        print(f"  http://localhost:{port}")
        print(f"  Logs:     {LOG_FILE}")
        print(f"  Settings: {SETTINGS_FILE}")
        print("  Press Ctrl+C to quit (or use the Quit button in the UI)")
        print(banner)

        threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
        # Service Mate daemon — pushes 240×240 JPEGs to GeekMagic clocks on
        # the LAN. No-op if no clock IPs are configured.
        start_clocks_loop()
        _serve(app, port)
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    except Exception as e:
        _show_startup_error(
            f"{APP_NAME} failed to start",
            f"{type(e).__name__}: {e}\n\nLog file:\n{LOG_FILE}"
        )
        sys.exit(1)
