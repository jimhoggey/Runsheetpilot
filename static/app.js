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
  'or-key', 'or-model', 'lib-dir', 'export-dir',
  'pp-host', 'pp-port', 'pp-host2', 'pp-port2', 'threshold',
  'create-timers'
];

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
  const bar = document.getElementById('status-bar');
  bar.innerHTML = html;
  bar.style.color = color || 'var(--muted)';
}
function setLoading(msg) {
  setStatus(`<div class="spinner"></div>${msg}`);
}

// ─── 2. Settings: load + auto-save ────────────────────────────────────────
async function loadSettings() {
  const s = await fetch('/api/settings').then(r => r.json());
  document.getElementById('pp-host').value    = s.pp_host  || 'localhost';
  document.getElementById('pp-port').value    = s.pp_port  || '50001';
  document.getElementById('pp-host2').value   = s.pp_host  || 'localhost';
  document.getElementById('pp-port2').value   = s.pp_port  || '50001';
  document.getElementById('or-key').value     = s.or_key   || '';
  document.getElementById('or-model').value   = s.or_model || 'google/gemini-2.0-flash-exp:free';
  document.getElementById('lib-dir').value    = s.library_dir || '';
  document.getElementById('export-dir').value = s.export_dir  || '';
  document.getElementById('threshold').value  = Math.round((s.threshold || .55) * 100);
  document.getElementById('thresh-val').textContent = document.getElementById('threshold').value + '%';
  document.getElementById('create-timers').checked = s.create_timers !== false;

  const today = new Date().toLocaleDateString('en-AU',
      {day:'2-digit', month:'short', year:'numeric'});
  document.getElementById('playlist-name').value = 'Service ' + today;

  document.getElementById('version-badge').textContent = 'v' + (s.version || '?');
  document.getElementById('footer-info').innerHTML =
    `Settings: <code>${s.data_dir || ''}</code>`;

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
    pp_host:       document.getElementById('pp-host2').value,
    pp_port:       document.getElementById('pp-port2').value,
    or_key:        document.getElementById('or-key').value,
    or_model:      document.getElementById('or-model').value,
    library_dir:   document.getElementById('lib-dir').value,
    export_dir:    document.getElementById('export-dir').value,
    threshold:     parseInt(document.getElementById('threshold').value) / 100,
    create_timers: document.getElementById('create-timers').checked,
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

// ─── 3. PDF upload + drag-and-drop ────────────────────────────────────────
function handleDragOver(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.add('drag-over');
}
function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file && file.name.toLowerCase().endsWith('.pdf')) handleFileSelect(file);
  else setStatus('Only .pdf files are supported.', 'var(--red)');
}
function handleFileSelect(file) {
  if (!file) return;
  uploadedFile = file;
  const dz = document.getElementById('drop-zone');
  dz.classList.add('has-file');
  dz.innerHTML = `
    <div style="font-size:1.8rem;margin-bottom:6px">✅</div>
    <div style="font-weight:700;color:var(--grn)">${file.name}</div>
    <div class="hint">${(file.size/1024).toFixed(0)} KB — click to change</div>`;
  dz.onclick = () => document.getElementById('pdf-input').click();
  setStatus(`PDF loaded: ${file.name} — click Parse to send to AI.`);
}

// ─── 4. Library load (disk + API) ─────────────────────────────────────────
function setLibStatus(msg, cls) {
  const el = document.getElementById('lib-status');
  el.textContent = msg;
  el.className = cls || '';
}

async function scanLibrary() {
  const dir = document.getElementById('lib-dir').value.trim();
  if (!dir) { setStatus('Enter the library folder path first.', 'var(--red)'); return; }
  setLoading('Scanning library from disk…');
  try {
    const res = await fetch('/api/library/scan', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({directory: dir})
    }).then(r => r.json());
    if (res.error) { setStatus('Error: ' + res.error, 'var(--red)'); setLibStatus('Scan failed', 'stat-err'); return; }
    libraryItems = res.items;
    setLibStatus(`✓  ${res.count} items loaded from disk`, 'stat-ok');
    setStatus(`Library scanned — ${res.count} presentations ready for matching.`, 'var(--grn)');
  } catch (e) {
    setStatus('Scan failed: ' + e, 'var(--red)');
  }
}

async function fetchLibraryApi() {
  setLoading('Fetching library from ProPresenter…');
  try {
    const res = await fetch('/api/library/fetch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        host: document.getElementById('pp-host').value,
        port: document.getElementById('pp-port').value,
      })
    }).then(r => r.json());
    if (res.error) { setStatus('Error: ' + res.error, 'var(--red)'); setLibStatus('Fetch failed', 'stat-err'); return; }
    libraryItems = res.items;
    setLibStatus(`✓  ${res.count} items via API`, 'stat-ok');
    setStatus(`Library loaded — ${res.count} presentations.`, 'var(--grn)');
  } catch (e) {
    setStatus('Fetch failed: ' + e, 'var(--red)');
  }
}

// ─── 5. Connection test ───────────────────────────────────────────────────
// Two near-identical test functions because the host/port fields appear in
// two sidebar sections (Library API tab + ProPresenter Connection). Each one
// reads the values from its own section's inputs so the user can verify
// either form before relying on it.
async function _runTest(host, port) {
  return await fetch('/api/test_connection', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({host, port})
  }).then(r => r.json());
}

async function testConnection() {
  const res = await _runTest(
    document.getElementById('pp-host').value,
    document.getElementById('pp-port').value);
  alert(res.ok
    ? `✅ Connected — ${res.count} library/libraries found.`
    : `❌ Failed:\n${res.error}\n\nEnable Network in ProPresenter → Preferences → Integrations.`);
}
async function testConnection2() {
  const res = await _runTest(
    document.getElementById('pp-host2').value,
    document.getElementById('pp-port2').value);
  alert(res.ok
    ? `✅ Connected — ProPresenter is ready.`
    : `❌ Failed:\n${res.error}\n\nMake sure ProPresenter is running and Network is enabled.`);
}

// ─── 6. Parse runsheet + render results table ─────────────────────────────
async function parseRunsheet() {
  if (!uploadedFile) { setStatus('Upload a PDF first.', 'var(--red)'); return; }
  if (!document.getElementById('or-key').value.trim()) {
    setStatus('Enter your OpenRouter API key in the sidebar.', 'var(--red)'); return; }

  const btn = document.getElementById('parse-btn');
  btn.disabled = true;
  setLoading('Uploading PDF and sending to AI…');

  const form = new FormData();
  form.append('pdf',      uploadedFile);
  form.append('or_key',   document.getElementById('or-key').value.trim());
  form.append('or_model', document.getElementById('or-model').value.trim());

  try {
    const res = await fetch('/api/upload_and_parse', {method:'POST', body: form})
      .then(r => r.json());
    if (res.error) { setStatus('❌ ' + res.error, 'var(--red)'); return; }

    // Auto-populate the playlist name from the AI-extracted service name
    // (or filename fallback). User can still edit it before creating.
    if (res.suggested_name) {
      document.getElementById('playlist-name').value = res.suggested_name;
    }

    setLoading(`AI found ${res.items.length} items — matching to library…`);

    const threshold = parseInt(document.getElementById('threshold').value) / 100;
    const matchRes = await fetch('/api/match', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({parsed: res.items, library: libraryItems, threshold})
    }).then(r => r.json());

    matchedItems = matchRes.items;
    renderResults();
  } catch (e) {
    setStatus('❌ ' + e, 'var(--red)');
  } finally {
    btn.disabled = false;
  }
}

function tagClass(type) {
  const m = {
    song:         'tag-song',
    mc_on_stage:  'tag-mc',
    announcement: 'tag-ann',
    sermon:       'tag-serm',
    prayer:       'tag-prayer',
    scripture:    'tag-script',
    offering:     'tag-offer',
    video:        'tag-video',
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
  let msg, color;
  if (total === 0) {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; no songs in this runsheet &nbsp;·&nbsp; Click <strong>Create Playlist</strong> when ready.`;
    color = 'var(--grn)';
  } else if (libraryItems.length === 0) {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; <span style="color:var(--org)"><strong>No ProPresenter songs loaded yet</strong> — all ${total} song${total!==1?'s':''} will become red <strong>ACTION NEEDED</strong> placeholders. To match automatically, click <strong>↓ Scan Library from Disk</strong> (or <strong>↓ Fetch Library</strong>) at the top of the sidebar, then re-parse.</span>`;
    color = 'var(--org)';
  } else if (unmatched) {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; ${matched}/${total} songs matched &nbsp;·&nbsp; <span style="color:var(--org)">${unmatched} unmatched — click <strong>Pick</strong> to choose manually, or leave for an <strong>ACTION NEEDED</strong> placeholder in the playlist.</span>`;
    color = 'var(--org)';
  } else {
    msg = `${matchedItems.length} items parsed &nbsp;·&nbsp; ✓ all ${total} song${total!==1?'s':''} matched &nbsp;·&nbsp; Click <strong>Create Playlist</strong> when ready.`;
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
  if (!name) { setStatus('Enter a playlist name.', 'var(--red)'); return; }

  const btn = document.getElementById('create-btn');
  btn.disabled = true;
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
      })
    }).then(r => r.json());

    const notice = document.getElementById('result-notice');
    if (res.error) {
      notice.innerHTML = `<div class="notice notice-err">❌ ${escapeHtml(res.error)}</div>`;
      setStatus('Error creating playlist.', 'var(--red)');
      return;
    }

    let html = `<div class="notice notice-ok">
      ✅ <strong>Playlist "${escapeHtml(name)}" created in ProPresenter!</strong><br>
      ${res.songs} song${res.songs!==1?'s':''} added &nbsp;·&nbsp;
      ${res.headers} section header${res.headers!==1?'s':''}`;
    if (res.needs_action) html += ` &nbsp;·&nbsp; <span style="color:#fca5a5">⚠ ${res.needs_action} <strong>ACTION NEEDED</strong> placeholder${res.needs_action!==1?'s':''}</span> — open the playlist in ProPresenter and add the song${res.needs_action!==1?'s':''} manually.`;
    if (res.timers_deleted) {
      html += `<br>🧹 Cleared ${res.timers_deleted} previous <code>[RB]</code> timer${res.timers_deleted!==1?'s':''} from PP.`;
    }
    if (res.timers_created) {
      html += `<br>⏱ <strong>${res.timers_created} duration countdown timer${res.timers_created!==1?'s':''}</strong> created in PP (named with <code>[RB]</code> prefix, prefixed by sequence number for runsheet order) — open the Timer panel and start each one when its section begins.`;
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
    const extra = res.needs_action ? `, ${res.needs_action} ACTION NEEDED` : '';
    setStatus(`✅ Playlist "${name}" created — ${res.songs} songs, ${res.headers} headers${extra}.`,
              res.needs_action ? 'var(--org)' : 'var(--grn)');
  } catch (e) {
    setStatus('❌ ' + e, 'var(--red)');
  } finally {
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

// ─── 9. Quit + boot ───────────────────────────────────────────────────────
async function quitApp() {
  if (!confirm('Quit the ProPresenter Runsheet Builder?\n\nYou can reopen it from your Applications folder.')) return;
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
  } catch (e) { console.warn('sm load clocks failed', e); }
  await smRefreshState();
  smRefreshPreview();
  // Refresh state + preview every 2 seconds
  setInterval(smRefreshState, 2000);
  setInterval(smRefreshPreview, 2000);
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
