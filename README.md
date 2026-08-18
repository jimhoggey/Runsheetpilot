# Runsheet Pilot
Project Demo on youtube: https://youtu.be/nfgaohhkAkc?si=DgqxRt6pJfySOcf_ 

A local web app that turns a church service runsheet into a ProPresenter playlist. It reads the runsheet — a PDF, or a PNG/JPG screenshot read by your operating system's own text recognition — uses an OpenRouter AI model to parse it into structured items, fuzzy-matches songs against your local ProPresenter library, and creates the playlist via ProPresenter's REST API. Optionally creates countdown timers for each section.

Runs on macOS and Windows. Opens at `http://localhost:5757` in your browser.

---

## Install

Runsheet Pilot is a ready-to-run app — **no Python, no setup**. You install it once; after that it **updates itself** (a one-click **Update & Restart** button appears in the app whenever a new version is out).

### 🍎 Mac

**Download & install**

1. Download **[Runsheet-Pilot-mac.dmg](https://github.com/jimhoggey/Runsheetpilot/releases/latest/download/Runsheet-Pilot-mac.dmg)**.
2. Open it, then drag **Runsheet Pilot** into your **Applications** folder.
3. **First launch only:** right-click the app → **Open**, then click **Open** on the prompt. (macOS shows this once because the app isn't signed by Apple; after that it opens normally.)

**Or, install with one line** — paste into **Terminal**. It downloads, installs to Applications, skips the security prompt, and launches the app:

```bash
curl -L https://github.com/jimhoggey/Runsheetpilot/releases/latest/download/Runsheet-Pilot-mac.zip -o /tmp/rp.zip && ditto -x -k /tmp/rp.zip /Applications && xattr -dr com.apple.quarantine "/Applications/Runsheet Pilot.app" && open "/Applications/Runsheet Pilot.app"
```

### 🪟 Windows

**Download & run**

1. Download **[Runsheet-Pilot-windows.exe](https://github.com/jimhoggey/Runsheetpilot/releases/latest/download/Runsheet-Pilot-windows.exe)**.
2. Move it somewhere permanent — a **Runsheet Pilot** folder or your **Desktop** works well (not the Downloads folder, so the app has a stable home for its self-updates).
3. Double-click it. Windows SmartScreen warns because the app isn't signed: click **More info → Run anyway**. (Once only — after that it launches straight away.)

**Or, download with one line** — paste into **PowerShell**. It saves the app to your Desktop:

```powershell
$ProgressPreference='SilentlyContinue'; iwr https://github.com/jimhoggey/Runsheetpilot/releases/latest/download/Runsheet-Pilot-windows.exe -OutFile "$HOME\Desktop\Runsheet Pilot.exe"
```

Then double-click **Runsheet Pilot** on your Desktop (**More info → Run anyway** the first time).

> **After this first install you never download again.** When a new version ships, the app shows an **Update & Restart** banner — one click swaps it and relaunches. Quit any time with the **Quit** button in the app.

---

## First-time setup

Two quick things so the app can build playlists. You only do these once.

### 1. Turn on ProPresenter's network API

The app talks to ProPresenter on the same computer. In ProPresenter: **Preferences → Integrations → Network → ON**. (If it's off, creating a playlist fails with a "connection refused" error.)

### 2. Add a free OpenRouter key

The app uses an OpenRouter AI model to read your runsheet PDF.

1. Sign up at [openrouter.ai](https://openrouter.ai) and create an API key.
2. In the app, open **⚙ Settings** and paste the key into **OpenRouter → API key** (it auto-saves).
3. Recommended free model: search "gpt-oss" at [openrouter.ai/models](https://openrouter.ai/models) and put e.g. `openai/gpt-oss-20b:free` in the **Model** field.

Your key stays on your computer (in the app-data folder — see [Where settings live](#where-settings-and-logs-live)); it's never committed to git and only leaves your machine in calls to OpenRouter.

---

## Run from source (for developers)

Only needed if you want to modify the code. Requires **Python 3.11+**.

```bash
git clone https://github.com/jimhoggey/Runsheetpilot.git
cd Runsheetpilot
./launch_mac.sh          # Mac: installs dependencies, then runs
```

On **Windows**, double-click **`run.bat`** instead (it does the same). Either way the app opens at `http://localhost:5757`. To build your own installable bundles: `./build_mac.sh` (Mac) or `build_win.bat` (Windows — the `.exe` must be built on a Windows machine; PyInstaller doesn't cross-compile).

---

## Typical workflow

The main panel walks you through three numbered steps; each one unlocks
when the previous is done, so it's hard to do them out of order.

1. Make sure ProPresenter is running with Network enabled.
2. Start the app — the library is fetched silently in the background.
3. On first run, open **⚙ Settings** and paste your OpenRouter API key
   + model (e.g. `openai/gpt-oss-20b:free`). Auto-saves.
4. **Step 1 — Upload Runsheet:** drop a PDF, or a PNG/JPG screenshot, on
   the upload card. Screenshots (and scanned PDFs with no embedded text)
   are read locally by your operating system's own text recognition —
   Apple Vision on Mac, Windows.Media.Ocr on Windows — and the text is
   shown for you to correct before parsing. Only text is ever sent to
   OpenRouter.
5. **Step 2 — Parse with AI:** click *Parse Runsheet*. The AI extracts
   items; songs are fuzzy-matched against your library; non-song items
   that match a section in your **Template Playlist** (see below) get
   their slides reused automatically.

   Turn **Populate with media from PP** off in the sidebar for a one-off
   event with no template and no reusable media — you get coloured
   headers and timers only, and the parse makes no ProPresenter calls at
   all. It switches itself back on with every new runsheet.
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
(e.g. for the sound op, "Mic on for Ps David"; for the lights op, "Spot —
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

## Anonymous usage stats

Runsheet Pilot sends a small amount of anonymous usage data so I can see
which parts are used and where it breaks. It is on by default and there
is a switch in **Settings → Anonymous usage stats** that turns it off
immediately.

**What is sent:** the app version, OS name and version, an event name,
and small numbers — how long a parse took, how many items and sections
were created in ProPresenter, how many songs matched, whether media
couldn't be attached, and the *type* of any error (e.g.
`FileNotFoundError`) with the file and line it came from.

**What is never sent:** your runsheets or any of their text, song
titles, media names, file names, file paths, your OpenRouter key, or
anything you typed. There is no account, no device id and no way to link
two sessions — the session id is random, held in memory only, and never
written to disk. Error messages are scrubbed of paths, filenames, quoted
values and your username before they leave the machine; the full,
unredacted detail stays in `app.log` on your computer.

The complete list of every event the app can ever send is the `EVENTS`
tuple at the top of `propresenterrunsheet/stats.py` — one screen, no
surprises. Nothing is sent when running from source.
