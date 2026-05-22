"""Server bootstrap — port discovery, browser open, waitress, main().

Mac UX note: a pure-Flask backend has no native UI, so the OS gives it no
Dock slot — the icon flickers once during PyInstaller's launcher hand-off
then disappears, even though the Python process is happily serving
requests. Users reasonably assume the app has crashed. We solve that by
spawning a small tkinter status window on the main thread; it gives macOS
something to register as an NSApplication (so the app appears in the
Dock, Cmd+Tab, and Force Quit normally) and gives the user an obvious
Quit button.

Consequence: waitress no longer owns the main thread — it runs in a
daemon thread, and so does the Service Mate clock daemon. When the user
closes the window (or clicks Quit) the main thread returns; with only
daemon threads left, the Python process tears down naturally — no
explicit os._exit needed, in-flight Flask requests are abandoned cleanly.
"""

import logging
import socket
import subprocess
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


def _tk_works() -> bool:
    """Return True if tkinter.Tk() can be constructed without crashing.

    Why this exists: on some Mac Python builds (notably `/usr/bin/python3`
    on macOS versions older than the Python's build-target SDK), creating
    a Tk root calls C-level abort() and kills the entire process — there's
    no Python exception to catch. We probe in a subprocess so the abort
    happens there, not in our app.

    The subprocess approach adds ~100 ms to startup on Mac/Linux when
    running from source. In a PyInstaller bundle we skip the probe and
    trust Tk works — the bundle includes its own python.org-built Tk that
    targets the build runner's macOS, so by construction it should work
    for anyone running the same or newer macOS than the build runner.
    Worst case (user on an OS older than the build target): app would
    crash hard, but Dependabot keeps the build runner's macOS current.

    Windows Tk is reliable everywhere; skip the probe there too.
    """
    if sys.platform == "win32":
        return True
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — sys.executable is the bundled .app/.exe, not
        # a Python interpreter. Spawning it with -c would just re-run the
        # whole app and loop forever. Trust the bundle.
        return True
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import tkinter; r = tkinter.Tk(); r.destroy()"],
            capture_output=True, timeout=5,
        )
        ok = result.returncode == 0
        if not ok:
            log.info("Tk probe failed (rc=%s, stderr=%r) — "
                     "will skip status window.",
                     result.returncode, result.stderr[:200])
        return ok
    except Exception as e:
        log.info("Tk probe threw %s — will skip status window.", e)
        return False


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


def main() -> None:
    """Boot the app: discover a free port, start the Service Mate daemon,
    open the browser, and run waitress in a background thread while the
    main thread hosts the tkinter status window.

    The status window is what keeps the app visible in the macOS Dock —
    without it, the headless Flask process gets no Dock slot and users
    can't tell whether the app is running. Closing the window (or
    clicking Quit) returns from `mainloop()`; with only daemon threads
    left (waitress + clocks), the process exits naturally.
    """
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
        print("  Close the status window or click Quit to shut down.")
        print(banner)

        threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
        # Service Mate daemon — pushes 240×240 JPEGs to GeekMagic clocks on
        # the LAN. No-op if no clock IPs are configured. Internally spawns
        # its own daemon thread (idempotent).
        start_clocks_loop()

        # Two-mode startup: probe whether tkinter actually works on this
        # Python install. If it does, waitress runs in a daemon thread and
        # the status window owns the main thread (gives us a Dock icon).
        # If Tk would crash this process, fall back to the original
        # behaviour — waitress on the main thread, no window — so users on
        # broken-Tk environments still get a working app, just without the
        # nice visibility upgrade.
        if _tk_works():
            threading.Thread(target=_serve, args=(app, port),
                             daemon=True, name="waitress").start()
            _run_status_window(port)  # blocks until user closes window
        else:
            log.warning("tkinter unavailable — running headless. The app is "
                        "still serving at http://localhost:%s but won't show "
                        "in the Dock. Quit via /api/quit, Ctrl+C, or kill.",
                        port)
            _serve(app, port)  # blocks on waitress, original behaviour
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    except Exception as e:
        _show_startup_error(
            f"{APP_NAME} failed to start",
            f"{type(e).__name__}: {e}\n\nLog file:\n{LOG_FILE}"
        )
        sys.exit(1)
