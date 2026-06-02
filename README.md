# Runsheet Pilot

A local web app that turns a church service runsheet PDF into a ProPresenter playlist. It reads the PDF, uses an OpenRouter AI model to parse it into structured items, fuzzy-matches songs against your local ProPresenter library, and creates the playlist via ProPresenter's REST API. Optionally creates countdown timers for each section.

Runs on macOS and Windows. Opens at `http://localhost:5757` in your browser.

---

## Before you start

You need three things on the machine that will run the app:

### 1. Python 3

- **Mac:** `brew install python` or download from [python.org](https://python.org).
- **Windows:** download from [python.org](https://python.org). During install, **tick "Add Python to PATH"** — the launcher needs `python` on PATH.

### 2. ProPresenter running, with Network enabled

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

- **Mac:** `./build_mac.sh` → `dist/Runsheet Pilot.app` and a `.dmg` installer.
- **Windows:** `build_win.bat` → `dist\Runsheet Pilot.exe`.

Bundles are unsigned, so the first launch shows a warning:

- **Mac:** right-click the app → **Open** (one-time Gatekeeper prompt).
- **Windows:** click **More info** → **Run anyway** on the SmartScreen prompt.

The Windows `.exe` must be built on a Windows machine — PyInstaller doesn't cross-compile.

---

## Typical workflow

The main panel walks you through three numbered steps; each one unlocks
when the previous is done, so it's hard to do them out of order.

1. Make sure ProPresenter is running with Network enabled.
2. Start the app — the library is fetched silently in the background.
3. On first run, open **⚙ Settings** and paste your OpenRouter API key
   + model (e.g. `openai/gpt-oss-20b:free`). Auto-saves.
4. **Step 1 — Upload Runsheet:** drop the PDF on the upload card.
5. **Step 2 — Parse with AI:** click *Parse Runsheet*. The AI extracts
   items; songs are fuzzy-matched against your library; non-song items
   that match a section in your **Template Playlist** (see below) get
   their slides reused automatically.
6. Review matches; override any wrong song matches via the **Pick** button.
7. **Step 3 — Create Runsheet & Export:** click *Create Runsheet &
   Export File*. The playlist appears in ProPresenter; if an export
   folder is set in Settings, the `.playlist` file is copied there too.

### Template playlist (reusable per-service slides)

Create a playlist in ProPresenter named something like *"Youth Service -
Library"* or *"Sunday Morning Library"* and fill it with the slides you
reuse every week, grouped under header rows (Culture, Welcome, MC Open,
Worship, …). The app auto-detects this playlist on parse and feeds the
header names to the AI as context. When a runsheet item maps to one of
those sections, the resulting playlist gets the section's slides
expanded under the runsheet's own header. Operators stop dragging the
same slides into every week's playlist by hand.

The sidebar **Template Playlist** picker controls this. **Auto** routes
by runsheet content — a youth runsheet picks the Youth library, a
Sunday runsheet picks the Sunday library. Override the dropdown to lock
to a specific template.

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

## Service Mate (paid add-on)

If you have one or more **GeekMagic SmallTV-Ultra** clocks on your LAN, the
app can use them as silent visual cue prompters at the Screen, Sound, and
Lights stations during a service. Each clock shows the **current** runsheet
item, a live MM:SS countdown, and a short **role-aware** hint of what's next
(e.g. for the sound op, "Mic on for Ps Nick"; for the lights op, "Spot —
preacher").

> **Service Mate is a paid add-on with a 14-day free trial.** The rest of
> Runsheet Pilot — upload, AI parse, and create-playlist — is **free
> forever**. The trial starts the first time you switch Service Mate on, and
> runs fully featured for 14 days. After that you'll need a licence key to
> keep pushing cues to your clocks.

### Get a key & activate

1. **Buy a licence** — see the in-app *Settings → Service Mate (paid add-on)*
   section for the purchase link. You'll receive a key that looks like
   `RP1.…`.
2. Open **⚙ Settings → Service Mate (paid add-on)**, paste the key into the
   **Licence key** box, and click **Activate**. It should turn green and show
   *"Licensed to <your church>"*.
3. That's it — the licence is stored on your machine and works **offline**
   (no internet needed during a service). One key unlocks Service Mate on
   that computer.

Service Mate is **off by default** — fresh installs see a collapsed card at
the bottom of the main panel with a master switch in OFF position. If you
don't own a GeekMagic clock, leave it off and nothing runs in the background.

### Setup

1. Plug the clock into USB power, connect it to your Wi-Fi via the GeekMagic
   mobile app, and note its IP address (your router's client list works too).
2. Scroll to the **⏰ Service Mate Clocks** card and **flip the master switch
   to ON**. The card auto-expands.
3. Enter the IP for each role (Screen, Sound, Lights). Click **Probe** to
   confirm the IP — a green ✓ means the clock answered. Click **Test** to
   push a coloured test card.
4. Upload + parse a runsheet, then **Create Runsheet & Export** — the
   runsheet is saved to disk and the clocks immediately start showing item 1.
5. Use the **Prev / Next** buttons in the Cue panel to step through the
   service, or leave **Auto-track ProPresenter** on and the clocks will
   follow whichever `[RB]` countdown timer is running in ProPresenter.

Flip the master switch back to OFF at any time to silence everything — the
clock IPs and other settings are preserved for next time.

### How it works

The clocks have no app-level "show this text" endpoint. The app renders a
240×240 PNG every couple of seconds (current item, countdown, cue line) and
pushes it to each clock over HTTP using the device's stock firmware — no
flashing required. Per-role cues are LLM-generated when the runsheet is
parsed, with a built-in rule-table fallback if the LLM doesn't supply one.

### Troubleshooting

| Problem | Fix |
|---|---|
| Probe says "✗ ConnectionError" | The IP is wrong, or the clock is on a different subnet from your computer. |
| Test pushes but the screen doesn't update | The clock is in clock/weather mode. The first push of a session sends `theme=3` to switch it into custom-image mode — wait ~3 seconds. |
| Clock shows old image after restart | The app's runsheet state is on disk; pull up the page and click any **Next/Prev** button to force a refresh. |
| Auto-track doesn't follow | Auto-track only follows `[RB]`-prefixed timers (the ones this app creates). Make sure **Create timers** was on when you created the playlist. |

---

## Running the tests

Pure-function and Flask-route tests live under `tests/`. They don't talk to ProPresenter, OpenRouter, or any GeekMagic device — safe to run anywhere:

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

The same suite runs on every PR and every push to `main` via GitHub Actions (`.github/workflows/test.yml`).

---

## Where settings and logs live

The app stores its config and logs **outside** the source tree so they survive across reinstalls and work inside frozen bundles:

| Platform | Location |
|---|---|
| Mac | `~/Library/Application Support/Runsheet Pilot/` |
| Windows | `%APPDATA%\Runsheet Pilot\` |

Files in there: `settings.json` (includes your OpenRouter API key — gitignored, never committed), `app.log` (rotated, 512 KB × 3), `runsheet_state.json` (Service Mate cue state), `clocks.json` (Service Mate clock IPs).
