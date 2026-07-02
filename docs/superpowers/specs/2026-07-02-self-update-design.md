# Self-Update Mechanism — Design

**Date:** 2026-07-02
**Status:** Approved (design), pending implementation
**Owner decision highlights:** prompt-based one-click update (never silent), Mac + Windows both in scope, **Windows is the production deployment target** (dev machine is a Mac — Windows logic must be unit-proven and church-PC-verified).

## Problem

Runsheet Pilot ships as unsigned PyInstaller bundles (`.dmg` → `Runsheet Pilot.app` on Mac, standalone `Runsheet Pilot.exe` on Windows) published on GitHub Releases. Today, updating means: notice a release exists, download it, dismiss Gatekeeper/SmartScreen again, replace the old copy by hand. Church AV volunteers won't do that, so installs rot. The app must check GitHub for a newer version and update itself in one click — no uninstall/reinstall.

## Decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Update UX | **Prompt + one-click** ("Update & Restart" banner) | Never surprise-update minutes before a service; still zero manual work |
| Platforms | **Mac + Windows**, Windows is the real deployment | Church machine is Windows; dev machine is Mac |
| Check source | GitHub Releases API `/releases/latest`, anonymous | Repo is public; endpoint auto-excludes pre-releases |
| Check timing | On launch, background daemon thread, frozen-only | Never blocks startup; dev runs from source never self-update |
| Integrity | SHA-256 of each asset, verified against `SHA256SUMS.txt` from the same release | Protects against corrupt/truncated downloads; HTTPS+GitHub is the trust anchor (same as the original install) |
| Failed check | Silent (log only) | Offline booths are normal; never nag about a failed *check* |
| Failed update | Banner: "Update failed — try again or download manually" + link | Running app is never left broken |

## Architecture

Four pieces, mirroring existing patterns (licensing.py / routes/license.py / licence badge UI):

1. **`propresenterrunsheet/updater.py`** (new, ~250 lines, stdlib + `requests` only)
2. **`propresenterrunsheet/routes/update.py`** (new blueprint; registered in `routes/__init__.py`)
3. **Frontend banner + Settings row** (`templates/index.html`, `static/app.js`, `static/app.css`)
4. **`release.yml` asset changes** (stable names + checksums)

### updater.py

State machine exposed to the UI: `idle → available → downloading → verifying → ready → applying → error`.

- `check_for_update() -> dict | None` — GET `https://api.github.com/repos/jimhoggey/propresenter-runsheet-builder/releases/latest`, 5 s timeout. Parse tag (`v2.3.0` → `(2,3,0)`), tuple-compare against `VERSION`. Returns `{version, notes_url, asset_url, sha_url}` or `None`. Any exception → log + `None`.
- `_semver(s) -> tuple` — pure, unit-tested. Non-semver tags → treated as no update.
- `_platform_asset(assets) -> dict | None` — picks `Runsheet-Pilot-mac.zip` (darwin) / `Runsheet-Pilot-windows.exe` (win32) from the release's asset list by exact name. Missing asset → no update offered (old releases without stable assets are invisible to the updater — correct, since they predate it).
- `download_and_verify(info) -> Path` — stream to `DATA_DIR/updates/` with `.part` suffix, rename when complete, SHA-256 against the entry in `SHA256SUMS.txt` (downloaded from the same release). Mismatch → delete + raise.
- `apply_update(path)` — platform dispatch below.
- `install_location() -> tuple[Path, bool]` — resolve the real bundle root from `sys.executable`; second element = writability. Mac: walk up to the `.app` directory. Windows: the exe path itself.
- `cleanup_leftovers()` — on boot: delete `*.old` bundles/exes and the `updates/` dir. Called from `main()`.

### Apply — Windows (production path, most conservative)

You cannot overwrite a running `.exe` on Windows, but you **can rename it**:

1. `Runsheet Pilot.exe` (running, at `sys.executable`) → rename to `Runsheet Pilot.exe.old` (same dir).
2. Move verified new exe from `updates/` to the original path.
3. Spawn it: `subprocess.Popen([new_exe], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)`.
4. `os._exit(0)` (same hard-exit the Quit button uses).
5. New instance's `cleanup_leftovers()` deletes the `.old` (retry loop ~10×500 ms — the old process may take a moment to die; failure to delete is logged, retried next boot, never fatal).

Rollback: if step 2 or 3 fails, rename `.old` back and report the error banner. The old exe is never deleted until the new one is confirmed running (its own boot does the cleanup).

Notes: Mark-of-the-Web is added by browsers, not by Python sockets — the swapped exe does **not** re-trigger SmartScreen. Defender may still scan the new file on first run (one-time delay, acceptable).

### Apply — Mac

1. Unzip verified `Runsheet-Pilot-mac.zip` in `updates/` → sanity-check `Runsheet Pilot.app/Contents/MacOS/` exists.
2. Rename current `.app` → `Runsheet Pilot.app.old` (same parent, usually `/Applications`).
3. `shutil.move` new `.app` into place.
4. `subprocess.Popen(["open", str(new_app)])`, then `os._exit(0)`.
5. New instance cleans `.old` + `updates/`.

Python downloads don't set `com.apple.quarantine`, so no new Gatekeeper prompt.

**Guard rail (both platforms):** if `install_location()` is read-only (running off the mounted `.dmg`, a network share, Program Files without rights), the banner degrades to notify-only: "Install the app to Applications to enable one-click updates" (Mac wording) / "Move the app to a writable folder…" (Windows) + a link to the release page.

### routes/update.py

- `GET /api/update` → `{state, current, latest, notes_url, error}` — read-only, safe to poll.
- `POST /api/update/apply` → kicks a background thread through download → verify → apply; returns immediately. UI polls `GET /api/update` for progress (`downloading` → `applying`). No auth needed — localhost-only app, same trust level as `/api/quit`.
- Dev mode (`not sys.frozen`): `GET` returns `{state:"dev"}`; `POST` returns 409. The updater never runs from source.

### Frontend

- **Header banner** (below header, dismissible per-session): "⬆ v2.3.0 available — **Update & Restart** · Release notes". Click → POST apply → button becomes "Downloading… / Restarting…" driven by polling. On `error`, shows retry + manual-download link.
- **Settings row** (near the version line): current version + "Check for updates" button (manual re-check) + status text.
- Reuses the licence-badge visual family; ~80 lines of JS following `loadLicense()`/`activateLicense()` patterns.

### release.yml changes

In `build-mac`: after building the `.app`, `ditto -c -k --keepParent` it to `Runsheet-Pilot-mac.zip` (ditto preserves the executable bits + symlinks a plain zip can lose). In `build-windows`: copy the exe to `Runsheet-Pilot-windows.exe`. New final step in the `release` job: generate `SHA256SUMS.txt` over **whichever stable assets exist** (the Windows exe may be absent when its non-blocking build fails — the sums file lists only what shipped, and the updater treats a missing sum entry the same as a missing asset: no update offered on that platform) and attach it. The versioned `.dmg` remains for first-time installs; `fail_on_unmatched_files` stays false (Windows is still non-blocking).

## Error handling summary

Every step is fail-safe: the running install is untouched until a verified binary sits on disk and the rename dance starts; the dance itself is two renames with a rollback path. Check failures are silent; download/apply failures surface in the banner with a manual fallback link. Partial downloads use `.part` + atomic rename. `updates/` and `.old` are self-healing (cleaned every boot).

## Testing

- **Unit (~15 tests, all runnable on the Mac dev machine):** semver parse/compare edge cases (`v2.10.0 > v2.9.9`, junk tags), platform asset selection, checksum verify (good/bad/missing), GitHub-response parsing from canned fixtures, read-only-location detection, **Windows path logic with mocked `sys.platform`/`sys.executable`** (rename sequence built as a pure "plan" function returning the operations list, so the exact Windows order is asserted without running Windows).
- **Route tests:** `/api/update` in dev mode, mocked-frozen states, 409 on apply-in-dev.
- **Manual — church Windows PC (mandatory before announcing):** install current exe → publish next release → banner appears → one click → app restarts on new version → `.old` gone after boot. Added to `RELEASE_CHECKLIST.md` as a required step while the Windows CI smoke test remains parked.

## Rollout

Self-update only works **from** the first version carrying it. Merge → tag **v2.3.0** with the new assets → everyone on ≤2.2.0 does one final manual install → every later release is one click. `RELEASE_CHECKLIST.md` gains: stable assets present, SHA256SUMS attached, update-from-previous-version verified on Windows.

## Out of scope (YAGNI, revisit on demand)

- Ed25519-signing the checksums file (would require the licensing private key in CI — contradicts its never-leaves-the-owner's-machine rule).
- Delta updates, update channels (beta/stable), auto-download-then-prompt, "skip this version" memory.
