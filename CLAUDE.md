# ProPresenter Runsheet Builder

A local web app that reads a church service runsheet PDF, uses OpenRouter AI to parse it, fuzzy-matches songs against the ProPresenter library, and creates a playlist in ProPresenter 7 via its REST API.

End-user delivery is a native bundle (`.app`/`.dmg` on Mac, `.exe` on Windows) produced via PyInstaller — volunteers don't need Python installed.

## How to run (developer)

**Mac:** `./launch_mac.sh` (or `python3 propresenter_app.py`)
**Windows:** double-click `run.bat`

Opens at `http://localhost:5757` (or the next free port if taken). Quit via the **Quit** button in the UI, or `Ctrl+C` in the terminal.

## How to build for distribution

**Mac:** `./build_mac.sh` → `dist/ProPresenter Runsheet Builder.app` and `dist/ProPresenter-Runsheet-Builder-<ver>.dmg`
**Windows:** `build_win.bat` → `dist\ProPresenter Runsheet Builder.exe`

Both bundle Python + all deps. Bundles are unsigned, so first launch needs right-click → Open (Mac) or "Run anyway" via SmartScreen (Windows).

## Files

| File | Purpose |
|------|---------|
| `propresenter_app.py` | **Main app** — Flask server + inline HTML/CSS/JS UI |
| `launch_mac.sh` | Mac dev launcher — installs deps, runs the script |
| `run.bat` | Windows dev launcher — installs deps, runs the script |
| `build_mac.sh` | Builds `.app` + `.dmg` via PyInstaller |
| `build_win.bat` | Builds `.exe` via PyInstaller |
| `requirements.txt` | Runtime deps: `flask`, `pdfplumber`, `requests`, `waitress` |
| `requirements-dev.txt` | Adds `pyinstaller` for building |

## User-data layout

Settings and logs live **outside** the source tree so they survive across installs and work inside frozen bundles:

| Platform | Location |
|----------|----------|
| Mac | `~/Library/Application Support/ProPresenter Runsheet Builder/` |
| Windows | `%APPDATA%\ProPresenter Runsheet Builder\` |
| Linux | `$XDG_CONFIG_HOME/ProPresenter Runsheet Builder/` |

Files in there: `settings.json`, `app.log` (rotated, 512 KB × 3).

If a legacy `settings.json` exists next to the script (pre-refactor), it is auto-migrated on first run.

## Architecture

Single-file Flask app. No database. The HTML/CSS/JS UI is inlined into `propresenter_app.py` so PyInstaller has a single source file.

**Server:** Production runs on `waitress` (8 threads, 127.0.0.1 only). Falls back to Flask's dev server if waitress is missing. Port `5757` preferred; if taken, scans `5758–5776`.

**Backend routes:**
- `GET  /api/health` — version + platform check
- `GET  /api/settings` — saved settings + auto-detected PP root + platform + version
- `POST /api/settings` — patch-merges into `settings.json` (atomic write via `.tmp` + rename)
- `POST /api/library/scan` — scans `.pro` files, extracts UUIDs from binary header (first 64 KB)
- `POST /api/library/fetch` — fetches library via PP REST API
- `POST /api/upload_and_parse` — receives PDF (max 25 MB), extracts text with `pdfplumber`, sends to OpenRouter. Returns `{items, filename, suggested_name}`. The AI is prompted to return `{service_name, items}` so the playlist name field auto-populates after parse; falls back to a cleaned-up filename if the AI returns nothing. Bare-array responses from older models are still accepted. Uses saved `or_key`/`or_model` if not in form.
- `GET  /api/prompt` — returns the current effective AI prompt: `{prompt, is_default, default}`. The user-customised prompt is returned if set; otherwise the built-in `DEFAULT_PROMPT`.
- `POST /api/prompt` — saves a customised AI prompt to `settings.json` (`ai_prompt` key). Empty string reverts to the built-in default. The "📝 Show / edit AI prompt" sidebar button opens a modal that fetches and POSTs to these endpoints with debounced auto-save.
- `POST /api/match` — fuzzy-matches via `difflib.SequenceMatcher`
- `POST /api/create_playlist` — creates playlist via PP API, then copies the new `.playlist` file to export dir. When the request body has `create_timers: true`, also creates one count-down-to-time timer in PP for every matched item whose `notes` (fallback: `title`) contains a parseable time like `9:24 AM` (regex requires the AM/PM marker). Past times are skipped. Timers are named `{playlist_name} — {time} {section_title}`.
- `POST /api/test_connection` — pings PP API
- `POST /api/quit` — shuts the server down (Quit button in UI)

**Auto-save:** UI fields (API key, model, host/port, threshold, paths, timer toggle) auto-save 500 ms after the last keystroke. Save indicator in the sidebar shows `saving…` → `saved ✓`.

**Timers:** Sidebar toggle "Create countdown timers from runsheet times" (on by default, persisted as `create_timers` in settings). When on, `POST /api/create_playlist` runs `_create_pp_timers()` after the playlist is built.

Timers are **duration-based** (`countdown: {duration: <seconds>}`), NOT count-down-to-time. Reason: runsheets are uploaded days before the service, so the time-of-day in the runsheet is just the planned slot — the operator starts each timer when the section actually begins. The runsheet time-of-day is included in the timer NAME as a hint (e.g. `[RB] 12. 10:14 AM — King Jesus - Ps Nick (30 min)`).

Duration comes from the AI's `duration_min` field; fallback regex on `notes`/`title` matches `\d+\s*(min|minute|m)`. Items with no/zero duration are skipped (counted in `timers_no_duration`). `song` and `scripture` types are also skipped — songs are presentations (PP shows length naturally), scripture is brief/not a timed slot.

Timer names: `[RB] {NN}. {time_hint} — {title} ({n} min)`. The `NN` is the item's 1-based index in the original runsheet (gaps reflect skipped songs), so PP's alphabetical timer panel sort matches runsheet order. The `[RB]` prefix isolates them — `_delete_existing_rb_timers` only touches names starting with `[RB] `, so user-created timers (Segment Countdown, etc.) are never touched.

PP also supports count-down-to-time timers (`count_down_to_time: {time_of_day: <seconds>, period: "am"|"pm"}` where `time_of_day = (hour % 12) * 3600 + minute * 60`). We're not using that mode right now, but the encoding is documented here for future reference. Auto-trigger of timers when a section becomes active is **not** implemented (would need polling on `/v1/playlist/active`).

**Layout / scroll:** body is the scroll container (`min-height:100vh` on body, no `overflow-y:auto` on `main`). Both the header and the sidebar use `position:sticky` so they stay visible while the main column scrolls. Earlier attempts using `overflow-y:auto` on `main` plus `flex:1` on the results table caused the bottom rows to be unreachable — current layout avoids that entirely.

**AI prompt — customisable:** `DEFAULT_PROMPT` (in `propresenter_app.py`) defines the schema (a `{service_name, items}` object) and the type vocabulary. Important rules baked into the default: (1) skip the "rostering" credits block at the top of the runsheet; (2) start extracting from the first item with a time-of-day; (3) `mc_on_stage` is a separate type from `song` for items like "Land Worship — Lauren" so they don't trigger fuzzy-match misses; (4) be conservative about `song` (only actual sung-by-the-band songs). Users can edit the prompt in the modal — their version persists in `settings.json` under `ai_prompt`. The `{RUNSHEET}` placeholder is replaced with the extracted PDF text; if the user removes it, the runsheet is appended at the end.

**Per-type playlist colours:** `TYPE_COLORS` maps every item type to an RGBA tuple, applied to PP playlist header items so worship/preach/MC/announcement are visually distinct in PP. UI table tags in the builder use matching colours via `tagClass()` + `.tag-*` CSS so the volunteer sees the same colour in both places. ACTION NEEDED placeholders use a separate bright red. Scripture items get an extra `📖` prefix in their label.

**Why scripture is a manual-action marker, not real scripture content:** confirmed against the official ProPresenter OpenAPI spec at `https://openapi.propresenter.com/swagger.json` (last-modified 2025-06-25, 194 paths). There are **zero** scripture/bible/verse/passage endpoints — the entire `/v1/bible*`, `/v1/scripture*`, `/v1/verse*` namespace is unimplemented. Playlist item types accepted are still only `presentation, header, media, audio, placeholder` (no `scripture`). The earlier suspicious-looking 400 from `/v1/presentation/scripture` turned out to be the `/v1/presentation/{uuid}` route rejecting `"scripture"` as not-a-UUID — not a hidden endpoint. So the green 📖 header is the most we can offer; the operator opens PP's built-in Bible feature manually when they hit one.

**Reusable templates (planned, not built):** `settings.json` reserves a `templates: []` key for the future "use saved PP playlist as content for this section" feature. Approach decided: read items from a saved PP playlist via `GET /v1/playlist/{uuid}` and inline them into the new runsheet playlist (PP doesn't support `playlist` as an item type — confirmed by API probe). UI lands once the user has set up their master template playlists in PP.

**PDF upload:** browser `<input type="file">` + FormData POST. Temp PDFs live in `{tempdir}/pp_runsheet_uploads/` and are deleted right after parsing; uploads older than 24 h are swept on startup.

**Fuzzy matching:** `difflib.SequenceMatcher`, default threshold 0.55. Strings normalised (lowercased, punctuation stripped). Unmatched songs can be manually overridden via the Pick prompt.

**ProPresenter API:** runs locally on port 50001. Enable in ProPresenter → Preferences → Integrations → Network. Key endpoints: `POST /v1/playlists`, `PUT /v1/playlist/{id}`. Non-song items become `header` items.

**Playlist export:** after the API call, the app waits 1 s, finds the newest file in `ProPresenter/Playlists/` written after the request started, and copies it to the export dir.

**Frozen-bundle awareness:** `sys.frozen` is checked before any file write next to `__file__`. Settings/logs always go to the user data dir (above). Tkinter is bundled to surface a startup-error dialog if the server can't bind.

## Key dependencies

- `flask` — web server
- `waitress` — production WSGI server (cross-platform pure Python)
- `pdfplumber` — PDF text extraction
- `requests` — HTTP to OpenRouter and the PP API
- `difflib`, `tkinter`, `logging`, `socket` — stdlib

## OpenRouter

Endpoint: `https://openrouter.ai/api/v1/chat/completions` (OpenAI-compatible).
Default model: `google/gemini-2.0-flash-exp:free` (overridable in the sidebar — any model id from `openrouter.ai/models` works).
Headers: `Authorization: Bearer {key}`, `HTTP-Referer`, `X-Title`.
Specific HTTP statuses (401 / 402 / 404 / timeout) get plain-English errors.

## Known behaviour

- ProPresenter must be running with Network enabled for playlist creation. Connection refusals return a friendly JSON error, not a 500.
- PDF must be text-based (not a scanned image) for `pdfplumber`.
- The `.playlist` format is binary Protocol Buffers — the app does not generate it directly; it lets ProPresenter write it via API, then copies the file.
- The dev (`Mac`) machine has no ProPresenter installed, so `.playlist` output validation happens on a Windows church machine.
