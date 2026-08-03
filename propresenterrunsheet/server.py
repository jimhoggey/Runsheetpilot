"""Server bootstrap — port discovery, native window, waitress, main().

Since v2.4.0 the UI opens in a NATIVE desktop window (pywebview — WebKit
on Mac, WebView2 on Windows) instead of the system browser, so the app
looks and quits like a real desktop app. The pywebview loop must own the
main thread — same constraint the old tkinter window had — so waitress
serves from a daemon thread whenever a window is shown. Everything below
about the tkinter status window still applies: it is the FALLBACK when
pywebview cannot start (e.g. Windows 10 without the WebView2 runtime),
paired with opening the UI in the system browser like pre-2.4 versions.

Mac UX note: a pure-Flask backend has no native UI, so the OS gives it no
Dock slot — the icon flickers once during PyInstaller's launcher hand-off
then disappears, even though the Python process is happily serving
requests. Users reasonably assume the app has crashed. We solve that by
spawning a small tkinter status window on the main thread; it gives macOS
something to register as an NSApplication (so the app appears in the
Dock, Cmd+Tab, and Force Quit normally) and gives the user an obvious
Quit button.

Consequence in the bundle / on Windows: waitress no longer owns the
main thread — it runs in a daemon thread, the Service Mate clock daemon
likewise. When the user closes the window (or clicks Quit) the main
thread returns; with only daemon threads left, the Python process tears
down naturally — no explicit os._exit needed, in-flight Flask requests
are abandoned cleanly.

Running from source on Mac/Linux: we skip the window entirely and serve
on the main thread (the original behaviour). Devs have a terminal as
their visibility signal, and skipping Tk avoids the "Python quit
unexpectedly" CrashReporter dialog on Macs where /usr/bin/python3 ships
a Tk that aborts at init (the system Python 3.9 on macOS 26+ does this).
See `_should_show_status_window()` for the routing rule.
"""

import logging
import socket
import sys
import threading
import time
import webbrowser

from .config import APP_NAME, DATA_DIR, DEFAULT_PORT, LOG_FILE, PORT_RANGE
from .config import SETTINGS_FILE, UPLOAD_FOLDER, VERSION
from .service_mate.daemon import start_clocks_loop
from .updater import cleanup_leftovers, start_background_check


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


def _probe_webview() -> int:
    """Import pywebview AND its platform backend, print the verdict, and
    return an exit code. `--probe-webview` runs this instead of the app.

    Exists for CI: the classic pywebview packaging failure on Windows is a
    bundle that builds clean but dies at runtime ("Failed to resolve
    Python.Runtime" — the pythonnet/clr pieces didn't make it into the
    exe). Importing the platform backend triggers exactly those imports
    without needing a window or a display, so the release smoke test can
    catch it on the runner instead of on an operator's machine."""
    import os
    import traceback

    def _emit(text):
        # A --windowed Windows exe has no console: print() is discarded.
        # CI sets RUNSHEET_PILOT_PROBE_OUT to a file path and reads that.
        print(text)
        out = os.environ.get("RUNSHEET_PILOT_PROBE_OUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(text + "\n")

    try:
        import webview
        if sys.platform == "win32":
            import webview.platforms.edgechromium  # noqa: F401 — pulls clr
        elif sys.platform == "darwin":
            import webview.platforms.cocoa  # noqa: F401 — pulls pyobjc
        _emit(f"webview OK ({getattr(webview, '__version__', '?')})")
        return 0
    except Exception:
        _emit(traceback.format_exc())
        _emit("webview UNAVAILABLE")
        return 1


def _run_native_window(port: int, webview_module=None) -> bool:
    """Open the UI in a native desktop window (pywebview). Blocks until
    the user closes the window. Returns False — without raising — when no
    usable webview backend exists, so main() can fall back to the old
    tkinter-status-window + system-browser combination.

    Must own the MAIN thread, same as the Tk window it replaces: Cocoa
    (Mac) and the Win32 message pump both refuse to run a UI loop from a
    background thread. That's why waitress is already in a daemon thread
    on this path.

    The URL uses 127.0.0.1, not localhost — managed Windows machines
    route "localhost" through the system proxy (the CI smoke test hung on
    exactly that until it switched).

    `webview_module` is injectable for tests; None imports the real one.
    """
    try:
        webview = webview_module
        if webview is None:
            import webview
        webview.create_window(
            APP_NAME, f"http://127.0.0.1:{port}",
            width=1280, height=860, min_size=(1000, 640))
        log.info("Native window open — close it to quit")
        webview.start()
        log.info("Native window closed — shutting down")
        return True
    except Exception as e:
        log.warning(f"Native window unavailable ({type(e).__name__}: {e}) "
                    "— falling back to status window + browser")
        return False


def _should_show_status_window() -> bool:
    """Return True if we should show a window (native, or the fallback
    status window) on the main thread.

    Rules (kept simple deliberately — see history if you're tempted to add
    a runtime probe):

      - **`--headless` in argv**: no. Explicit override for CI smoke
        tests and other automated launches where there's no interactive
        desktop. Real users never pass this; double-clicking the .app or
        .exe shows the window normally.

      - **PyInstaller bundle**: yes. The bundle ships python.org's Python
        with a Tk built against the GitHub Actions macos-latest SDK; that
        Tk works for any user on the same or newer macOS than the build
        runner. Worst case (user on an older macOS than the build target)
        is a hard crash, which is rare enough to accept.

      - **From source on Windows**: yes. Windows Tk is reliable.

      - **From source on Mac/Linux**: no. The status window is for
        end-users on the .app; devs running `python3 propresenter_app.py`
        have a terminal as their visibility signal. Skipping Tk entirely
        here avoids the "Python quit unexpectedly" CrashReporter dialog
        on Macs whose `/usr/bin/python3` has a Tk that aborts at init
        (system Python 3.9 on macOS 26+ is the common case).

    An earlier version of this code ran a subprocess probe to detect
    broken-Tk environments. Two problems with that: (1) the probe
    subprocess STILL crashes, so the user still gets the CrashReporter
    dialog every launch; (2) it added complexity for the only audience
    (devs running from source) that doesn't need the window in the first
    place. Dropped in favour of this static rule.
    """
    if "--headless" in sys.argv:
        return False
    if "--window" in sys.argv:
        # Developer override: see the native window from source without
        # building a bundle. --headless above still wins (CI safety).
        return True
    if getattr(sys, "frozen", False):
        return True
    return sys.platform == "win32"


def _serve(app, port: int) -> None:
    try:
        from waitress import serve
        log.info(f"Serving with waitress on http://127.0.0.1:{port}")
        serve(app, host="127.0.0.1", port=port, threads=8, _quiet=True)
    except ImportError:
        log.warning("waitress not installed — using Flask dev server "
                    "(install waitress for production use)")
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def _run_status_window(port: int) -> None:
    """Show a small always-visible status window that keeps the app
    registered with the OS as a normal desktop application.

    Why this exists — see the Mac UX note at the top of this module: a
    headless Flask app vanishes from the Dock because nothing in the
    process ever creates an NSApplication. Tkinter does, so a single
    tkinter window is enough to make the app "visible" in every place
    macOS users look — Dock, Cmd+Tab, Force Quit — and gives them an
    obvious Quit button.

    Must run on the **main thread**. Tkinter on macOS hard-crashes if
    instantiated from a background thread; this is a Tk-on-Cocoa
    restriction, not a Python one. Hence the architecture flip in
    `main()` — waitress and the clock daemon go to daemon threads so
    this can own the main thread.

    Falls back to a blocking sleep loop if tkinter can't initialise.
    Three known failure modes on Mac:
      - ImportError: Python built without `--enable-frameworks` and
        Tk wasn't compiled in (rare in shipped binaries).
      - TclError on Tk() construction: macOS's `/usr/bin/python3` ships
        a Tcl/Tk built against a newer macOS SDK than the running OS,
        so the constructor blows up at runtime. The PyInstaller bundle
        uses python.org's Python which is immune.
      - Any other tkinter error: catch-all so the server stays up even
        in environments we haven't seen.
    In every case the server keeps running; the user just doesn't get a
    visible window. Degraded but not broken — they can still hit
    localhost:5757 from a browser and quit via the in-UI Quit button.
    """
    url = f"http://localhost:{port}"

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:
        log.warning(f"tkinter unavailable ({type(e).__name__}: {e}) — "
                    f"running headless on {url}. Quit via /api/quit, "
                    "Ctrl+C, or by killing the process.")
        while True:
            time.sleep(3600)

    root.title(APP_NAME)
    root.geometry("420x220")
    root.resizable(False, False)
    # Subtle background that reads as macOS-native without forcing dark/
    # light mode-specific colours.
    root.configure(bg="#f5f5f7")

    # Bring the window to the front on launch — tkinter on Mac otherwise
    # opens it behind whichever app currently has focus, defeating the
    # whole "make it obvious the app is running" point.
    if sys.platform == "darwin":
        root.lift()
        root.attributes("-topmost", True)
        # Release the topmost flag after a moment so the user can put
        # other windows in front normally; we only want the initial pop.
        root.after(700, lambda: root.attributes("-topmost", False))

    # ── Title ──────────────────────────────────────────────────────
    tk.Label(root, text=APP_NAME,
             font=("Helvetica", 15, "bold"),
             bg="#f5f5f7", fg="#1d1d1f").pack(pady=(22, 4))

    # ── Status line ────────────────────────────────────────────────
    status_frame = tk.Frame(root, bg="#f5f5f7")
    status_frame.pack(pady=(0, 6))
    tk.Label(status_frame, text="●", fg="#16a34a", bg="#f5f5f7",
             font=("Helvetica", 14)).pack(side="left")
    tk.Label(status_frame, text=" Running on ",
             bg="#f5f5f7", fg="#1d1d1f").pack(side="left")
    url_lbl = tk.Label(status_frame, text=url, fg="#2563eb",
                       bg="#f5f5f7", cursor="hand2",
                       font=("Helvetica", 11, "underline"))
    url_lbl.pack(side="left")
    url_lbl.bind("<Button-1>", lambda _e: webbrowser.open(url))

    # ── Buttons ────────────────────────────────────────────────────
    btn_frame = tk.Frame(root, bg="#f5f5f7")
    btn_frame.pack(pady=14)
    tk.Button(btn_frame, text="Open in browser", width=16,
              command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Quit", width=10,
              command=root.destroy).pack(side="left", padx=6)

    # ── Footer ─────────────────────────────────────────────────────
    tk.Label(root, text=f"Version {VERSION}",
             font=("Helvetica", 9), fg="#86868b", bg="#f5f5f7"
             ).pack(side="bottom", pady=(10, 14))

    # Close-button (red dot on Mac, X on Windows) behaves like Quit.
    root.protocol("WM_DELETE_WINDOW", root.destroy)

    log.info("Status window open — close it or click Quit to shut down")
    root.mainloop()
    log.info("Status window closed — shutting down server")


def main(app=None) -> None:
    """Boot the app: discover a free port, start the Service Mate daemon,
    open the browser, and run waitress in a background thread while the
    main thread hosts the tkinter status window.

    The status window is what keeps the app visible in the macOS Dock —
    without it, the headless Flask process gets no Dock slot and users
    can't tell whether the app is running. Closing the window (or
    clicking Quit) returns from `mainloop()`; with only daemon threads
    left (waitress + clocks), the process exits naturally.

    `app` should be passed in from propresenter_app.py's `if __name__ ==
    "__main__"` block. We accept None and fall back to importing it as a
    convenience for the legacy callsite, but the explicit-pass path is
    the safe one — `from propresenter_app import app` inside a frozen
    PyInstaller bundle has been observed to yield a freshly-loaded
    module instance whose `app` is missing the routes registered on the
    bootloader's __main__ copy. End result: every URL 404s, even the
    ones that obviously exist in the source.
    """
    if app is None:
        from propresenter_app import app  # legacy / dev fallback

    # CI diagnostic: verify the pywebview backend is actually inside the
    # bundle, then exit. Never starts the server. See _probe_webview.
    if "--probe-webview" in sys.argv:
        sys.exit(_probe_webview())

    try:
        # Belt-and-braces diagnostic: log the routes Flask actually sees
        # so a future "URLs 404 in the bundle but not from source"
        # regression is one log line away from being obvious. Cheap;
        # runs once at startup.
        rules = sorted(
            f"{r.rule:40} → {r.endpoint}" for r in app.url_map.iter_rules()
        )
        log.info("Flask URL map (%d rules):\n  %s", len(rules), "\n  ".join(rules))
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
        print("  Close the app window to quit.")
        print(banner)

        # Service Mate daemon — pushes 240×240 JPEGs to GeekMagic clocks on
        # the LAN. No-op if no clock IPs are configured. Internally spawns
        # its own daemon thread (idempotent).
        start_clocks_loop()

        # Self-update housekeeping (frozen bundles only — both are no-ops
        # from source): delete the previous version's .old leftover and the
        # updates staging dir, then check GitHub for a newer release in the
        # background. Never blocks startup; check failures are silent.
        if getattr(sys, "frozen", False):
            cleanup_leftovers()
        start_background_check()

        # Two-mode startup, gated by _should_show_status_window():
        #   PyInstaller bundle + Windows from-source (+ --window) →
        #     waitress in a daemon thread, the UI in a NATIVE window on
        #     the main thread. Closing the window quits the app. If
        #     pywebview can't start (no WebView2 runtime on an old
        #     Windows 10, broken backend), fall back to the pre-2.4 UX:
        #     tkinter status window + the UI in the system browser.
        #   From-source on Mac/Linux → waitress on main thread, no
        #     window, browser opens — devs keep the old workflow.
        if _should_show_status_window():
            threading.Thread(target=_serve, args=(app, port),
                             daemon=True, name="waitress").start()
            if not _run_native_window(port):     # blocks until closed
                threading.Thread(target=_open_browser, args=(port,),
                                 daemon=True).start()
                _run_status_window(port)         # blocks until closed
        else:
            threading.Thread(target=_open_browser, args=(port,),
                             daemon=True).start()
            log.info("Running from source on %s — no status window. "
                     "Server at http://localhost:%d. "
                     "Quit with Ctrl+C or POST /api/quit.",
                     sys.platform, port)
            _serve(app, port)  # blocks on waitress, original behaviour
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    except Exception as e:
        _show_startup_error(
            f"{APP_NAME} failed to start",
            f"{type(e).__name__}: {e}\n\nLog file:\n{LOG_FILE}"
        )
        sys.exit(1)
