# ProPresenter Runsheet Builder

A local web app that turns a church service runsheet PDF into a ProPresenter 7 playlist. It reads the PDF, uses an OpenRouter AI model to parse it into structured items, fuzzy-matches songs against your local ProPresenter library, and creates the playlist via ProPresenter's REST API. Optionally creates countdown timers for each section.

Runs on macOS and Windows. Opens at `http://localhost:5757` in your browser.

---

## Before you start

You need three things on the machine that will run the app:

### 1. Python 3

- **Mac:** `brew install python` or download from [python.org](https://python.org).
- **Windows:** download from [python.org](https://python.org). During install, **tick "Add Python to PATH"** — the launcher needs `python` on PATH.

### 2. ProPresenter 7 running, with Network enabled

The app talks to ProPresenter on `localhost:50001`, so **ProPresenter must be running on the same machine** while you create playlists.

To enable the API:

1. Open ProPresenter → **Preferences** → **Integrations** → **Network**
2. Switch **Network** to **ON**

If Network is off, playlist creation will fail with a connection-refused error.

### 3. An OpenRouter API key

The app uses OpenRouter to parse the runsheet PDF.

1. Sign up at [openrouter.ai](https://openrouter.ai) and create an API key.
2. **Recommended free model: GPT-OSS.** It's free to use and parses runsheets reliably. Find it at [openrouter.ai/models](https://openrouter.ai/models) (search "gpt-oss") and copy the model ID — for example `openai/gpt-oss-20b:free` — into the app's *Model* field.
3. Paste your key into the *API Key* field in the app's sidebar. It auto-saves.

Your key is stored locally in your OS app-data folder (see [Where settings live](#where-settings-and-logs-live)) — it never leaves your machine except in calls to OpenRouter, and it is never committed to git.

---

## Run on Mac

From a Terminal in the project folder:

```bash
./launch_mac.sh
```

Or directly: `python3 propresenter_app.py`.

The launcher installs/updates Python dependencies on each run, then starts the server. Your browser should open automatically at `http://localhost:5757`.

Quit via the **Quit** button in the UI, or `Ctrl+C` in the terminal.

## Run on Windows

Double-click `run.bat`. It checks Python is installed, installs/updates dependencies, and starts the server. Your browser should open automatically.

Quit via the **Quit** button in the UI, or close the terminal window.

---

## Build a one-click app (optional)

If you'd rather not install Python on every machine, you can produce a native bundle. The user just double-clicks an icon — no Python required.

- **Mac:** `./build_mac.sh` → `dist/ProPresenter Runsheet Builder.app` and a `.dmg` installer.
- **Windows:** `build_win.bat` → `dist\ProPresenter Runsheet Builder.exe`.

Bundles are unsigned, so the first launch shows a warning:

- **Mac:** right-click the app → **Open** (one-time Gatekeeper prompt).
- **Windows:** click **More info** → **Run anyway** on the SmartScreen prompt.

The Windows `.exe` must be built on a Windows machine — PyInstaller doesn't cross-compile.

---

## Typical workflow

1. Make sure ProPresenter is running with Network enabled.
2. Start the app.
3. In the sidebar: paste your OpenRouter API key, set the model (e.g. `openai/gpt-oss-20b:free`).
4. Click **Scan local library** or **Fetch from ProPresenter** to load your songs.
5. Upload the runsheet PDF — the AI parses it; songs are fuzzy-matched against your library.
6. Review matches; override any wrong ones via the Pick prompt.
7. Click **Create playlist** — it appears in ProPresenter, and the `.playlist` file is copied to your export folder.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Connection refused" creating playlist | ProPresenter isn't running, or **Network** is OFF in Preferences → Integrations → Network. |
| AI returns nothing useful | Try a different model, or edit the AI prompt via the sidebar's **Show / edit AI prompt** button. |
| PDF text is empty | The runsheet is a scanned image, not text-based. `pdfplumber` only reads embedded text — re-export from the source as a text PDF. |
| Mac bundle won't open by double-clicking | First launch must be right-click → Open. The Gatekeeper warning on unsigned apps is normal. |
| Windows SmartScreen blocks the .exe | Click **More info** → **Run anyway**. |

---

## Where settings and logs live

The app stores its config and logs **outside** the source tree so they survive across reinstalls and work inside frozen bundles:

| Platform | Location |
|---|---|
| Mac | `~/Library/Application Support/ProPresenter Runsheet Builder/` |
| Windows | `%APPDATA%\ProPresenter Runsheet Builder\` |

Files in there: `settings.json` (includes your OpenRouter API key — gitignored, never committed), `app.log` (rotated, 512 KB × 3).
