/* ─────────────────────────────────────────────────────────────────────────
   UI script. Sections below match the regions in the Python module map:
     1. Globals + UI helpers
     2. Settings: load + auto-save
     3. PDF upload + drag-and-drop
     4. Library load (disk + API)
     5. Connection test
     6. Parse + render results table
     7. Create playlist in ProPresenter
     8. AI prompt modal
     9. Quit + boot
   ───────────────────────────────────────────────────────────────────────── */

// ─── 1. Globals + UI helpers ──────────────────────────────────────────────
let libraryItems = [];
let matchedItems = [];
let uploadedFile = null;
let saveTimer = null;
let suppressAutoSave = true; // suppress during initial loadSettings()

const AUTOSAVE_FIELDS = [
  'or-key', 'or-model', 'lib-dir', 'export-dir', 'sm-hide',
  'stats-enabled', 'auto-port', 'media-assist',
  'pp-host', 'pp-port', 'pp-host2', 'pp-port2', 'threshold',
  'create-timers', 'template-playlist'
];

// Rolling record of real parse durations (seconds), persisted in
// settings. The step-2 estimate and the orb progress bar both run off
// its average, so the "how long will this take" answer is learned from
// this operator's actual PDFs and model, not a hardcoded guess.
let _parseTimes = [];
function _parseAvgSecs() {
  if (!_parseTimes.length) return 15;
  return _parseTimes.reduce((a, b) => a + b, 0) / _parseTimes.length;
}
function _renderParseEstimate() {
  document.getElementById('step-2-meta').textContent =
    '~' + Math.round(_parseAvgSecs()) + ' seconds';
}
function _recordParseTime(secs) {
  _parseTimes = _parseTimes.slice(-9).concat(Math.round(secs * 10) / 10);
  _renderParseEstimate();
  // save_settings merges partial posts, so this can't clobber anything.
  fetch('/api/settings', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({parse_times: _parseTimes})}).catch(() => {});
}

// Cached library source mode ('auto'|'api'|'disk') from settings. Updated
// by the radio buttons in the Settings modal; read by loadLibraryAuto so
// each refresh respects the operator's preference.
let libSourceMode = 'auto';

function setSaveDot(state) {
  const dot = document.getElementById('save-dot');
  dot.className = 'save-dot ' + (state || '');
  dot.textContent = state === 'saving' ? 'saving…'
                 : state === 'saved'  ? 'saved ✓'
                 : 'auto-save';
}

function switchTab(t) {
  document.getElementById('tab-btn-disk').classList.toggle('active', t === 'disk');
  document.getElementById('tab-btn-api').classList.toggle('active', t === 'api');
  document.getElementById('tab-disk').classList.toggle('active', t === 'disk');
  document.getElementById('tab-api').classList.toggle('active', t === 'api');
}

function setStatus(html, color) {
  // Writes ONLY the text span — the Start-over button shares this bar
  // and must survive every status update. Any real status silences the
  // idle greeter until the next reset.
  _stopIdleGreeter();
  const bar = document.getElementById('status-bar');
  document.getElementById('status-text').innerHTML = html;
  bar.style.color = color || 'var(--muted)';
}

// ─── Idle greeter — the app feels alive while it waits ────────────────────
// A waving hand and a slowly typed rotation of welcomes/encouragements,
// shown only in the idle state (fresh launch, or after Start over). The
// first real status message stops it. Reduced-motion users get a static
// line — no typing, no waving.
const IDLE_GREETINGS = [
  'Welcome — drop a runsheet on Step 1 to begin.',
  'Welcome to efficiency.',
  'Thanks for everything you do — I\'m just here to help.',
  'Sunday\'s coming. Let\'s get the runsheet sorted.',
  'One runsheet in, one ProPresenter playlist out.',
  'You bring the service. I\'ll bring the slides.',
  'PDF, or just a screenshot — either works.',
];
let _greetTimers = [];
function _stopIdleGreeter() {
  _greetTimers.forEach(clearTimeout);
  _greetTimers = [];
}
function _startIdleGreeter() {
  _stopIdleGreeter();
  const el = document.getElementById('status-text');
  const bar = document.getElementById('status-bar');
  if (!el) return;
  bar.style.color = 'var(--muted)';
  const reduced = window.matchMedia
    && matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) {
    el.innerHTML = '👋 ' + IDLE_GREETINGS[0];
    return;
  }
  let gi = 0;
  const showNext = () => {
    const text = IDLE_GREETINGS[gi];
    gi = (gi + 1) % IDLE_GREETINGS.length;
    let i = 0;
    const type = () => {
      el.innerHTML = '<span class="wave">👋</span> ' +
        escapeHtml(text.slice(0, i)) +
        (i < text.length ? '<span class="type-caret"></span>' : '');
      if (i < text.length) {
        i++;
        _greetTimers.push(setTimeout(type, 26));
      } else {
        _greetTimers.push(setTimeout(showNext, 7000));
      }
    };
    type();
  };
  showNext();
}
function setLoading(msg) {
  // Text only — no spinner. Whatever is loading shows exactly ONE loader
  // (the orb); the status bar narrates, it doesn't compete.
  setStatus(msg);
}

// Toggle the visual state of a numbered step card (1, 2, 3).
//   'locked'   → grey badge, body hidden, "complete step X first" hint
//   'active'   → glowing blue badge, body fully interactive
//   'complete' → green check badge, body still interactive
//   'busy'     → spinner-on-badge, body locked
function setStepState(n, state) {
  const card = document.getElementById(`step-card-${n}`);
  if (!card) return;
  card.classList.remove('state-locked', 'state-active',
                        'state-complete', 'state-busy');
  card.classList.add('state-' + state);
}

// ─── 2. Settings: load + auto-save ────────────────────────────────────────
// Fill the Model dropdown from /api/models (OpenRouter's live catalogue,
// filtered to free models that can return JSON) and select `saved`.
//
// A hardcoded list is what this replaces: the app used to ship
// google/gemini-2.0-flash-exp:free as the default, OpenRouter retired it, and
// every install that never changed the setting started failing with a 404
// nobody could fix without a new release.
//
// Degrades quietly. If OpenRouter can't be reached we keep whatever is saved
// as a single option so the user's choice survives and Settings still opens.
async function loadModels(saved) {
  const sel  = document.getElementById('or-model');
  const note = document.getElementById('or-model-note');
  if (!sel) return;
  let data = {models: [], auto: null, available: false};
  try {
    data = await fetch('/api/models').then(r => r.json());
  } catch (e) { /* offline — fall through to the degraded path below */ }

  sel.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = data.auto
    ? `Automatic — currently ${data.auto}`
    : 'Automatic (recommended)';
  sel.appendChild(auto);

  for (const m of data.models || []) {
    const o = document.createElement('option');
    o.value = m.id;
    const k = Math.round((m.context_length || 0) / 1000);
    o.textContent = k ? `${m.id} — ${k}k context` : m.id;
    sel.appendChild(o);
  }

  // Keep a saved model selectable even when it isn't in the free list —
  // it may be a paid model the user chose deliberately, or one that has since
  // been retired. Silently dropping it would look like the app forgot.
  if (saved && !Array.from(sel.options).some(o => o.value === saved)) {
    const o = document.createElement('option');
    o.value = saved;
    o.textContent = data.available ? `${saved} — not in the free list` : saved;
    sel.appendChild(o);
  }
  sel.value = saved || '';

  if (note) {
    note.textContent = data.available
      ? ''
      : 'Could not reach OpenRouter, so the model list is unavailable. '
        + 'Your saved model still works.';
  }
}

async function loadSettings() {
  const s = await fetch('/api/settings').then(r => r.json());
  document.getElementById('pp-host').value    = s.pp_host  || 'localhost';
  document.getElementById('pp-port').value    = s.pp_port  || '50001';
  document.getElementById('pp-host2').value   = s.pp_host  || 'localhost';
  document.getElementById('pp-port2').value   = s.pp_port  || '50001';
  document.getElementById('or-key').value     = s.or_key   || '';
  document.getElementById('stats-enabled').checked = s.stats_enabled !== false;
  document.getElementById('auto-port').checked     = s.auto_port !== false;
  document.getElementById('media-assist').checked  = !!s.media_assist;
  // Populate the model dropdown from OpenRouter's live catalogue, then select
  // whatever is saved. Empty = Automatic. Awaited so the saved value has an
  // <option> to land on — otherwise assigning .value to a <select> that
  // doesn't contain it silently selects nothing.
  await loadModels(s.or_model || '');
  const lk = document.getElementById('license-key');
  if (lk) lk.value = s.license_key || '';
  // Service Mate licence/trial state (drives the Settings status line, the
  // card badge, and the locked overlay). Best-effort; never blocks boot.
  loadLicense();
  document.getElementById('lib-dir').value    = s.library_dir || '';
  document.getElementById('export-dir').value = s.export_dir  || '';
  document.getElementById('threshold').value  = Math.round((s.threshold || .55) * 100);
  document.getElementById('thresh-val').textContent = document.getElementById('threshold').value + '%';
  document.getElementById('create-timers').checked = s.create_timers !== false;
  _aliases = Array.isArray(s.template_aliases) ? s.template_aliases : [];
  renderAliasRows();
  _parseTimes = Array.isArray(s.parse_times) ? s.parse_times.slice(-10) : [];
  _renderParseEstimate();

  // Hide Service Mate entirely for operators who don't own a clock —
  // removes the whole panel from the main screen, nothing else changes.
  const smHide = document.getElementById('sm-hide');
  smHide.checked = s.sm_hide === true;
  applySmHidden(smHide.checked);
  smHide.addEventListener('change', e => applySmHidden(e.target.checked));

  // Library source preference (auto / api / disk). Used by loadLibraryAuto
  // to constrain which source(s) it tries. Default "auto" matches the
  // old behaviour (try API, fall back to disk).
  libSourceMode = (s.lib_source || 'auto').toLowerCase();
  if (!['auto', 'api', 'disk'].includes(libSourceMode)) libSourceMode = 'auto';
  const rb = document.querySelector(`input[name="lib-source"][value="${libSourceMode}"]`);
  if (rb) rb.checked = true;

  // Template playlist selection — fetch the live list of playlists from
  // PP, populate the dropdown, then select the saved UUID (if any).
  // Best-effort: if PP is unreachable the dropdown stays at "— None —".
  await loadTemplatePlaylists(s.template_playlist_uuid || '');

  // Auto-load the library in the background — operator never has to
  // click "Scan Library" or "Fetch Library" themselves. Refreshes on
  // every PDF upload too so a freshly-added song shows up without a
  // manual rescan.
  loadLibraryAuto();

  const today = new Date().toLocaleDateString('en-AU',
      {day:'2-digit', month:'short', year:'numeric'});
  document.getElementById('playlist-name').value = 'Service ' + today;

  document.getElementById('version-badge').textContent = 'v' + (s.version || '?');
  const sv = document.getElementById('settings-version');
  if (sv) sv.textContent = s.version || '?';

  const isMac = s.platform === 'darwin';
  document.getElementById('lib-dir').placeholder = isMac
    ? '~/Documents/ProPresenter/Libraries/Default'
    : 'C:\\Users\\…\\Documents\\ProPresenter\\Libraries\\Default';
  document.getElementById('export-dir').placeholder = isMac
    ? '~/Desktop'
    : 'C:\\Users\\…\\Desktop';

  // Wire up auto-save AFTER initial values are set, so setting them doesn't trigger a save
  AUTOSAVE_FIELDS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('input', autoSaveDebounced);
    el.addEventListener('change', autoSaveDebounced);
  });
  // Library source radio set — each click updates libSourceMode + autosaves +
  // triggers a re-load so the operator sees the new source's items immediately.
  document.querySelectorAll('input[name="lib-source"]').forEach(rb => {
    rb.addEventListener('change', () => {
      libSourceMode = rb.value;
      autoSaveDebounced();
      reloadLibraryAuto();
    });
  });
  // Mirror host/port between the two sections
  document.getElementById('pp-host').addEventListener('input', e =>
    document.getElementById('pp-host2').value = e.target.value);
  document.getElementById('pp-port').addEventListener('input', e =>
    document.getElementById('pp-port2').value = e.target.value);
  document.getElementById('pp-host2').addEventListener('input', e =>
    document.getElementById('pp-host').value = e.target.value);
  document.getElementById('pp-port2').addEventListener('input', e =>
    document.getElementById('pp-port').value = e.target.value);

  suppressAutoSave = false;
  setSaveDot('');
}

function autoSaveDebounced() {
  if (suppressAutoSave) return;
  setSaveDot('saving');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveSettings, 500);
}

async function saveSettings() {
  const data = {
    pp_host:                 document.getElementById('pp-host2').value,
    pp_port:                 document.getElementById('pp-port2').value,
    or_key:                  document.getElementById('or-key').value,
    stats_enabled:           document.getElementById('stats-enabled').checked,
    auto_port:               document.getElementById('auto-port').checked,
    media_assist:            document.getElementById('media-assist').checked,
    or_model:                document.getElementById('or-model').value,
    library_dir:             document.getElementById('lib-dir').value,
    lib_source:              libSourceMode,
    export_dir:              document.getElementById('export-dir').value,
    threshold:               parseInt(document.getElementById('threshold').value) / 100,
    create_timers:           document.getElementById('create-timers').checked,
    template_playlist_uuid:  document.getElementById('template-playlist').value,
    sm_hide:                 document.getElementById('sm-hide').checked,
    // Drop half-typed rows so a blank pair can't shadow a real match.
    template_aliases:        _aliases.filter(a => (a.match || '').trim()
                                              && (a.template || '').trim()),
  };
  try {
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    setSaveDot('saved');
    setTimeout(() => setSaveDot(''), 1500);
  } catch (e) {
    setSaveDot('');
    setStatus('Could not save settings: ' + e, 'var(--red)');
  }
}

// ─── 3. Runsheet upload + drag-and-drop ───────────────────────────────────
// PDFs and screenshots. Images are OCR'd locally (see parsing/ocr.py) and
// only ever TEXT goes to OpenRouter — of the free models exactly one
// accepts images, so an image pathway would shrink the operator's model
// pool to a single provider.
const ACCEPTED_EXTS = ['.pdf', '.png', '.jpg', '.jpeg'];

function _isAccepted(name) {
  const n = (name || '').toLowerCase();
  return ACCEPTED_EXTS.some(ext => n.endsWith(ext));
}

function handleDragOver(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.add('drag-over');
}
function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file && _isAccepted(file.name)) handleFileSelect(file);
  else setStatus('Drop a PDF, or a PNG/JPG screenshot of the runsheet.',
                 'var(--red)');
}
function handleFileSelect(file) {
  if (!file) return;
  uploadedFile = file;
  // Every new runsheet starts with matching ON. See resetMatchToggle().
  resetMatchToggle();
  _hideOcrReview();
  const dz = document.getElementById('drop-zone');
  dz.classList.add('has-file');
  dz.innerHTML = `
    <div style="font-size:1.8rem;margin-bottom:6px">✅</div>
    <div style="font-weight:700;color:var(--grn)">${file.name}</div>
    <div class="hint">${(file.size/1024).toFixed(0)} KB</div>`;
  // From here, "Start over" is the ONE way to change course — a second
  // hidden path (clicking the zone to swap files) made the state model
  // ambiguous. The button lives right beside this card's title.
  dz.onclick = null;
  document.getElementById('reset-btn').hidden = false;
  // Show the filename + size beside the step 1 title for at-a-glance state.
  document.getElementById('step-1-meta').textContent =
    `${file.name} · ${(file.size/1024).toFixed(0)} KB`;
  // Advance the step flow: step 1 done, step 2 unlocked, step 3 still locked.
  setStepState(1, 'complete');
  setStepState(2, 'active');
  // Silent library refresh — picks up any songs the operator added in PP
  // since the app launched, so the upcoming match table is current.
  loadLibraryAuto();
  // Read the file NOW rather than at Parse. For a screenshot this is where
  // the operator finds out whether OCR could read it — before they commit
  // one of a free account's 50 daily OpenRouter requests to it.
  extractText(file);
}

// ─── OCR review panel ─────────────────────────────────────────────────────
// Shown only when the text came from OCR. A text PDF is exact, so the path
// every Sunday runsheet takes gains no extra step.
function _hideOcrReview() {
  const box = document.getElementById('ocr-review');
  if (box) { box.hidden = true; document.getElementById('ocr-text').value = ''; }
}

function _showOcrReview(text) {
  const box = document.getElementById('ocr-review');
  document.getElementById('ocr-text').value = text;
  document.getElementById('ocr-review-meta').textContent =
    `${text.split('\n').length} lines read`;
  box.hidden = false;
}

async function extractText(file) {
  setLoading('Reading your runsheet…');
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/api/extract_text', {method: 'POST', body: form})
      .then(r => r.json());
    if (res.error) {
      // The file is unusable, so don't leave Step 2 looking ready.
      setStatus('❌ ' + escapeHtml(res.error), 'var(--red)');
      setStepState(1, 'active');
      setStepState(2, 'locked');
      uploadedFile = null;
      return;
    }
    if (res.needs_review) {
      _showOcrReview(res.text);
      setStatus('📝 Read your screenshot — check the text on Step 2, ' +
                'fix anything odd, then <strong>🔍 Parse Runsheet</strong>.');
    } else {
      setStatus('Runsheet loaded — click <strong>🔍 Parse Runsheet</strong> ' +
                'on Step 2.');
    }
  } catch (e) {
    setStatus('❌ ' + escapeHtml(String(e)), 'var(--red)');
  }
}

// ─── "Populate with media from PP" toggle ────────────────────────────────
// Lives in the sidebar under the template picker it governs. Unlike every
// other sidebar control it does NOT persist to settings.json — it resets
// to on with each new runsheet, because a matching switch silently left
// off would produce a bare playlist next Sunday.
function matchingOn() {
  const el = document.getElementById('match-toggle');
  return !el || el.checked;
}

function resetMatchToggle() {
  const el = document.getElementById('match-toggle');
  if (el && !el.checked) { el.checked = true; onMatchToggle(); }
}

function onMatchToggle() {
  // Grey out the template picker and its refresh button when matching is
  // off — with nothing to match against, they'd be live controls that do
  // nothing, which reads as a bug.
  const on = matchingOn();
  const sel = document.getElementById('template-playlist');
  const refresh = document.querySelector(
    '[onclick="loadTemplatePlaylists()"]');
  [sel, refresh].forEach(el => {
    if (!el) return;
    el.disabled = !on;
    el.style.opacity = on ? '' : '.45';
  });
  const status = document.getElementById('template-status');
  if (status) {
    status.innerHTML = on
      ? '<strong>Auto</strong> routes by runsheet content.'
      : 'Off — you\'ll get coloured headers and timers only, no songs or ' +
        'template media. Resets on each new runsheet.';
  }
}

// ─── Template playlist dropdown ───────────────────────────────────────────
// The template playlist is a PP playlist whose section headers (Culture,
// Welcome, …) and the media items under them get reused at parse time —
// when a runsheet item matches a section name, the new playlist gets that
// section's media items expanded in place. See routes/parse.py for the
// resolution + propresenter/templates.py for the section grouping.
async function loadTemplatePlaylists(selectUuid) {
  const sel = document.getElementById('template-playlist');
  const status = document.getElementById('template-status');
  const host = document.getElementById('pp-host2').value || 'localhost';
  const port = document.getElementById('pp-port2').value || '50001';
  status.textContent = 'Loading playlists from PP…';
  try {
    const qs = new URLSearchParams({host, port}).toString();
    const res = await fetch('/api/pp/playlists?' + qs).then(r => r.json());
    const playlists = res.playlists || [];
    // Rebuild dropdown: keep the "Auto" option first, then all playlists.
    // Auto = empty value; backend reads it as "pick best for this runsheet"
    // (route by runsheet content — youth/sunday/etc.).
    sel.innerHTML = '<option value="">⚡ Auto — pick best for this runsheet</option>';
    playlists.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.uuid;
      const meta = p.section_count
        ? ` — ${p.section_count} section${p.section_count!==1?'s':''}, ${p.media_count} slide${p.media_count!==1?'s':''}`
        : ' — (no sections)';
      opt.textContent = p.name + meta;
      sel.appendChild(opt);
    });
    // Selection priority: explicit saved UUID > Auto (empty).
    // When the operator hasn't picked anything (selectUuid == '' or unset),
    // keep the dropdown on Auto rather than silently picking the auto-
    // detected playlist — that way the operator SEES "Auto" is in effect
    // and the routing happens fresh on every parse.
    const want = selectUuid || '';
    if (want && [...sel.options].some(o => o.value === want)) {
      sel.value = want;
    } else {
      sel.value = '';
    }
    setPPDot(playlists.length > 0);
    if (!playlists.length) {
      status.innerHTML = '<span style="color:var(--org)">No playlists found — is ProPresenter running with Network mode on?</span>';
    } else if (sel.value) {
      const picked = playlists.find(p => p.uuid === sel.value);
      status.innerHTML = `Locked to <strong>${escapeHtml(picked?.name || 'selected playlist')}</strong> as template (${picked?.section_count || 0} section${(picked?.section_count||0)!==1?'s':''}). Switch to <em>⚡ Auto</em> to route by runsheet content.`;
    } else if (res.auto_detected) {
      // Show which playlist Auto would pick RIGHT NOW (no runsheet yet, so
      // it falls back to the first library-named playlist; on parse the
      // actual pick uses the runsheet content too).
      const guess = playlists.find(p => p.uuid === res.auto_detected);
      status.innerHTML = `<strong>⚡ Auto</strong> — currently would pick <strong>${escapeHtml(guess?.name || '?')}</strong>. On parse, routes by runsheet content (youth/sunday/etc.). Override above to lock a specific template.`;
    } else {
      status.innerHTML = `${playlists.length} playlist${playlists.length!==1?'s':''} loaded — name one with "library" or "template" to enable Auto routing, or pick one above.`;
    }
  } catch (e) {
    setPPDot(false);
    status.innerHTML = `<span style="color:var(--red)">Could not load playlists: ${escapeHtml(String(e))}</span>`;
  }
}

// ─── Downloaded-media assist ──────────────────────────────────────────────
// ProPresenter's media API is read-only and its Media bin is a registry,
// not a folder — a file copied into PP's media folder stays invisible
// (verified against a real install). So the drag is irreducible. What the
// app does is everything around it: find the file, say which runsheet
// item it's for, and notice the moment it lands in the bin.
let _mediaAssistTimer = null;
let _mediaAssistPolls = 0;

// Poll only while it can achieve something, and not forever. With
// ProPresenter closed every file reads as "not imported", so a naive
// "keep polling while anything is pending" never stops — it re-scans
// Downloads and re-posts the whole runsheet every 3s for the life of
// the page.
const MEDIA_POLL_MS = 3000;
const MEDIA_POLL_MAX = 100;          // ~5 minutes, then it gives up

function _stopMediaPoll() {
  clearInterval(_mediaAssistTimer);
  _mediaAssistTimer = null;
}

async function loadMediaAssist(isPoll) {
  const card = document.getElementById('media-assist-card');
  if (!card) return;
  try {
    const res = await fetch('/api/media_assist', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        items: matchedItems.map(mi => mi.parsed),
        host: document.getElementById('pp-host2').value,
        port: document.getElementById('pp-port2').value,
        // Only the first look is a "view"; the polls that follow are
        // the same view, and counting each one would make the metric
        // measure how long a window stayed open.
        poll: !!isPoll,
      })
    }).then(r => r.json());

    const anything = (res.files || []).length || (res.timers || []).length;
    if (!res.enabled || !anything) {
      _stopMediaPoll();
      card.hidden = true;
      return;
    }
    renderMediaAssist(res);
    card.hidden = false;
  } catch (e) {
    _stopMediaPoll();
    card.hidden = true;
  }
}

function renderMediaAssist(res) {
  const pending = res.files.filter(f => !f.in_bin);
  const timers = res.timers || [];
  const bits = [];
  if (pending.length) bits.push(`${pending.length} to import`);
  if (timers.length) bits.push(`${timers.length} countdown${timers.length !== 1 ? 's' : ''} to sort`);
  document.getElementById('media-assist-meta').textContent =
    bits.join(' · ') || 'all imported';

  // Countdowns the runsheet asked for. Rows already covered by the
  // ProPresenter template — the Sunday countdown you reuse every week —
  // never reach here, so anything shown genuinely needs doing.
  document.getElementById('media-assist-timers').innerHTML = timers.map(t => {
    const made = t.state === 'rendered';
    const near = t.nearest
      ? ` Closest you have is ${t.nearest.minutes} min.` : '';
    const line = made
      ? `<span style="color:var(--org)">Made in Service Visuals, not in ProPresenter yet</span>`
      : `<span style="color:var(--org)">No ${t.minutes}-minute countdown exists — make one in Service Visuals.${escapeHtml(near)}</span>`;
    const action = made
      ? `<button class="btn btn-dim btn-sm" onclick="revealMedia(this)"
                 data-path="${escapeHtml(t.path)}">Show me the file</button>`
      : '';
    return `<div style="display:flex;align-items:center;gap:12px;padding:9px 0;
                        border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <div style="font-weight:600">⏱ ${escapeHtml(t.title)} — needs ${t.minutes} min</div>
        <div class="hint">${line}</div>
      </div>
      <div style="flex:none">${action}</div>
    </div>`;
  }).join('');

  document.getElementById('media-assist-list').innerHTML = res.files.map(f => {
    const kb = f.size > 1048576
      ? (f.size / 1048576).toFixed(1) + ' MB'
      : Math.round(f.size / 1024) + ' KB';
    const age = f.age_h < 1 ? 'just now'
      : (f.age_h < 24 ? Math.round(f.age_h) + 'h ago'
                      : Math.round(f.age_h / 24) + 'd ago');
    const forItem = f.suggested
      ? `<span style="color:var(--acc-2)">→ ${escapeHtml(f.suggested.title)}</span>`
      : '<span style="color:var(--dim)">no obvious match</span>';
    const state = f.in_bin
      ? '<span style="color:var(--grn);font-weight:600">✓ in ProPresenter</span>'
      : `<button class="btn btn-dim btn-sm" onclick="revealMedia(this)"
                 data-path="${escapeHtml(f.path)}">Show me the file</button>`;
    return `<div style="display:flex;align-items:center;gap:12px;padding:9px 0;
                        border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;
                    white-space:nowrap">${escapeHtml(f.name)}</div>
        <div class="hint">${kb} · ${age} · ${forItem}</div>
      </div>
      <div style="flex:none">${state}</div>
    </div>`;
  }).join('');

  // PP writes its Media registry the instant a file is dropped in, so a
  // short poll picks the import up within a second or two. It stops when
  // nothing is pending, when ProPresenter isn't answering (nothing can
  // change until it is), and after a bounded number of ticks.
  _stopMediaPoll();
  if (pending.length && res.bin_ok && _mediaAssistPolls < MEDIA_POLL_MAX) {
    _mediaAssistTimer = setInterval(() => {
      if (++_mediaAssistPolls >= MEDIA_POLL_MAX) { _stopMediaPoll(); return; }
      loadMediaAssist(true);
    }, MEDIA_POLL_MS);
  }
}

async function revealMedia(btn) {
  const path = btn.dataset.path;
  btn.disabled = true;
  btn.textContent = 'Opening…';
  try {
    const res = await fetch('/api/media_assist/reveal', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path})
    }).then(r => r.json());
    btn.textContent = res.ok ? 'Drag it into Media →' : 'Couldn\'t open';
  } catch (e) {
    btn.textContent = 'Couldn\'t open';
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Show me the file'; }, 4000);
}

// ─── Config drawer ────────────────────────────────────────────────────────
// The sidebar hides off-screen by default (v2.8.0) — it held controls used
// perhaps once a month, at the permanent cost of a third of the window.
// Opening PUSHES the content right (the pre-2.8 two-column layout) rather
// than overlaying it, so both panes stay usable and modals stack above.
// Deliberately NO persistence: every launch starts hidden, calm by default.
// The edge handle stays visible and carries the PP dot, replacing the
// at-a-glance connection check the always-on panel used to give.
function toggleDrawer() {
  const open = document.body.classList.toggle('drawer-open');
  document.getElementById('drawer-handle')
    .setAttribute('aria-expanded', String(open));
}
function closeDrawer() {
  document.body.classList.remove('drawer-open');
  document.getElementById('drawer-handle')
    .setAttribute('aria-expanded', 'false');
}
document.addEventListener('keydown', (e) => {
  // Don't steal Escape while a modal is up — the drawer sits underneath.
  if (e.key === 'Escape' &&
      !document.querySelector('.modal-backdrop.active, .whatsnew-backdrop.active')) {
    closeDrawer();
  }
});

// Green = last ProPresenter contact worked, red = failed, grey = untried.
function setPPDot(ok) {
  const dot = document.getElementById('pp-dot');
  if (dot) dot.className = 'pp-dot ' + (ok ? 'ok' : 'bad');
}

// ─── What's new (once per version) ────────────────────────────────────────
// The backend decides whether to show (version changed since last seen —
// covers the update button AND a manually installed DMG) and sends up to
// three notes from WHATS_NEW in config.py. Dismissing marks the version
// seen; a crash before render doesn't (GET is read-only), so the notes
// survive to the next launch.
const WHATSNEW_GREETINGS = [
  'Ooh, you updated — good choice.',
  'Fresh paint. Same mission.',
  'Look at you, staying current.',
  'Updated and ready to roll.',
  'New bits installed. Carry on being great.',
];

async function initWhatsNew() {
  try {
    const res = await fetch('/api/whats_new').then(r => r.json());
    if (!res.show || !(res.notes || []).length) return;
    document.getElementById('whatsnew-title').textContent =
      WHATSNEW_GREETINGS[Math.floor(Math.random() * WHATSNEW_GREETINGS.length)];
    document.getElementById('whatsnew-version').textContent =
      `You're now on v${res.version}`;
    const ul = document.getElementById('whatsnew-notes');
    ul.innerHTML = '';
    res.notes.slice(0, 3).forEach(n => {
      const li = document.createElement('li');
      li.textContent = n;
      ul.appendChild(li);
    });
    document.getElementById('whatsnew-backdrop').classList.add('active');
  } catch (e) {
    // Never let a changelog break the app.
  }
}

function dismissWhatsNew() {
  document.getElementById('whatsnew-backdrop').classList.remove('active');
  fetch('/api/whats_new/seen', {method: 'POST'}).catch(() => {});
}

// ─── 4. Library load (silent auto + manual rescan) ────────────────────────
// Library loading is now mostly invisible — auto-fires on launch and on
// every PDF upload via /api/library/auto. The Settings modal has a manual
// "Re-scan now" button for when the operator added songs in PP and wants
// the match table to reflect them without re-uploading the PDF.

function setLibStatus(msg, cls) {
  const el = document.getElementById('lib-status');
  if (!el) return;
  el.textContent = msg;
  el.className = cls || '';
}

// Update the always-visible sidebar footer pill with the current library
// status. Orange warning when no library is loaded — beginners need to
// know why songs aren't matching. "items" not "songs": PP's library
// can hold media, presentations, video clips, scripture decks — not
// just sung worship.
function setLibraryFooter(count, source) {
  const el = document.getElementById('library-footer-status');
  if (!el) return;
  if (!count) {
    el.textContent = '⚠ No library loaded — open Settings to set one.';
    el.classList.add('stat-warn');
  } else {
    const where = source === 'api'  ? 'from PP'
                : source === 'disk' ? 'from disk'
                : '';
    el.textContent = `♪ ${count} item${count!==1?'s':''} loaded${where ? ' ' + where : ''}`;
    el.classList.remove('stat-warn');
  }
}

async function loadLibraryAuto() {
  // Silent — no status-bar spinner, just the sidebar footer + the in-modal
  // lib-status line if Settings happens to be open. mode reflects the
  // operator's current radio preference.
  try {
    const qs = new URLSearchParams({
      host: document.getElementById('pp-host2').value || 'localhost',
      port: document.getElementById('pp-port2').value || '50001',
      dir:  document.getElementById('lib-dir').value || '',
      mode: libSourceMode || 'auto',
    }).toString();
    const res = await fetch('/api/library/auto?' + qs).then(r => r.json());
    libraryItems = res.items || [];
    setLibraryFooter(res.count || 0, res.source);
    if (res.source === 'api')  setLibStatus(`${res.count} item${res.count!==1?'s':''} loaded from ProPresenter`, 'stat-ok');
    else if (res.source === 'disk') setLibStatus(`${res.count} item${res.count!==1?'s':''} loaded from disk`, 'stat-ok');
    else                            setLibStatus('No library found — check the path or start ProPresenter.', 'stat-err');
  } catch (e) {
    setLibraryFooter(0, 'none');
    setLibStatus('Auto-load failed: ' + e, 'stat-err');
  }
}

// Manual "Re-scan now" button in Settings — same call as loadLibraryAuto
// but surfaces a status-bar message so the operator sees the action.
async function reloadLibraryAuto() {
  setLoading('Re-scanning library…');
  await loadLibraryAuto();
  const n = libraryItems.length;
  setStatus(n
    ? `✓ Library re-scanned — ${n} item${n!==1?'s':''} loaded.`
    : '⚠ No library found — check the source / path in Settings.',
    n ? 'var(--grn)' : 'var(--org)');
}

// ─── Settings modal (Library / OpenRouter / Playlist options / Export) ───
function openSettingsModal() {
  document.getElementById('settings-modal').classList.add('active');
}
function closeSettingsModal() {
  document.getElementById('settings-modal').classList.remove('active');
}

// ─── 4b. Service Mate licensing (paid add-on) ─────────────────────────────
// The clocks feature is a paid add-on with a 14-day free trial. These talk
// to /api/license and drive three bits of UI: the Settings status line, the
// badge on the Service Mate card header, and the locked overlay shown when
// the trial has expired.
//
// TODO(owner): point this at your real store/landing URL once you pick one
// (Gumroad / Lemon Squeezy / Stripe). For now it links to the README.
const SERVICE_MATE_BUY_URL =
  'https://github.com/jimhoggey/Runsheetpilot#service-mate-paid-add-on';

let _licenseState = 'trial_unstarted';

async function loadLicense() {
  try {
    applyLicenseState(await fetch('/api/license').then(r => r.json()));
  } catch (e) {
    console.warn('license load failed', e);
  }
}

function applyLicenseState(st) {
  if (!st || !st.state) return;
  _licenseState = st.state;
  const buy = document.getElementById('license-buy-link');
  if (buy) buy.href = SERVICE_MATE_BUY_URL;

  // 1. Settings status line.
  const line = document.getElementById('license-status');
  if (line) {
    line.classList.remove('is-trial', 'is-licensed', 'is-expired');
    if (st.state === 'licensed') {
      line.classList.add('is-licensed');
      line.textContent = '✓ Licensed to ' + (st.licensed_to || 'you') + ' — thank you!';
    } else if (st.state === 'trial') {
      line.classList.add('is-trial');
      line.textContent = '⏳ Free trial — ' + st.days_left +
        (st.days_left === 1 ? ' day left.' : ' days left.');
    } else if (st.state === 'trial_unstarted') {
      line.classList.add('is-trial');
      line.textContent = '14-day free trial — starts when you turn Service Mate on.';
    } else {
      line.classList.add('is-expired');
      line.textContent = '🔒 Trial ended — enter a key to keep using Service Mate.';
    }
  }

  // 2. Badge on the Service Mate card header.
  const badge = document.getElementById('sm-license-badge');
  if (badge) {
    badge.classList.remove('is-trial', 'is-licensed', 'is-expired');
    if (st.state === 'licensed') {
      badge.hidden = false; badge.classList.add('is-licensed'); badge.textContent = 'Licensed';
    } else if (st.state === 'trial') {
      badge.hidden = false; badge.classList.add('is-trial');
      badge.textContent = 'Trial · ' + st.days_left + 'd';
    } else if (st.state === 'expired') {
      badge.hidden = false; badge.classList.add('is-expired'); badge.textContent = 'Trial ended';
    } else {
      badge.hidden = true;
    }
  }

  // 3. Locked overlay — only when expired. Re-apply the collapse logic so
  //    the body/overlay/off-hint visibility stays consistent.
  if (typeof smApplyCollapsed === 'function') {
    const card = document.getElementById('sm-card');
    smApplyCollapsed(card ? card.hasAttribute('data-collapsed') : true);
  }
}

async function activateLicense() {
  const input = document.getElementById('license-key');
  const msg = document.getElementById('license-msg');
  const key = (input && input.value || '').trim();
  const setMsg = (color, text) => { if (msg) { msg.style.color = color; msg.textContent = text; } };
  if (!key) { setMsg('var(--org)', 'Paste your key first.'); return; }
  setMsg('var(--muted)', 'Checking…');
  try {
    const r = await fetch('/api/license', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key})});
    const st = await r.json();
    if (r.ok && st.ok) {
      setMsg('var(--grn)', '✓ Activated.');
      applyLicenseState(st);
    } else {
      setMsg('var(--red)', st.error || 'That key was not recognised.');
    }
  } catch (e) {
    setMsg('var(--red)', 'Could not reach the app. Try again.');
  }
}

async function removeLicense() {
  const input = document.getElementById('license-key');
  const msg = document.getElementById('license-msg');
  try {
    const st = await fetch('/api/license', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: ''})}).then(r => r.json());
    if (input) input.value = '';
    if (msg) { msg.style.color = 'var(--muted)'; msg.textContent = 'Key removed.'; }
    applyLicenseState(st);
  } catch (e) {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = 'Could not reach the app.'; }
  }
}

// ─── 5. Connection test ───────────────────────────────────────────────────
// Two near-identical test functions because the host/port fields appear in
// two sidebar sections (Library API tab + ProPresenter Connection). Each one
// reads the values from its own section's inputs so the user can verify
// either form before relying on it.
async function _runTest(host, port) {
  const res = await fetch('/api/test_connection', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({host, port})
  }).then(r => r.json());
  setPPDot(!!res.ok);
  // The backend may have found ProPresenter on a different port (it does
  // not always use 50001). Write the working value back into the field
  // and save it, or the operator fixes the same thing every launch.
  if (res.ok && res.port && res.port !== String(port)) {
    ['pp-port2', 'pp-port'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = res.port;
    });
    saveSettings();
    setStatus(`🔌 ${escapeHtml(res.note || 'Found ProPresenter on port ' + res.port + '.')}`);
  }
  return res;
}

async function testConnection() {
  const res = await _runTest(
    document.getElementById('pp-host').value,
    document.getElementById('pp-port').value);
  alert(res.ok
    ? `✅ Connected — ${res.count} library/libraries found.`
    : `❌ ${res.error}\nOpen ProPresenter and turn on Network (Preferences → Integrations), then try again.`);
}
async function testConnection2() {
  const res = await _runTest(
    document.getElementById('pp-host2').value,
    document.getElementById('pp-port2').value);
  alert(res.ok
    ? (res.note ? `✅ Connected.\n\n${res.note}` : `✅ Connected — ProPresenter is ready.`)
    : `❌ ${res.error}\nOpen ProPresenter and turn on Network (Preferences → Integrations), then try again.`);
}

// ─── 6. Parse runsheet + render results table ─────────────────────────────
// The post-parse nudge: one line, pointing at the only thing left to do.
// Picked at random per parse so repeat use stays light without churning
// while the operator is mid-read.
// Post-create line for the status bar. Same voice as the idle greeter —
// warm, brief, and pointing at the one place to look next. The notice
// below carries the numbers, so this never repeats them.
const DONE_LINES = [
  'Amazing — all done! Go check it in ProPresenter.',
  'That was quick. Your playlist is waiting in ProPresenter.',
  'All done! Pop over to ProPresenter and have a look.',
  'Beautiful. ProPresenter has everything now.',
  'Done and dusted — go take a look in ProPresenter.',
  'Nice work. It\'s all sitting in ProPresenter for you.',
  'That\'s Sunday sorted. Check it over in ProPresenter.',
];

const NEXT_STEP_LINES = [
  n => `${n} items ready. Step 3 does the actual work — hit Create and they land in ProPresenter.`,
  n => `Runsheet's built. One more click and ProPresenter has your ${n}-item playlist.`,
  n => `Looking good. Create Runsheet &amp; Export sends all ${n} items to ProPresenter.`,
  n => `${n} items lined up and nowhere to go — until you hit Create below.`,
  n => `That's the hard part done. Step 3 puts these ${n} items into ProPresenter.`,
];
function _showNextStepHint(count) {
  const hint = document.getElementById('next-step-hint');
  const text = document.getElementById('next-step-text');
  if (!hint || !text) return;
  const pick = NEXT_STEP_LINES[Math.floor(Math.random() * NEXT_STEP_LINES.length)];
  text.innerHTML = pick(count);
  hint.hidden = false;
}
function _hideNextStepHint() {
  const hint = document.getElementById('next-step-hint');
  if (hint) hint.hidden = true;
}

// ─── Template links (operator-taught aliases) ─────────────────────────────
// Each row is "when the runsheet says X → use template slide Y". Saved as
// template_aliases and applied at parse AND create time, so a link taught
// once keeps working even when the runsheet's wording drifts.
let _aliases = [];

function renderAliasRows() {
  const box = document.getElementById('alias-rows');
  if (!box) return;
  box.innerHTML = _aliases.map((a, i) => `
    <div class="row alias-row" style="gap:6px;align-items:center;margin-bottom:6px">
      <input type="text" placeholder="When the runsheet says…"
             value="${escapeHtml(a.match || '')}" style="flex:1;margin:0"
             oninput="updateAlias(${i}, 'match', this.value)">
      <span style="color:var(--muted)">→</span>
      <input type="text" placeholder="use this template slide"
             value="${escapeHtml(a.template || '')}" style="flex:1;margin:0"
             oninput="updateAlias(${i}, 'template', this.value)">
      <button class="btn btn-dim btn-sm" onclick="removeAlias(${i})"
              title="Remove this link">✕</button>
    </div>`).join('');
}
function addAliasRow() { _aliases.push({match: '', template: ''}); renderAliasRows(); }
function removeAlias(i) { _aliases.splice(i, 1); renderAliasRows(); saveAliases(); }
function updateAlias(i, field, value) {
  if (_aliases[i]) { _aliases[i][field] = value; autoSaveDebounced(); }
}
function saveAliases() { autoSaveDebounced(); }

function applySmHidden(hidden) {
  const card = document.getElementById('sm-card');
  if (card) card.style.display = hidden ? 'none' : '';
}

// ─── Re-match — relink the existing parse to ProPresenter as it is now ──
// The runsheet text never changes once parsed, but ProPresenter does:
// slides get renamed, media gets added, the template gets edited. This
// recomputes ONLY the links — no AI request, so it costs nothing against
// the daily free-tier cap and is instant.
async function rematchNow() {
  if (!matchedItems.length) return;
  // Pressing Re-match IS a request to match, so it turns the toggle back
  // on rather than silently contradicting it. Create later reads the
  // toggle, so leaving it off here would throw the new links away.
  resetMatchToggle();
  const btn = document.getElementById('rematch-btn');
  btn.disabled = true;
  btn.textContent = '↻ Re-matching…';
  try {
    await loadLibraryAuto();          // fresh songs straight from PP
    const parsed = matchedItems.map(mi => mi.parsed);
    const res = await fetch('/api/match', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        parsed, library: libraryItems,
        threshold: parseInt(document.getElementById('threshold').value) / 100,
        rematch_template: true,
        host: document.getElementById('pp-host2').value,
        port: document.getElementById('pp-port2').value,
        template_playlist_uuid: document.getElementById('template-playlist').value,
      })
    }).then(r => r.json());
    matchedItems = res.items;
    renderResults();
    const linked = matchedItems.filter(mi => mi.match).length;
    setStatus(`↻ Re-matched against ProPresenter — ${linked} of ` +
              `${matchedItems.length} items linked.`, 'var(--grn)');
  } catch (e) {
    setStatus('Could not re-match: ' + escapeHtml(String(e)), 'var(--org)');
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Re-match with ProPresenter';
  }
}

// ─── Start over — wipe the parsed state back to a fresh Step 1 ───────────
function resetFlow() {
  _stopMediaPoll();
  _mediaAssistPolls = 0;
  document.getElementById('media-assist-card').hidden = true;
  uploadedFile = null;
  matchedItems = [];
  document.getElementById('pdf-input').value = '';
  resetMatchToggle();
  _hideOcrReview();
  const dz = document.getElementById('drop-zone');
  dz.classList.remove('has-file');
  dz.innerHTML = `
    <div class="icon">☁️</div>
    <div style="font-weight:600;margin-bottom:4px">Drop your runsheet here, or click to browse</div>
    <div class="hint">PDF · or a PNG/JPG screenshot</div>`;
  dz.onclick = () => document.getElementById('pdf-input').click();
  document.getElementById('reset-btn').hidden = true;
  document.getElementById('results-wrap').hidden = true;
  document.getElementById('results-body').innerHTML = '';
  _hideNextStepHint();
  document.getElementById('result-notice').innerHTML = '';
  document.getElementById('step-1-meta').textContent = '';
  _renderParseEstimate();
  document.getElementById('step-3-meta').textContent = '';
  const today = new Date().toLocaleDateString('en-AU',
      {day:'2-digit', month:'short', year:'numeric'});
  document.getElementById('playlist-name').value = 'Service ' + today;
  setStepState(1, 'active');
  setStepState(2, 'locked');
  setStepState(3, 'locked');
  _startIdleGreeter();
}

async function parseRunsheet() {
  if (!uploadedFile) { setStatus('Upload a runsheet first.', 'var(--red)'); return; }
  if (!document.getElementById('or-key').value.trim()) {
    setStatus('Enter your OpenRouter API key in <strong>⚙ Settings</strong>.',
              'var(--red)');
    openSettingsModal();
    return;
  }

  const btn = document.getElementById('parse-btn');
  btn.disabled = true;
  btn.hidden = true;
  const loader = document.getElementById('parse-loader');
  loader.hidden = false;
  const orb = Orb.mount(document.getElementById('parse-orb'), 'solving');

  // The label keeps the wait human — church-flavoured, gently silly.
  const quips = ['Reading your runsheet…', 'Analyzing the speakers…',
                 'Praising the Lord…', 'Welcoming the guests…',
                 'Counting worship songs…', 'Timing the sermon…',
                 'Cueing the countdown…', 'Handing out connect cards…',
                 'Warming up the projector…', 'Checking mic levels…',
                 'Dimming the house lights…', 'Consulting the roster…',
                 'Untangling the cables…', 'Finding the offering bags…',
                 'Saving seats for latecomers…', 'Rehearsing transitions…',
                 'Locating the worship leader…', 'Putting the kettle on…',
                 'Blessing the sound desk…', 'Counting in the band…'];
  const label = document.getElementById('parse-orb-label');
  let qi = 0;
  label.textContent = quips[0];
  const quipTimer = setInterval(() => {
    qi = (qi + 1) % quips.length;
    label.textContent = quips[qi];
  }, 2600);

  // Progress bar paced by the learned average: fills to 92% over avg
  // seconds, holds there until the response truly lands, then snaps
  // full. Honest about being an estimate, useful as an indication.
  const fill = document.getElementById('parse-progress');
  fill.style.transition = 'none';
  fill.style.width = '0';
  void fill.offsetWidth;   // commit the reset before animating
  fill.style.transition = `width ${_parseAvgSecs()}s linear`;
  fill.style.width = '92%';

  const t0 = performance.now();
  let parseSucceeded = false;
  setStepState(2, 'busy');
  setLoading('Sending your runsheet to the AI…');

  const form = new FormData();
  // If the operator reviewed OCR text, THAT is the runsheet — it's the
  // corrected version of the file, so it wins over re-reading the image.
  const reviewed = document.getElementById('ocr-review').hidden
    ? '' : document.getElementById('ocr-text').value;
  if (reviewed.trim()) {
    form.append('runsheet_text', reviewed);
    form.append('filename',      uploadedFile.name);
  } else {
    form.append('pdf', uploadedFile);
  }
  form.append('matching', matchingOn() ? 'on' : 'off');
  form.append('or_key',   document.getElementById('or-key').value.trim());
  form.append('or_model', document.getElementById('or-model').value.trim());

  try {
    const res = await fetch('/api/upload_and_parse', {method:'POST', body: form})
      .then(r => r.json());
    if (res.error) {
      setStatus('❌ ' + escapeHtml(res.error), 'var(--red)');
      setStepState(2, 'active');     // back to active so they can retry
      return;
    }

    // Auto-populate the playlist name from the AI-extracted service name
    // (or filename fallback). User can still edit it before creating.
    if (res.suggested_name) {
      document.getElementById('playlist-name').value = res.suggested_name;
    }

    setLoading(matchingOn()
      ? `AI found ${res.items.length} items — matching to library…`
      : `AI found ${res.items.length} items — building headers…`);

    const threshold = parseInt(document.getElementById('threshold').value) / 100;
    const matchRes = await fetch('/api/match', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({parsed: res.items, library: libraryItems, threshold,
                            matching: matchingOn()})
    }).then(r => r.json());

    matchedItems = matchRes.items;
    renderResults();
    _showNextStepHint(matchedItems.length);
    loadMediaAssist();
    parseSucceeded = true;
    // Mark step 2 complete + unlock step 3 so the operator's eye goes to
    // the Create button.
    setStepState(2, 'complete');
    setStepState(3, 'active');
    // The timed-row guard resynthesizes rows the AI dropped; say so, so
    // the operator knows why the count beats what the model returned.
    document.getElementById('step-2-meta').textContent =
      res.rescued_rows > 0
        ? `${matchedItems.length} items (${res.rescued_rows} recovered)`
        : `${matchedItems.length} items`;
    _recordParseTime((performance.now() - t0) / 1000);
  } catch (e) {
    setStatus('❌ ' + escapeHtml(String(e)), 'var(--red)');
    setStepState(2, 'active');
  } finally {
    clearInterval(quipTimer);
    fill.style.transition = 'width .25s ease';
    fill.style.width = '100%';
    if (parseSucceeded) {
      // Completion choreography: bar snaps full, the orb takes a small
      // spring bow out, then the results spring in and the page glides
      // down to them. ~0.8s end to end.
      label.textContent = 'Done!';
      setTimeout(() => loader.classList.add('orb-done'), 220);
      setTimeout(() => {
        orb.stop();
        loader.hidden = true;
        loader.classList.remove('orb-done');
        btn.hidden = false;
        btn.disabled = false;
        const wrap = document.getElementById('results-wrap');
        wrap.hidden = false;
        wrap.classList.add('spring-in');
        setTimeout(() => wrap.classList.remove('spring-in'), 700);
        // Scroll only AFTER the spring has played. Scrolling on the same
        // tick moved the orb out of view mid-bow, so the finish animation
        // was never actually seen — the whole point of it.
        setTimeout(
          () => wrap.scrollIntoView({behavior: 'smooth', block: 'start'}),
          620);
      }, 640);
    } else {
      orb.stop();
      loader.hidden = true;
      btn.hidden = false;
      btn.disabled = false;
    }
  }
}

function tagClass(type) {
  const m = {
    song:                'tag-song',
    mc_on_stage:         'tag-mc',
    announcement:        'tag-ann',
    sermon:              'tag-serm',
    prayer_and_ministry: 'tag-prayer',
    // Legacy types from states saved by older versions — the server now
    // clamps everything to the fixed six, but keep these rendering.
    prayer:              'tag-prayer',
    scripture:           'tag-script',
    offering:            'tag-offer',
    video:               'tag-video',
  };
  return m[type] || 'tag-oth';
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function renderResults() {
  const tbody = document.getElementById('results-body');
  tbody.innerHTML = '';
  let matched = 0, total = 0;
  // Non-song items the LLM (or fuzzy fallback) linked to an existing
  // reusable presentation in the library — surfaced separately so the
  // operator knows the "Culture / Welcome / Offering" slides got wired up
  // automatically and they don't need to drag them in by hand.
  let reused = 0;

  matchedItems.forEach((mi, i) => {
    const p = mi.parsed, m = mi.match, conf = mi.confidence;
    if (p.type === 'song') total++;

    let matchCell, scoreCell;
    if (p.type === 'song') {
      if (m) {
        matchCell  = `<span class="match-ok">${escapeHtml(m.name)}</span>`;
        scoreCell  = `<span class="score">${Math.round(conf*100)}%</span>`;
        matched++;
      } else {
        matchCell = `<span class="match-bad">⚠ ACTION NEEDED placeholder</span>
          <button class="btn btn-dim btn-sm" style="margin-left:6px"
                  onclick="pickManual(${i})">Pick</button>`;
        scoreCell = `<span class="score match-bad">${Math.round(conf*100)}%</span>`;
      }
    } else if (m && m.header && Array.isArray(m.items)) {
      // Non-song that the parse step linked to a template-playlist
      // SECTION (e.g. runsheet "Culture Moment" → template section
      // "Culture" with 3 media items). Operator sees the section name
      // and how many slides will be interpolated into the playlist.
      const n = m.items.length;
      matchCell = `<span class="match-ok">♻ ${escapeHtml(m.header.name || '')} <span style="color:var(--muted);font-weight:400">(${n} slide${n!==1?'s':''})</span></span>`;
      scoreCell = `<span class="score">${Math.round((conf || 1) * 100)}%</span>`;
      reused++;
    } else if (m && m.uuid) {
      // Single-presentation reuse (older shape, still supported) —
      // one library presentation linked at parse time.
      matchCell = `<span class="match-ok">♻ ${escapeHtml(m.name)}</span>`;
      scoreCell = `<span class="score">${Math.round((conf || 1) * 100)}%</span>`;
      reused++;
    } else {
      matchCell = `<span class="match-hdr">→ section header</span>`;
      scoreCell = '';
    }

    const notes = p.notes
      ? `<span style="color:var(--muted);font-size:.78rem"> [${escapeHtml(p.notes)}]</span>` : '';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:var(--muted)">${i+1}</td>
      <td><span class="tag ${tagClass(p.type)}">${escapeHtml(p.type)}</span></td>
      <td>${escapeHtml(p.title)}${notes}</td>
      <td>${matchCell}</td>
      <td>${scoreCell}</td>`;
    tbody.appendChild(tr);
  });

  const unmatched = total - matched;
  const reusedFrag = reused
    ? ` &nbsp;·&nbsp; ♻ ${reused} reused from library` : '';
  let msg, color;
  if (total === 0) {
    msg = `${matchedItems.length} items parsed${reusedFrag} &nbsp;·&nbsp; no songs in this runsheet &nbsp;·&nbsp; Click <strong>Create Playlist</strong> when ready.`;
    color = 'var(--grn)';
  } else if (libraryItems.length === 0) {
    msg = `${matchedItems.length} items parsed${reusedFrag} &nbsp;·&nbsp; <span style="color:var(--org)">⚠ No library loaded — all ${total} song${total!==1?'s':''} will be <strong>ACTION NEEDED</strong> placeholders. Open <strong>⚙ Settings</strong> to set a library, then click Parse again.</span>`;
    color = 'var(--org)';
  } else if (unmatched) {
    msg = `${matchedItems.length} items parsed${reusedFrag} &nbsp;·&nbsp; ${matched}/${total} songs matched &nbsp;·&nbsp; <span style="color:var(--org)">${unmatched} unmatched — click <strong>Pick</strong> to choose manually, or leave for an <strong>ACTION NEEDED</strong> placeholder in the playlist.</span>`;
    color = 'var(--org)';
  } else {
    msg = `${matchedItems.length} items parsed${reusedFrag} &nbsp;·&nbsp; ✓ all ${total} song${total!==1?'s':''} matched &nbsp;·&nbsp; Click <strong>Create Playlist</strong> when ready.`;
    color = 'var(--grn)';
  }
  setStatus(msg, color);
}

function pickManual(idx) {
  const q = prompt('Search for song name (or leave blank to see all):') || '';
  const hits = libraryItems.filter(x =>
    !q || (x.name || '').toLowerCase().includes(q.toLowerCase()));
  if (!hits.length) { alert('No matches found.'); return; }
  const list = hits.slice(0, 20).map((m, i) => `${i+1}. ${m.name}`).join('\n');
  const pick = parseInt(prompt(`Choose a number:\n${list}`));
  if (isNaN(pick) || pick < 1 || pick > hits.length) return;
  matchedItems[idx].match      = hits[pick-1];
  matchedItems[idx].confidence = 1.0;
  renderResults();
}

// ─── 7. Create playlist in ProPresenter ───────────────────────────────────
async function createPlaylist() {
  if (!matchedItems.length) { setStatus('Parse a runsheet first.', 'var(--red)'); return; }
  const name = document.getElementById('playlist-name').value.trim();
  if (!name) { setStatus('Enter a service name on Step 3.', 'var(--red)'); return; }

  const btn = document.getElementById('create-btn');
  btn.disabled = true;
  const loader = document.getElementById('create-loader');
  loader.hidden = false;
  const orb = Orb.mount(document.getElementById('create-orb'), 'working');
  setStepState(3, 'busy');
  setLoading('Creating playlist in ProPresenter…');

  try {
    const res = await fetch('/api/create_playlist', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        host:          document.getElementById('pp-host2').value,
        port:          document.getElementById('pp-port2').value,
        name,
        matched:       matchedItems,
        export_dir:    document.getElementById('export-dir').value,
        create_timers: document.getElementById('create-timers').checked,
        template_playlist_uuid: document.getElementById('template-playlist').value,
        // Carried through so create skips the template rescue and the
        // media-bin walk too — otherwise turning matching off at parse
        // would quietly come back at build time.
        matching:      matchingOn(),
      })
    }).then(r => r.json());

    const notice = document.getElementById('result-notice');
    if (res.error) {
      notice.innerHTML = `<div class="notice notice-err">❌ ${escapeHtml(res.error)}</div>`;
      setStatus('Error creating playlist.', 'var(--red)');
      setStepState(3, 'active');
      return;
    }
    // Step 3 done — the nudge has served its purpose.
    _hideNextStepHint();
    setStepState(3, 'complete');
    document.getElementById('step-3-meta').textContent = `✓ "${name}"`;

    // Count template slides reused — same rule renderResults() uses for
    // the ♻ column, so the summary agrees with the table above it.
    const reused = matchedItems.filter(mi => {
      const m = mi.match;
      return (mi.parsed || {}).type !== 'song' && m
             && ((m.header && Array.isArray(m.items)) || m.uuid);
    }).length;

    // One scannable stat line — every number the operator cares about,
    // no sentences repeating what the numbers already say.
    const bits = [`${res.headers} section header${res.headers!==1?'s':''}`];
    if (reused)     bits.push(`♻ ${reused} slide${reused!==1?'s':''} reused`);
    if (res.songs)  bits.push(`${res.songs} song${res.songs!==1?'s':''} matched`);
    if (res.needs_action) {
      bits.push(`<span style="color:#fca5a5">⚠ ${res.needs_action} song${res.needs_action!==1?'s':''} to add by hand</span>`);
    }
    let html = `<div class="notice notice-ok">
      ✅ <strong>Playlist "${escapeHtml(name)}" created in ProPresenter</strong><br>
      ${bits.join(' &nbsp;·&nbsp; ')}`;
    if (res.unlinked && res.unlinked.length) {
      // Media that isn't in PP's Media area can't be attached over the
      // API at all (PP matches media by name against the Media bin).
      // One-time fix, in plain words — after it, every future create
      // links these automatically.
      const names = [...new Set(res.unlinked.map(u => u.media_name))];
      html += `<br><span style="color:#fbbf24">⚠ ${names.length} slide${names.length!==1?'s':''} couldn't be attached:
        <strong>${names.map(escapeHtml).join(', ')}</strong>.</span><br>
        ProPresenter can only attach media that's in its <strong>Media</strong> area
        (the section headers were still created). One-time fix: in ProPresenter,
        drag ${names.length!==1?'these files':'this file'} from your template playlist
        into <strong>Media</strong> in the left sidebar, then click
        <strong>Create Runsheet &amp; Export File</strong> again.`;
    }
    if (res.timers_created) {
      const cleared = res.timers_deleted
        ? `${res.timers_deleted} old cleared &nbsp;·&nbsp; ` : '';
      html += `<br>⏱ ${cleared}<strong>${res.timers_created} countdown timer${res.timers_created!==1?'s':''}</strong> ready in PP's Timer panel.`;
    } else if (res.timers_total_items > 0 && res.timers_no_duration === res.timers_total_items) {
      html += `<br>⏱ <span style="color:#fbbf24">No timers created — none of the timeable items had a recognisable duration.</span> Make sure your runsheet shows minutes (e.g. <code>9:30 AM 20 Worship and Ministry Time</code> — the 20 is the duration).`;
    } else if (res.timers_no_duration > 0) {
      html += `<br>⏱ ${res.timers_no_duration} item${res.timers_no_duration!==1?'s':''} skipped (no duration in runsheet).`;
    }
    if (res.timer_errors && res.timer_errors.length) {
      html += `<br><span style="color:#fbbf24">⚠ ${res.timer_errors.length} timer${res.timer_errors.length!==1?'s':''} failed:</span> <code style="font-size:.72rem">${escapeHtml(res.timer_errors.join('; '))}</code>`;
    }
    if (res.export_path) {
      html += `<br><br>📁 <strong>.playlist file exported to:</strong><br>
        <code>${escapeHtml(res.export_path)}</code>`;
    } else if (document.getElementById('export-dir').value) {
      html += `<br><br>⚠️ Could not find the exported file automatically.
        Check your ProPresenter/Playlists/ folder.`;
    }
    html += '</div>';
    notice.innerHTML = html;
    // Deliberately NOT a stat line — the notice below carries the
    // numbers, and printing them twice made the screen feel noisy.
    setStatus('✅ ' + DONE_LINES[Math.floor(Math.random() * DONE_LINES.length)],
              'var(--grn)');
  } catch (e) {
    setStatus('❌ ' + escapeHtml(String(e)), 'var(--red)');
    setStepState(3, 'active');
  } finally {
    orb.stop();
    loader.hidden = true;
    btn.disabled = false;
  }
}

// ─── 8. AI prompt modal ───────────────────────────────────────────────────
let promptSaveTimer = null;

async function openPromptModal() {
  const ta = document.getElementById('prompt-textarea');
  ta.value = '';
  document.getElementById('prompt-modal').classList.add('active');
  setPromptSaveDot('');
  try {
    const res = await fetch('/api/prompt').then(r => r.json());
    ta.value = res.prompt || '';
    if (res.is_default) setPromptSaveDot('default');
  } catch (e) {
    ta.value = '(could not load prompt: ' + e + ')';
  }
}

function closePromptModal() {
  document.getElementById('prompt-modal').classList.remove('active');
}

function setPromptSaveDot(state) {
  const dot = document.getElementById('prompt-save-dot');
  dot.className = 'save-dot ' + (state === 'saving' || state === 'saved' ? state : '');
  dot.textContent = state === 'saving' ? 'saving…'
                  : state === 'saved'  ? 'saved ✓'
                  : state === 'default' ? 'using default'
                  : 'auto-save';
}

function autoSavePromptDebounced() {
  setPromptSaveDot('saving');
  clearTimeout(promptSaveTimer);
  promptSaveTimer = setTimeout(savePrompt, 600);
}

async function savePrompt() {
  const value = document.getElementById('prompt-textarea').value;
  try {
    await fetch('/api/prompt', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: value})
    });
    setPromptSaveDot(value.trim() ? 'saved' : 'default');
    if (value.trim()) setTimeout(() => setPromptSaveDot(''), 1500);
  } catch (e) {
    setPromptSaveDot('');
    alert('Could not save prompt: ' + e);
  }
}

async function resetPrompt() {
  if (!confirm('Discard your customisations and revert to the built-in default prompt?')) return;
  try {
    await fetch('/api/prompt', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: ''})
    });
    const res = await fetch('/api/prompt').then(r => r.json());
    document.getElementById('prompt-textarea').value = res.prompt || '';
    setPromptSaveDot('default');
  } catch (e) {
    alert('Could not reset: ' + e);
  }
}

// ─── Easter egg ───────────────────────────────────────────────────────────
// Seven taps on the version badge within a rolling 3-second window pops
// a small thank-you toast. Hidden by design — the badge looks decorative,
// nobody clicks it on purpose, and 7 quick taps with the counter
// resetting on a 3 s gap means no accidental triggers.
let _eeTaps = 0;
let _eeTimer = null;
function bumpVersionTaps() {
  _eeTaps++;
  clearTimeout(_eeTimer);
  _eeTimer = setTimeout(() => { _eeTaps = 0; }, 3000);
  if (_eeTaps >= 7) {
    _eeTaps = 0;
    _showEasterEggToast();
  }
}
function _showEasterEggToast() {
  _burstConfetti();
  const toast = document.createElement('div');
  toast.className = 'ee-toast';
  toast.textContent = '✨ Built with care — Fynn';
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('ee-toast-show'));
  setTimeout(() => {
    toast.classList.remove('ee-toast-show');
    setTimeout(() => toast.remove(), 400);
  }, 4000);
}

// Confetti — ~60 small coloured shapes burst from the top-centre of the
// viewport, fan out, fall with gravity, fade as they go. Pure CSS keyframes
// driven by per-particle CSS custom properties; no JS frame loop.
function _burstConfetti() {
  const colors = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444',
                  '#a78bfa', '#5eead4', '#fbbf24', '#fca5a5', '#60a5fa'];
  const shapes = ['ee-confetti-square', 'ee-confetti-circle', 'ee-confetti-strip'];
  const container = document.createElement('div');
  container.className = 'ee-confetti';
  for (let i = 0; i < 60; i++) {
    const p = document.createElement('span');
    p.className = 'ee-confetti-piece ' + shapes[i % shapes.length];
    // burst origin: roughly the centre of the visible viewport
    p.style.left = (45 + Math.random() * 10) + '%';
    p.style.top  = (38 + Math.random() * 8)  + '%';
    p.style.background = colors[i % colors.length];
    // travel: ±420 px horizontally, +280..+820 px vertically (down + a bit)
    p.style.setProperty('--tx',    ((Math.random() - 0.5) * 840).toFixed(0) + 'px');
    p.style.setProperty('--ty',    (260 + Math.random() * 560).toFixed(0) + 'px');
    p.style.setProperty('--rot',   ((Math.random() - 0.5) * 1080).toFixed(0) + 'deg');
    p.style.setProperty('--dur',   (1.8 + Math.random() * 1.4).toFixed(2) + 's');
    p.style.setProperty('--delay', (Math.random() * 0.25).toFixed(2) + 's');
    container.appendChild(p);
  }
  document.body.appendChild(container);
  // Clean up after the longest particle finishes (max dur 3.2 + delay 0.25
  // = 3.45 s, plus a buffer).
  setTimeout(() => container.remove(), 3800);
}

// ─── 9. Quit + boot ───────────────────────────────────────────────────────
async function quitApp() {
  if (!confirm('Quit the Runsheet Pilot?\n\nYou can reopen it from your Applications folder.')) return;
  try { await fetch('/api/quit', {method:'POST'}); } catch (_) {}
  document.body.innerHTML = '<div style="padding:40px;text-align:center;font-family:sans-serif">'
    + '<h2>👋 Server stopped.</h2><p style="color:#888;margin-top:10px">You can close this tab.</p></div>';
}

loadSettings();

// ─── 10. Service Mate (GeekMagic clocks) ─────────────────────────────────
// Renders the clocks table, current/next preview, and the inline image
// preview that shows what each role's clock is showing right now.

let smClocksConfig = {clocks: [], brightness: 70, enabled: true};

async function smInit() {
  try {
    const cfg = await fetch('/api/clocks').then(r => r.json());
    smClocksConfig = cfg;
    smRenderClocksTable();
    document.getElementById('sm-enabled').checked = !!cfg.enabled;
    document.getElementById('sm-brightness').value = cfg.brightness || 70;
    document.getElementById('sm-brightness-val').textContent = cfg.brightness || 70;
    // Sync the master-switch visual state + body visibility with the saved
    // enabled flag. Card starts COLLAPSED for a clean default look — the
    // operator clicks the chevron to expand once they enable it.
    smApplyMasterState(!!cfg.enabled);
    smApplyCollapsed(true);
  } catch (e) {
    console.warn('sm load clocks failed', e);
    smApplyMasterState(false);
    smApplyCollapsed(true);
  }
  // Polling intervals always run; the gate is inside each callback so
  // flipping the master switch ON mid-session starts polling without a
  // page reload, and OFF stops it without leaving zombie timers behind.
  if (smClocksConfig.enabled) {
    await smRefreshState();
    smRefreshPreview();
  }
  setInterval(() => { if (smClocksConfig.enabled) smRefreshState(); }, 2000);
  setInterval(() => { if (smClocksConfig.enabled) smRefreshPreview(); }, 2000);
}

// Master switch — coarsest on/off. Flipping it drives `enabled` on
// clocks.json, which the daemon checks every tick. Off → no PP polling,
// no clock pushes, and the action endpoints (standby/preview/probe/test)
// return 409 to anyone hitting them directly.
async function smMasterToggle() {
  const enabled = document.getElementById('sm-enabled').checked;
  smApplyMasterState(enabled);
  // When turning on, auto-expand so the operator can configure clocks
  // immediately. When turning off, collapse so the card disappears from
  // view.
  smApplyCollapsed(!enabled);
  await smSaveSettings();
  // If we just turned ON, fire one refresh so cue/preview populate now
  // instead of after the 2-second poll tick.
  if (enabled) {
    smRefreshState();
    smRefreshPreview();
  }
}

function smApplyMasterState(enabled) {
  const card = document.getElementById('sm-card');
  const label = document.getElementById('sm-master-label');
  card.classList.toggle('sm-disabled', !enabled);
  if (label) label.textContent = enabled ? 'ON' : 'OFF';
}

function smToggleCollapse() {
  const card = document.getElementById('sm-card');
  smApplyCollapsed(!card.hasAttribute('data-collapsed'));
}

function smApplyCollapsed(collapsed) {
  const card = document.getElementById('sm-card');
  const body = document.getElementById('sm-body');
  const off  = document.getElementById('sm-off-hint');
  const overlay = document.getElementById('sm-locked-overlay');
  const enabled = document.getElementById('sm-enabled').checked;
  // When the paid trial has expired, the locked overlay replaces the body
  // regardless of the master switch — the feature can't run anyway.
  const expired = (_licenseState === 'expired');
  if (collapsed) {
    card.setAttribute('data-collapsed', '');
    body.hidden = true;
    if (off) off.style.display = 'none';
    if (overlay) overlay.hidden = true;
  } else {
    card.removeAttribute('data-collapsed');
    if (overlay) overlay.hidden = !expired;
    // Expanded view: show the body if SM is enabled (and not expired); show
    // the off-hint only when disabled and not expired. They never overlap.
    body.hidden = expired ? true : !enabled;
    if (off) off.style.display = (enabled || expired) ? 'none' : 'block';
  }
}

function smRenderClocksTable() {
  const body = document.getElementById('sm-clocks-body');
  body.innerHTML = '';
  for (const c of smClocksConfig.clocks) {
    const verb = (c.verbosity || 'compact');
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="padding:6px 4px;font-weight:600;text-transform:capitalize">${c.role}</td>
      <td style="padding:6px 4px">
        <input type="text" placeholder="192.168.x.x" value="${(c.ip||'').replace(/"/g,'&quot;')}"
               data-id="${c.id}" class="sm-ip-input"
               style="width:100%;padding:6px 8px;font-family:var(--font-mono);font-size:0.82rem">
      </td>
      <td style="padding:6px 4px">
        <select data-id="${c.id}" class="sm-verb-select"
                style="width:100%;padding:5px 6px;font-size:0.82rem">
          <option value="compact"${verb==='compact'?' selected':''}>Compact</option>
          <option value="detailed"${verb==='detailed'?' selected':''}>Detailed</option>
        </select>
      </td>
      <td style="padding:6px 4px">
        <button class="btn btn-dim btn-sm" onclick="smProbe('${c.id}')">Probe</button>
        <button class="btn btn-dim btn-sm" onclick="smTest('${c.id}')">Test</button>
        <span class="sm-status" data-id="${c.id}" style="margin-left:6px;font-size:0.72rem;color:var(--muted)"></span>
      </td>`;
    body.appendChild(tr);
  }
  // Wire IP inputs to save on blur
  body.querySelectorAll('.sm-ip-input').forEach(inp => {
    inp.addEventListener('blur', smSaveSettings);
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') inp.blur();
    });
  });
  // Layout selects auto-save and refresh the preview if it's showing this role
  body.querySelectorAll('.sm-verb-select').forEach(sel => {
    sel.addEventListener('change', () => {
      smSaveSettings();
      smRefreshPreview();
    });
  });
}

async function smSaveSettings() {
  const ipInputs = document.querySelectorAll('.sm-ip-input');
  const verbSels = document.querySelectorAll('.sm-verb-select');
  const updated = smClocksConfig.clocks.map(c => {
    const ipInp = Array.from(ipInputs).find(i => i.dataset.id === c.id);
    const verbSel = Array.from(verbSels).find(s => s.dataset.id === c.id);
    return {
      ...c,
      ip: ipInp ? ipInp.value.trim() : c.ip,
      verbosity: verbSel ? verbSel.value : (c.verbosity || 'compact'),
    };
  });
  const brightness = parseInt(document.getElementById('sm-brightness').value, 10);
  document.getElementById('sm-brightness-val').textContent = brightness;
  const enabled = document.getElementById('sm-enabled').checked;
  smClocksConfig = {clocks: updated, brightness, enabled};
  try {
    await fetch('/api/clocks', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(smClocksConfig)
    });
  } catch (e) { console.warn('sm save failed', e); }
}

async function smProbe(id) {
  await smSaveSettings();
  const status = document.querySelector(`.sm-status[data-id="${id}"]`);
  status.textContent = '…';
  try {
    const res = await fetch(`/api/clocks/${id}/probe`, {method:'POST'}).then(r => r.json());
    if (res.ok) {
      status.textContent = '✓ online';
      status.style.color = 'var(--grn)';
    } else {
      status.textContent = '✗ ' + (res.error || 'failed');
      status.style.color = 'var(--red)';
    }
  } catch (e) {
    status.textContent = '✗ ' + e;
    status.style.color = 'var(--red)';
  }
}

async function smTest(id) {
  await smSaveSettings();
  const status = document.querySelector(`.sm-status[data-id="${id}"]`);
  status.textContent = 'pushing…';
  status.style.color = 'var(--muted)';
  try {
    const res = await fetch(`/api/clocks/${id}/test`, {method:'POST'}).then(r => r.json());
    status.textContent = res.ok ? '✓ pushed' : '✗ failed';
    status.style.color = res.ok ? 'var(--grn)' : 'var(--red)';
  } catch (e) {
    status.textContent = '✗ ' + e;
    status.style.color = 'var(--red)';
  }
}

async function smRefreshState() {
  try {
    const state = await fetch('/api/runsheet/state').then(r => r.json());
    const items = state.items || [];
    const idx = state.current_index || 0;
    const cur = items[idx];
    const nxt = items[idx + 1];
    if (state.standby) {
      document.getElementById('sm-current').textContent =
        '⏸ Standby · clocks showing waiting page';
      document.getElementById('sm-next').textContent =
        'Load a runsheet to resume';
    } else {
      document.getElementById('sm-current').textContent =
        cur ? `Now · ${cur.title || cur.type || '—'}` : '— (no runsheet loaded)';
      document.getElementById('sm-next').textContent =
        nxt ? `Next · ${nxt.title || nxt.type || ''}` :
        (cur ? 'Last item — end of service' : 'Create a playlist to seed the runsheet');
    }
    // Sync auto-track checkbox without firing a save
    const autoBox = document.getElementById('sm-autotrack');
    const autoEnabled = (state.auto_track || {}).enabled !== false;
    if (autoBox && autoBox.checked !== autoEnabled) autoBox.checked = autoEnabled;
  } catch (e) { /* ignore */ }
}

async function smCue(delta) {
  try {
    await fetch('/api/runsheet/cue', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({delta})
    });
    smRefreshState();
    smRefreshPreview();
  } catch (e) { console.warn('cue failed', e); }
}

async function smRestart() {
  // Restart the current item's timer by re-cueing to the same index
  try {
    const state = await fetch('/api/runsheet/state').then(r => r.json());
    const idx = state.current_index || 0;
    await fetch('/api/runsheet/cue', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({index: idx})
    });
    smRefreshState();
    smRefreshPreview();
  } catch (e) { console.warn('restart failed', e); }
}

async function smStandby() {
  // Reset all clocks to the pre-service waiting page. Clears the current
  // runsheet cue but keeps the clock IPs / config — the next parsed runsheet
  // will pick up where it left off.
  if (!confirm('Send all clocks to standby?\n\nThis clears the current cue. Loading a new runsheet will resume normally.')) {
    return;
  }
  try {
    await fetch('/api/clocks/standby', {method:'POST'});
    smRefreshState();
    smRefreshPreview();
  } catch (e) { console.warn('standby failed', e); }
}

async function smSaveAutoTrack() {
  const enabled = document.getElementById('sm-autotrack').checked;
  try {
    const state = await fetch('/api/runsheet/state').then(r => r.json());
    if (!state || !state.items) return;
    state.auto_track = {enabled};
    await fetch('/api/runsheet/state', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(state)
    });
  } catch (e) { console.warn('auto-track save failed', e); }
}

function smRefreshPreview() {
  const role = document.getElementById('sm-preview-role').value;
  const img = document.getElementById('sm-preview');
  // Match the verbosity of the clock with this role, so preview = what the
  // device will show. Falls back to "compact" if no clock has this role.
  const clock = (smClocksConfig.clocks || []).find(c => c.role === role);
  const verb = (clock && clock.verbosity) || 'compact';
  img.src = `/api/clocks/preview?role=${encodeURIComponent(role)}`
          + `&verbosity=${encodeURIComponent(verb)}&t=${Date.now()}`;
}

smInit();

// Wire up the prompt-modal textarea + ESC-to-close (script runs after DOM
// is parsed, so the elements exist).
document.getElementById('prompt-textarea')
        .addEventListener('input', autoSavePromptDebounced);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closePromptModal();
});

// ─── 11. Self-update ───────────────────────────────────────────────────────
// ONE surface: the version pill in the header. The server checks GitHub in
// the background at launch; if a release is newer the pill turns into a
// persistent "⬆ Update to vX.Y.Z" button. Nothing is ever downloaded or
// swapped until that button is clicked.
//
// There is deliberately no banner. Having a banner AND a pill meant two
// competing entry points for one action, and the banner's "Later" implied
// a decision the operator hadn't been asked to make.

// Every visual state the pill can hold, in one place — so "what does the
// pill say right now" is never spread across four functions.
function _setPill(text, opts) {
  const tab = document.getElementById('badge-check-btn');
  if (!tab) return;
  opts = opts || {};
  tab.textContent = text;
  tab.disabled = !!opts.busy;
  tab.dataset.state = opts.available ? 'available' : '';
  tab.classList.toggle('badge-check-available', !!opts.available);
}

// Restore the resting label after a transient message ("Up to date ✓").
function _resetPillSoon() {
  setTimeout(() => {
    const tab = document.getElementById('badge-check-btn');
    if (tab && tab.dataset.state !== 'available') _setPill('Check for updates');
  }, 2500);
}

async function loadUpdateState() {
  try {
    const st = await fetch('/api/update').then(r => r.json());
    renderUpdateState(st);
  } catch (e) { /* server unreachable — ignore */ }
}

function renderUpdateState(st) {
  const cur = document.getElementById('update-current-version');
  if (cur && st.current) cur.textContent = st.current;

  if (st.state === 'available') {
    // Persistent and visible without hovering — this is the launch-check
    // result surfacing. Still requires a click to do anything.
    _setPill('⬆ Update to v' + st.latest, {available: true});
  } else if (st.state === 'error') {
    _setPill('⚠ Update failed — retry', {available: true});
    setStatus('Update failed: ' + escapeHtml(st.error || 'unknown error') +
              ' — you can also download it manually from ' +
              '<a href="https://github.com/jimhoggey/Runsheetpilot/releases/latest" ' +
              'target="_blank" rel="noopener">the releases page</a>.', 'var(--org)');
  } else if (st.state !== 'downloading' && st.state !== 'verifying'
             && st.state !== 'applying') {
    _setPill('Check for updates');
  }
}

async function applyUpdate() {
  _setPill('Downloading…', {busy: true});
  try {
    const r = await fetch('/api/update/apply', {method: 'POST'});
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      _setPill('⬆ Update — retry', {available: true});
      setStatus('Update could not start: ' + escapeHtml(String(body.error || r.status)), 'var(--org)');
      return;
    }
  } catch (e) { /* fall through to polling — server may already be swapping */ }
  _pollUpdateProgress();
}

async function _pollUpdateProgress() {
  try {
    const st = await fetch('/api/update').then(r => r.json());
    if (st.state === 'error') { renderUpdateState(st); return; }
    if (st.state === 'downloading' || st.state === 'verifying') {
      _setPill('Downloading…', {busy: true});
    } else if (st.state === 'applying') {
      _setPill('Restarting…', {busy: true});
    }
    setTimeout(_pollUpdateProgress, 1000);
  } catch (e) {
    // Server went away — the swap + relaunch is happening. Reload until
    // the new version answers.
    _setPill('Restarting…', {busy: true});
    _reloadWhenBack(20);
  }
}

function _reloadWhenBack(attempts) {
  if (attempts <= 0) {
    setStatus('The app is restarting — reload this page in a moment.', 'var(--org)');
    return;
  }
  setTimeout(async () => {
    try {
      await fetch('/api/health', {cache: 'no-store'});
      location.reload();
    } catch (e) {
      _reloadWhenBack(attempts - 1);
    }
  }, 1500);
}

async function checkForUpdatesNow() {
  // Settings-modal check. It reports inline AND arms the pill, so closing
  // the modal doesn't lose the result — the pill is where updates live.
  const msg = document.getElementById('update-check-msg');
  msg.style.color = 'var(--muted)';
  msg.textContent = 'Checking…';
  try {
    const st = await fetch('/api/update?refresh=1').then(r => r.json());
    if (st.state === 'dev') {
      msg.textContent = 'Running from source — self-update is disabled.';
    } else if (st.state === 'available') {
      msg.style.color = 'var(--grn)';
      msg.innerHTML = 'Version ' + st.latest + ' is ready — ' +
        '<button class="btn btn-acc btn-sm" onclick="applyUpdate()">Update &amp; Restart</button>';
      renderUpdateState(st);
    } else {
      msg.style.color = 'var(--grn)';
      msg.textContent = 'You are up to date ✓';
    }
  } catch (e) {
    msg.style.color = 'var(--org)';
    msg.textContent = 'Could not reach GitHub — check your connection.';
  }
}

// The pill's own click: check when idle, apply when an update is armed.
async function badgeCheckUpdates() {
  const tab = document.getElementById('badge-check-btn');
  if (tab.dataset.state === 'available') { applyUpdate(); return; }
  _setPill('Checking…', {busy: true});
  try {
    const st = await fetch('/api/update?refresh=1').then(r => r.json());
    if (st.state === 'available') {
      renderUpdateState(st);
    } else if (st.state === 'dev') {
      _setPill('Dev mode — no updates');
      _resetPillSoon();
    } else {
      _setPill('Up to date ✓');
      _resetPillSoon();
    }
  } catch (e) {
    _setPill('Offline — try again');
    _resetPillSoon();
  }
}

loadUpdateState();
setInterval(loadUpdateState, 60 * 60 * 1000);   // re-render if a check lands later
_startIdleGreeter();   // the app says hello until real work starts
initWhatsNew();        // once-per-version release notes, after an update
