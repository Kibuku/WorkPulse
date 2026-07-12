// ── State ──────────────────────────────────────────────────────────────────
let watcherRunning = false;
let aiEnabled = false;
let currentDate = null;       // YYYY-MM-DD; null = today
let availableStreams = [];    // [{key, label, color}]
let todayISO = null;

const TITLE_CASE = s => (s || '').replace(/-/g,' ').replace(/\b\w/g, c => c.toUpperCase());

function fmtMins(m) {
  if (m == null) return '—';
  m = Math.round(m);
  if (m < 1) return '<1 min';
  if (m < 60) return m + ' min';
  const h = Math.floor(m / 60), mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

function escapeHtml(s) {
  // Also normalise em/en dashes to a plain hyphen — the dashboard should
  // never show "—". This covers static strings AND LLM-generated content
  // (profile, cluster summaries, reports) since nearly everything rendered
  // passes through here. A surrounding " — " collapses to " - ".
  return (s || '')
    .replace(/\s*[—–]\s*/g, ' - ')
    .replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function activeDate() { return currentDate || todayISO; }
function isViewingToday() { return !currentDate || currentDate === todayISO; }

function relativeDateLabel(iso) {
  if (!iso || !todayISO) return iso || '';
  const t = new Date(todayISO + 'T12:00:00');
  const d = new Date(iso + 'T12:00:00');
  const diff = Math.round((t - d) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  if (diff > 1 && diff < 7) return d.toLocaleDateString(undefined, {weekday:'long'});
  return d.toLocaleDateString(undefined, {weekday:'short', month:'short', day:'numeric'});
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 2400);
}

function setHeaderDate() {
  const iso = activeDate();
  const d = iso ? new Date(iso + 'T12:00:00') : new Date();
  document.getElementById('date').textContent =
    d.toLocaleDateString(undefined, {weekday:'long', month:'long', day:'numeric'});
}

// ── System + identity ──────────────────────────────────────────────────────
let systemSnapshot = null;  // stash for openSettings()

async function fetchSystem() {
  const r = await fetch('/api/system');
  const d = await r.json();
  systemSnapshot = d;
  watcherRunning = d.watcher_running;
  aiEnabled = d.ai_enabled;
  availableStreams = d.streams || [];
  todayISO = d.today;

  document.getElementById('pulse').className = 'pulse' + (watcherRunning ? '' : ' off');
  document.getElementById('foot-status').textContent =
    watcherRunning ? 'Watcher running' : 'Watcher stopped';
  document.getElementById('toggle-btn').textContent =
    watcherRunning ? 'Pause' : 'Start';

  const suffix = d.identity && d.identity.actor_label
    ? '— ' + d.identity.actor_label : '';
  document.getElementById('actor-suffix').textContent = suffix;

  // AI banner — tailored to what's actually missing
  const banner = document.getElementById('ai-banner');
  const detail = document.getElementById('ai-banner-detail');
  if (aiEnabled) {
    banner.style.display = 'none';
  } else {
    banner.style.display = 'flex';
    const llm = d.llm || {};
    if (llm.ollama && llm.ollama.installed && !llm.ollama.model_ready) {
      detail.innerHTML = `Ollama is running but model <code>${llm.ollama.want_model}</code> isn't pulled yet. Run: <code>ollama pull ${llm.ollama.want_model}</code>, or add an Anthropic key in Settings.`;
    } else {
      detail.textContent = 'Add an Anthropic API key in Settings, or install Ollama for free local AI.';
    }
  }

  document.getElementById('anthropic-status').className =
    'status-dot' + (d.secrets.anthropic_key.configured ? '' : ' off');
  document.getElementById('smtp-status').className =
    'status-dot' + (d.secrets.smtp_password.configured ? '' : ' off');

  // Soft onboarding gate: keep the streams banner up until real streams exist.
  const _sb = document.getElementById('streams-banner');
  if (_sb) _sb.style.display = (systemSnapshot && systemSnapshot.taxonomy_trivial) ? 'flex' : 'none';

  // First-run guided tour (falls back to the taxonomy nudge once seen).
  maybeStartTour();
}

async function toggleWatcher() {
  const ep = watcherRunning ? '/api/watcher/stop' : '/api/watcher/start';
  await fetch(ep, { method: 'POST' });
  await fetchSystem();
}

// ── Date navigation ────────────────────────────────────────────────────────
function renderDateBar() {
  const bar = document.getElementById('datebar');
  if (!todayISO) { bar.innerHTML = ''; return; }
  const yesterday = new Date(todayISO + 'T12:00:00');
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayISO = yesterday.toISOString().slice(0,10);

  const chip = (iso, label) => {
    const active = (iso === activeDate());
    return `<button class="date-chip ${active?'active':''}" onclick="selectDate('${iso}')">${label}</button>`;
  };
  bar.innerHTML =
    chip(todayISO, 'Today') +
    chip(yesterdayISO, 'Yesterday') +
    `<span class="date-chip ${currentDate && currentDate !== todayISO && currentDate !== yesterdayISO ? 'active' : ''}">
       <input type="date" max="${todayISO}" value="${currentDate || todayISO}"
              onchange="selectDate(this.value)" />
     </span>`;
}

function selectDate(iso) {
  if (!iso) return;
  currentDate = (iso === todayISO) ? null : iso;
  renderDateBar();
  setHeaderDate();
  refreshDayPanels();
}

async function fetchRealWork() {
  const url = '/api/realwork' + (currentDate ? '?date=' + currentDate : '');
  let d;
  try {
    const r = await fetch(url);
    d = await r.json();
  } catch (e) {
    document.getElementById('hero-headline').textContent = 'Could not load activity data.';
    return;
  }

  const whenLabel = isViewingToday() ? 'today' : 'on ' + relativeDateLabel(d.date).toLowerCase();
  const whenStart = isViewingToday() ? 'You did' : 'On ' + relativeDateLabel(d.date) + ', you did';

  // Empty-state hero
  if (d.no_data || !d.total_active_minutes) {
    document.getElementById('hero-headline').textContent =
      isViewingToday() ? 'Quiet day so far.' : `No tracked activity on ${relativeDateLabel(d.date)}.`;
    document.getElementById('hero-sub').textContent =
      isViewingToday() ? 'Once you start working in a tracked app, this view will fill up.'
                       : 'Either WorkPulse wasn\'t running, or no work happened.';
    document.getElementById('donut-panel').innerHTML = '<div class="empty">No focused activity.</div>';
    document.getElementById('attn-panel').innerHTML = '<div class="empty">Nothing to review.</div>';
    document.getElementById('apps-panel').innerHTML = '<div class="empty">No apps tracked.</div>';
    document.getElementById('sessions-panel').innerHTML = '<div class="empty">No sessions.</div>';
    renderTimeline([]);
    return;
  }

  // ── Hero ──────────────────────────────────────────────────────────────
  const total = fmtMins(d.total_active_minutes);
  const top = d.by_stream && d.by_stream[0];
  let head = isViewingToday()
    ? `You did <span class="strong">${total}</span> of focused work today`
    : `On <span class="strong">${relativeDateLabel(d.date)}</span> you did <span class="strong">${total}</span> of focused work`;
  if (top) head += `, mostly on <span class="strong">${TITLE_CASE(top.stream)}</span> (${fmtMins(top.minutes)}).`;
  else head += '.';
  document.getElementById('hero-headline').innerHTML = head;

  const projCount = (d.by_stream || []).length;
  const subBits = [];
  if (projCount) subBits.push(`${projCount} project${projCount===1?'':'s'} active`);
  if (d.untagged_minutes) subBits.push(`${fmtMins(d.untagged_minutes)} uncategorized`);
  else subBits.push('fully tagged');
  document.getElementById('hero-sub').textContent = subBits.join(' · ');

  // ── Donut + legend ────────────────────────────────────────────────────
  const streams = d.by_stream || [];
  let cum = 0;
  const slices = streams.length
    ? streams.map(s => {
        const a = cum, b = cum + s.pct; cum = b;
        return `${s.color} ${a}% ${b}%`;
      }).join(', ')
    : 'var(--border) 0% 100%';
  const legend = streams.length
    ? streams.map(s => `
        <div class="lg-row">
          <span class="lg-dot" style="background:${s.color}"></span>
          <span class="lg-name">${escapeHtml(s.stream.replace(/-/g,' '))}</span>
          <span class="lg-min">${fmtMins(s.minutes)}</span>
          <span class="lg-pct">${s.pct}%</span>
        </div>`).join('')
    : '<div class="empty">No tagged streams yet.</div>';
  document.getElementById('donut-panel').innerHTML = `
    <div class="donut-wrap">
      <div class="donut" style="background: conic-gradient(${slices})">
        <div class="donut-c">
          <div class="num">${total}</div>
          <div class="lbl">focused</div>
        </div>
      </div>
      <div class="legend">${legend}</div>
    </div>`;

  // ── Untagged-time alert (v1.7) ────────────────────────────────────────
  // Fires when today's untagged time crosses the threshold. Reuses the
  // same Tag-as dropdown UX as the lower "Needs your attention" panel but
  // makes the worst offenders impossible to miss.
  const UNTAGGED_ALERT_MIN = 20;
  const alertEl = document.getElementById('untagged-alert');
  const untaggedMin = d.untagged_minutes || 0;
  const topUntagged = (d.untagged_windows || []).filter(w => w.minutes >= 3).slice(0, 3);
  if (untaggedMin >= UNTAGGED_ALERT_MIN && topUntagged.length > 0) {
    document.getElementById('untagged-alert-text').textContent =
      `${fmtMins(untaggedMin)} untagged today — tag the patterns below so future activity rolls up.`;
    document.getElementById('untagged-alert-items').innerHTML = topUntagged.map((w, i) => `
      <div class="alert-row">
        <span class="alert-title" title="${escapeHtml(w.title)}">${escapeHtml(w.title)}</span>
        <span class="alert-min">${fmtMins(w.minutes)}</span>
        <div class="attn-tag">
          <button class="tag-btn" onclick="toggleTagMenu('alert-${i}')">Tag as ▾</button>
          <div class="tag-menu" id="tag-menu-alert-${i}">
            ${availableStreams.map(s => `
              <div class="tag-opt" onclick="learn('${escapeHtml(w.title)}', '${s.key}')">
                <span class="lg-dot" style="background:${s.color}"></span>
                <span>${escapeHtml(s.label)}</span>
              </div>`).join('')}
            <div class="tag-opt ignore" onclick="learn('${escapeHtml(w.title)}', null)">
              Never tag this (ignore)
            </div>
          </div>
        </div>
      </div>`).join('');
    alertEl.style.display = '';
  } else {
    alertEl.style.display = 'none';
  }

  // ── Needs your attention (with Loop A tag-as dropdown) ───────────────
  const attn = (d.untagged_windows || []).filter(w => w.minutes >= 1).slice(0, 10);
  if (attn.length === 0) {
    const msg = aiEnabled
      ? 'Nothing untagged worth reviewing. Auto-tagging is doing its job.'
      : 'Nothing untagged yet. As windows show up here, tag them once and WorkPulse remembers.';
    document.getElementById('attn-panel').innerHTML = `<div class="empty">${msg}</div>`;
  } else {
    document.getElementById('attn-panel').innerHTML = attn.map((w, i) => `
        <div class="attn-row">
          <div class="attn-title" title="${escapeHtml(w.title)}">${escapeHtml(w.title)}</div>
          <div class="attn-min">${fmtMins(w.minutes)}</div>
          <div class="attn-tag">
            <button class="tag-btn" onclick="toggleTagMenu(${i})">Tag as ▾</button>
            <div class="tag-menu" id="tag-menu-${i}">
              ${availableStreams.map(s => `
                <div class="tag-opt" onclick="learn('${escapeHtml(w.title)}', '${s.key}')">
                  <span class="lg-dot" style="background:${s.color}"></span>
                  <span>${escapeHtml(s.label)}</span>
                </div>`).join('')}
              <div class="tag-opt ignore" onclick="learn('${escapeHtml(w.title)}', null)">
                Never tag this (ignore)
              </div>
            </div>
          </div>
        </div>`).join('');
  }

  // ── Apps used (compact) ───────────────────────────────────────────────
  const apps = (d.by_app || []).slice(0, 8);
  if (apps.length === 0) {
    document.getElementById('apps-panel').innerHTML = '<div class="empty">No apps tracked yet.</div>';
  } else {
    const maxMin = apps[0].minutes || 1;
    document.getElementById('apps-panel').innerHTML = apps.map(a => `
      <div class="app-row">
        <div class="app-name">${escapeHtml(a.app)}</div>
        <div class="app-track"><div class="app-fill" style="width:${Math.round(a.minutes/maxMin*100)}%"></div></div>
        <div class="app-min">${fmtMins(a.minutes)}</div>
      </div>`).join('');
  }

  // ── Hidden detail: every window visit ─────────────────────────────────
  const sessions = d.recent_sessions || [];
  if (sessions.length === 0) {
    document.getElementById('sessions-panel').innerHTML = '<div class="empty">No sessions yet.</div>';
  } else {
    const rows = sessions.map(s => {
      const dur = s.duration_s < 60 ? `${Math.round(s.duration_s)}s` : `${s.duration_min}m`;
      const chip = s.stream
        ? `<span class="chip" style="background:${s.color}">${escapeHtml(s.stream.replace(/-/g,' '))}</span>`
        : '<span class="chip muted">untagged</span>';
      return `<tr>
        <td class="mono">${s.start}</td>
        <td class="mono">${dur}</td>
        <td>${chip}</td>
        <td class="mono">${escapeHtml(s.app)}</td>
        <td class="title-cell" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</td>
      </tr>`;
    }).join('');
    document.getElementById('sessions-panel').innerHTML = `
      <table class="session-table">
        <thead><tr><th>Start</th><th>Duration</th><th>Project</th><th>App</th><th>Window</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // ── Timeline (24-hour strip) ──────────────────────────────────────────
  renderTimeline(sessions);
}

function renderTimeline(sessions) {
  // Bucket by hour-of-day. Color each hour by its dominant stream.
  const buckets = Array.from({length:24}, () => ({total:0, byStream:{}, color:null}));
  sessions.forEach(s => {
    if (!s.start || !s.duration_s) return;
    const hh = parseInt(s.start.slice(0,2), 10);
    if (isNaN(hh) || hh < 0 || hh > 23) return;
    const m = s.duration_s / 60;
    buckets[hh].total += m;
    if (s.stream) {
      const cur = buckets[hh].byStream[s.stream] || {min:0, color:s.color};
      cur.min += m;
      buckets[hh].byStream[s.stream] = cur;
    }
  });
  buckets.forEach(b => {
    const top = Object.values(b.byStream).sort((a,b)=>b.min-a.min)[0];
    b.color = top ? top.color : '#c8c5bb';
  });
  const maxMin = Math.max(1, ...buckets.map(b => b.total));
  const tl = document.getElementById('timeline');
  const ax = document.getElementById('tl-axis');
  tl.innerHTML = ''; ax.innerHTML = '';
  buckets.forEach((b, h) => {
    const hStr = h.toString().padStart(2,'0');
    if (b.total > 0) {
      const heightPct = Math.max(8, Math.round(b.total / maxMin * 100));
      const tip = `${hStr}:00 · ${fmtMins(b.total)}`;
      tl.insertAdjacentHTML('beforeend',
        `<div class="tl-h" title="${tip}"><div class="tl-bar" style="background:${b.color};height:${heightPct}%"></div></div>`);
    } else {
      tl.insertAdjacentHTML('beforeend',
        `<div class="tl-h" title="${hStr}:00"><div class="tl-empty"></div></div>`);
    }
    ax.insertAdjacentHTML('beforeend',
      `<div class="tl-tick">${h % 3 === 0 ? hStr : ''}</div>`);
  });
}

async function fetchLastActive() {
  const url = '/api/focus' + (currentDate ? '?date=' + currentDate : '');
  let d;
  try {
    const r = await fetch(url);
    d = await r.json();
  } catch (e) {
    document.getElementById('last-active').innerHTML = '<div class="empty">Unavailable.</div>';
    return;
  }
  const list = (d.last_touched || []).filter(s => s.last_ago);
  const emptyMsg = isViewingToday()
    ? 'No project files touched yet today.'
    : 'No file activity on this day.';
  document.getElementById('last-active').innerHTML = list.length === 0
    ? `<div class="empty">${emptyMsg}</div>`
    : list.map(s => `
        <div class="la-row">
          <span class="lg-dot" style="background:${s.color}"></span>
          <span class="la-name">${escapeHtml(s.label)}</span>
          <span class="la-ago">${escapeHtml(s.last_ago)}</span>
        </div>`).join('');
}

async function fetchAI() {
  const url = '/api/ai' + (currentDate ? '?date=' + currentDate : '');
  let d;
  try {
    const r = await fetch(url);
    d = await r.json();
  } catch (e) {
    document.getElementById('ai-panel').innerHTML = '<div class="empty">Unavailable.</div>';
    return;
  }
  if (!d.count) {
    document.getElementById('ai-panel').innerHTML =
      '<div class="empty">No AI sessions logged for this day.</div>';
    return;
  }
  document.getElementById('ai-panel').innerHTML = `
    <div class="ai-strip">
      <div class="ai-stat"><div class="num">${d.count}</div><div class="lbl">sessions</div></div>
      <div class="ai-stat"><div class="num">$${d.total_cost.toFixed(2)}</div><div class="lbl">spent</div></div>
      <div class="ai-stat"><div class="num">${(d.total_tokens/1000).toFixed(1)}k</div><div class="lbl">tokens</div></div>
    </div>`;
}

// ── Jobs in flight (v1.1a Coach surface) ───────────────────────────────────

function fmtRelative(iso) {
  if (!iso) return '';
  try {
    const t = new Date(iso);
    const diffMin = Math.floor((Date.now() - t.getTime()) / 60000);
    if (diffMin < 2) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return `${diffH}h ago`;
    const diffD = Math.floor(diffH / 24);
    if (diffD === 1) return 'yesterday';
    if (diffD < 7) return `${diffD} days ago`;
    return t.toLocaleDateString(undefined, {month:'short', day:'numeric'});
  } catch (e) { return ''; }
}

// ── Taxonomy wizard (v1.7 Slice 2) ──────────────────────────────────────
// Build / edit the stream hierarchy on the dashboard. Auto-opens once per
// browser session while the tree is "trivial" (per /api/system). After
// closing, the user can re-open from Settings.

let taxonomyOpen = false;

function maybeAutoOpenTaxonomy() {
  // Only auto-open once per session, and only if the tree is still trivial.
  if (taxonomyOpen) return;
  if (sessionStorage.getItem('wp.taxWizardSeen')) return;
  if (!systemSnapshot || !systemSnapshot.taxonomy_trivial) return;
  openTaxonomy('first-load');
}

function openTaxonomy(reason) {
  taxonomyOpen = true;
  document.getElementById('tx-modal').classList.add('open');
  document.getElementById('tx-title').textContent =
    reason === 'first-load' ? 'Set up your taxonomy' : 'Edit your taxonomy';
  // Show quick-start chips only on the first-time setup
  document.getElementById('tx-quickstart').style.display =
    (systemSnapshot && systemSnapshot.taxonomy_trivial) ? '' : 'none';
  renderTaxonomyTree();
}

function closeTaxonomy() {
  taxonomyOpen = false;
  document.getElementById('tx-modal').classList.remove('open');
  sessionStorage.setItem('wp.taxWizardSeen', '1');
}

async function finishTaxonomy() {
  // Streams are persisted as the user edits the tree; just close and
  // re-sync the dashboard so the new taxonomy takes effect everywhere.
  closeTaxonomy();
  await fetchSystem();
  await refreshDayPanels();
  showToast('Taxonomy saved.');
}

async function txQuickAdd(key, label) {
  try {
    const r = await fetch('/api/streams', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ key, label })
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not add'); return; }
    await fetchSystem();
    renderTaxonomyTree();
  } catch (e) { showToast('Error: ' + e.message); }
}

function renderTaxonomyTree() {
  const streams = (systemSnapshot && systemSnapshot.streams) || [];
  // Build adjacency: parent → [children]
  const byParent = {};
  streams.forEach(s => {
    const p = s.parent || '__root__';
    (byParent[p] = byParent[p] || []).push(s);
  });
  function renderLevel(parentKey) {
    const kids = byParent[parentKey] || [];
    if (kids.length === 0) return '';
    kids.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
    return `<ul class="tx-tree-list">${kids.map(s => `
      <li>
        <div class="tx-node" id="tx-node-${escapeHtml(s.key)}">
          <span class="tx-node-color" style="background:${s.color}"></span>
          <span class="tx-node-label">${escapeHtml(s.label)}</span>
          <span class="tx-node-key">${escapeHtml(s.key)}</span>
          ${s.recognize
            ? `<span class="tx-node-recog" title="How WorkPulse auto-tags this stream">⌖ ${escapeHtml(s.recognize)}</span>`
            : `<span class="tx-node-recog none" title="No auto-tag hint — add one to tag this stream in real time">no auto-tag</span>`}
          <div class="tx-node-actions">
            <button class="tx-icon-btn" onclick="txBeginRename('${escapeHtml(s.key)}')">edit</button>
            <button class="tx-icon-btn" onclick="txShowAddChild('${escapeHtml(s.key)}')">+ child</button>
            <button class="tx-icon-btn danger" onclick="txDelete('${escapeHtml(s.key)}','${escapeHtml(s.label)}')">×</button>
          </div>
        </div>
        <div id="tx-form-${escapeHtml(s.key)}"></div>
        ${renderLevel(s.key)}
      </li>`).join('')}
    </ul>`;
  }
  const tree = document.getElementById('tx-tree');
  const rootHTML = renderLevel('__root__');
  if (!rootHTML) {
    tree.innerHTML = `
      <div class="empty" style="text-align:left">
        No streams yet — use the quick-start chips above or
        <button class="tx-icon-btn" onclick="txShowAddChild('')" style="text-decoration:underline">+ add a top-level domain</button>.
      </div>
      <div id="tx-form-"></div>`;
    return;
  }
  tree.innerHTML = `
    ${rootHTML}
    <div style="margin-top:12px"><button class="tx-icon-btn" onclick="txShowAddChild('')">+ add another top-level domain</button></div>
    <div id="tx-form-"></div>`;
}

function txShowAddChild(parentKey) {
  // Replace the form slot under this node with an inline form.
  const slot = document.getElementById(`tx-form-${parentKey}`);
  if (!slot) return;
  slot.innerHTML = `
    <div class="tx-add-form">
      <div class="tx-add-row">
        <input type="text" placeholder="key (e.g. acme-web)" id="tx-new-key-${parentKey}" />
        <input type="text" placeholder="label (e.g. Acme Web)" id="tx-new-label-${parentKey}" />
      </div>
      <input type="text" class="tx-recog-input"
             placeholder="recognize by: a folder or keyword (optional)" id="tx-new-recog-${parentKey}" />
      <div class="tx-recog-hint">A folder (<code>~/Clients/Acme</code>) or a keyword (<code>acme</code>)
        so WorkPulse can auto-tag this stream from day one.</div>
      <div class="tx-add-actions">
        <button onclick="txAddChild('${parentKey}')" class="primary">Add</button>
        <button onclick="txCancelAdd('${parentKey}')">Cancel</button>
      </div>
    </div>`;
  setTimeout(() => document.getElementById(`tx-new-key-${parentKey}`).focus(), 30);
}

function txCancelAdd(parentKey) {
  const slot = document.getElementById(`tx-form-${parentKey}`);
  if (slot) slot.innerHTML = '';
}

async function txAddChild(parentKey) {
  const key   = document.getElementById(`tx-new-key-${parentKey}`).value.trim().toLowerCase();
  const label = document.getElementById(`tx-new-label-${parentKey}`).value.trim();
  const recog = document.getElementById(`tx-new-recog-${parentKey}`).value.trim();
  if (!key || !label) { showToast('Both key and label are required.'); return; }
  try {
    const body = { key, label };
    if (parentKey) body.parent = parentKey;
    if (recog) body.recognize = recog;
    const r = await fetch('/api/streams', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not add'); return; }
    await fetchSystem();
    renderTaxonomyTree();
  } catch (e) { showToast('Error: ' + e.message); }
}

function txBeginRename(key) {
  const node = document.getElementById(`tx-node-${key}`);
  if (!node) return;
  const s = ((systemSnapshot && systemSnapshot.streams) || []).find(x => x.key === key) || {};
  // Edit label + recognize-by together. Cancel/save rebuild the whole tree.
  node.innerHTML = `
    <div class="tx-edit-form">
      <input type="text" id="tx-rename-${key}" value="${escapeHtml(s.label || '')}" placeholder="label" />
      <input type="text" class="tx-recog-input" id="tx-recog-${key}" value="${escapeHtml(s.recognize || '')}"
             placeholder="recognize by: a folder or keyword (optional)" />
      <div class="tx-add-actions">
        <button class="tx-icon-btn primary" onclick="txCommitRename('${key}')">save</button>
        <button class="tx-icon-btn" onclick="renderTaxonomyTree()">cancel</button>
      </div>
    </div>`;
  setTimeout(() => {
    const inp = document.getElementById(`tx-rename-${key}`);
    if (inp) { inp.focus(); inp.select(); }
  }, 30);
}

async function txCommitRename(key) {
  const label = document.getElementById(`tx-rename-${key}`).value.trim();
  const recog = document.getElementById(`tx-recog-${key}`).value.trim();
  if (!label) { showToast('Label cannot be empty.'); return; }
  try {
    const r = await fetch(`/api/streams/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ label, recognize: recog })
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not save'); renderTaxonomyTree(); return; }
    await fetchSystem();
    renderTaxonomyTree();
  } catch (e) { showToast('Error: ' + e.message); renderTaxonomyTree(); }
}

async function txDelete(key, label) {
  try {
    // Phase 1: ask the server what deleting would cost (this does NOT delete).
    const pr = await fetch(`/api/streams/${encodeURIComponent(key)}`, { method: 'DELETE' });
    const pd = await pr.json();
    if (!pr.ok) { showToast(pd.error || 'could not delete'); return; }   // children / not found
    if (!pd.needs_confirm) {                                            // nothing to confirm
      await fetchSystem(); renderTaxonomyTree(); showToast(`Deleted "${label}".`); return;
    }
    const im = pd.impact || {};
    const body = im.sessions
      ? `${im.sessions} session${im.sessions === 1 ? '' : 's'} (${im.hours}h across `
        + `${im.active_days} day${im.active_days === 1 ? '' : 's'}) is tagged to this stream. `
        + `Deleting it untags that work; it reverts to unclassified.`
      : `No tracked work is tagged to this stream.`;
    if (!confirm(`Delete "${label}"?\n\n${body}\n\nThis cannot be undone.`)) return;
    // Phase 2: confirmed — actually delete.
    const r = await fetch(`/api/streams/${encodeURIComponent(key)}?confirm=true`, { method: 'DELETE' });
    const d = await r.json();
    if (!d.ok) { showToast('Error: ' + (d.error || 'could not delete')); return; }
    await fetchSystem();
    renderTaxonomyTree();
    const n = d.untagged_sessions || 0;
    showToast(`Deleted "${label}".` +
      (n ? ` ${n} window${n === 1 ? '' : 's'} reverted to unclassified.` : ''));
  } catch (e) { showToast('Error: ' + e.message); }
}


// ── Today's plan (Morning Plan — v1.7 intent capture) ─────────────────────

// ── Health banner: surface the doctor's verdict ────────────────────────────
let healthState = null;

async function fetchHealth() {
  try {
    const r = await fetch('/api/v2/health');
    healthState = await r.json();
  } catch (e) {
    healthState = {verdict: 'unknown', summary: 'Health check unavailable.', checks: []};
  }
  updateHealthDot(healthState);
}

function updateHealthDot(d) {
  const dot = document.getElementById('health-dot');
  const btn = document.getElementById('health-indicator');
  if (!dot || !btn) return;
  const v = (d && d.verdict) || 'unknown';
  if (v === 'ok') {
    dot.textContent = '●'; dot.style.color = '#16a34a';
    btn.title = 'All systems healthy';
  } else if (v === 'fail') {
    dot.textContent = '▲'; dot.style.color = '#dc2626';
    btn.title = "Something's wrong, click for details";
  } else if (v === 'warn') {
    dot.textContent = '▲'; dot.style.color = '#d97706';
    btn.title = 'Needs a look, click for details';
  } else {
    dot.textContent = '●'; dot.style.color = 'var(--text-faint)';
    btn.title = 'Health status unknown';
  }
}

function openHealthModal(ev) {
  if (ev) ev.stopPropagation();
  document.getElementById('health-modal').style.display = 'flex';
  renderHealthModal();
}
function closeHealthModal() {
  document.getElementById('health-modal').style.display = 'none';
}

function renderHealthModal() {
  const body = document.getElementById('health-modal-body');
  const title = document.getElementById('health-modal-title');
  const d = healthState || {verdict: 'unknown', checks: []};
  const verdictLabel = {ok: 'All systems healthy', warn: 'Needs a look',
                        fail: "Something's wrong", unknown: 'Status unknown'}[d.verdict] || 'Status unknown';
  const verdictColor = {ok: '#16a34a', warn: '#d97706', fail: '#dc2626',
                        unknown: 'var(--text-soft)'}[d.verdict] || 'var(--text-soft)';
  if (title) title.innerHTML = `System health <span style="color:${verdictColor}; font-weight:500;">· ${verdictLabel}</span>`;

  if (!d.checks || !d.checks.length) {
    body.innerHTML = '<div class="empty">No health check has run yet. The doctor runs every 3 hours.</div>';
    return;
  }
  const iconFor = (s) => s === 'ok'
    ? '<span style="color:#16a34a;">●</span>'
    : (s === 'fail' ? '<span style="color:#dc2626;">▲</span>'
                    : '<span style="color:#d97706;">▲</span>');
  const nice = (k) => ({
    sensors_running: 'Sensors running',
    sensor_liveness: 'Tracker is live',
    sensor_not_stuck: 'Tracking varied apps',
    agents_healthy: 'Background agents',
    data_fresh: 'Recent file activity',
    nightly_ran: 'Nightly summary',
  })[k] || k;
  let html = '<div style="display:flex; flex-direction:column; gap:2px;">';
  d.checks.forEach(c => {
    html += `<div style="display:flex; gap:10px; padding:8px 4px; border-bottom:1px solid var(--border, #eee5d2);">
      <div style="width:16px; text-align:center; flex-shrink:0;">${iconFor(c.status)}</div>
      <div>
        <div style="font-weight:500; font-size:14px;">${escapeHtml(nice(c.check))}</div>
        <div style="font-size:12px; color:var(--text-soft); margin-top:2px;">${escapeHtml(c.message)}</div>
      </div>
    </div>`;
  });
  html += '</div>';
  if (d.ts) {
    html += `<div style="font-size:11px; color:var(--text-faint); margin-top:10px;">Last checked ${new Date(d.ts).toLocaleString()}</div>`;
  }
  body.innerHTML = html;
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const m = document.getElementById('health-modal');
    if (m && m.style.display === 'flex') closeHealthModal();
  }
});
document.addEventListener('click', function(e) {
  const m = document.getElementById('health-modal');
  if (m && m.style.display === 'flex' && e.target === m) closeHealthModal();
});

// ── UI cleanup: collapsible cards + View menu (show/hide per card) ─────────

// Per-card UX defaults. Cards not listed default to collapsed=false, visible=true.
const CARD_DEFAULTS = {
  today:       { collapsed: false, visible: true },
  profile:     { collapsed: true,  visible: true },   // summary only by default
  plan:        { collapsed: true,  visible: true },
  jobs:        { collapsed: true,  visible: true },
  heatmap:     { collapsed: true,  visible: true },
  donut:       { collapsed: true,  visible: true },
  'last-active': { collapsed: true, visible: true },
  timeline:    { collapsed: true,  visible: false },  // hidden by default
  attention:   { collapsed: true,  visible: true },
  apps:        { collapsed: true,  visible: false },
  ai:          { collapsed: true,  visible: false },
};

function getCardStates() {
  let s;
  try { s = JSON.parse(localStorage.getItem('wp_card_states') || '{}'); }
  catch (e) { s = {}; }
  return s;
}
function saveCardStates(s) {
  localStorage.setItem('wp_card_states', JSON.stringify(s));
}
function cardState(id) {
  const saved = getCardStates()[id];
  const def = CARD_DEFAULTS[id] || { collapsed: false, visible: true };
  return Object.assign({}, def, saved || {});
}

function toggleCard(id, ev) {
  if (ev) ev.stopPropagation();
  const el = document.querySelector(`[data-card="${id}"]`);
  if (!el) return;
  el.classList.toggle('is-collapsed');
  const states = getCardStates();
  states[id] = Object.assign({}, states[id] || {}, {
    collapsed: el.classList.contains('is-collapsed'),
  });
  saveCardStates(states);
}

function setCardVisibility(id, visible) {
  const el = document.querySelector(`[data-card="${id}"]`);
  if (el) el.classList.toggle('hidden', !visible);
  const states = getCardStates();
  states[id] = Object.assign({}, states[id] || {}, { visible: !!visible });
  saveCardStates(states);
}

function applyCardStates() {
  document.querySelectorAll('[data-card]').forEach(el => {
    const id = el.dataset.card;
    const s = cardState(id);
    el.classList.toggle('is-collapsed', !!s.collapsed);
    el.classList.toggle('hidden', s.visible === false);
  });
}

// View menu — dropdown with checkboxes per card
function openViewMenu(ev) {
  ev.stopPropagation();
  closeViewMenu();
  const cards = Array.from(document.querySelectorAll('[data-card]'));
  const menu = document.createElement('div');
  menu.className = 'view-menu';
  const rect = ev.target.getBoundingClientRect();
  menu.style.top  = (rect.bottom + 6) + 'px';
  menu.style.right = (window.innerWidth - rect.right) + 'px';

  let html = '<div style="padding:8px 14px; font-size:11px; color:var(--text-soft); text-transform:uppercase; letter-spacing:1.2px;">Show / hide cards</div>';
  cards.forEach(el => {
    const id = el.dataset.card;
    const label = el.dataset.cardLabel || id;
    const checked = cardState(id).visible !== false;
    html += `<label class="view-item">
      <input type="checkbox" ${checked ? 'checked' : ''}
             onchange="setCardVisibility('${id}', this.checked)" />
      <span>${escapeHtml(label)}</span>
    </label>`;
  });
  html += '<div class="view-sep"></div>';
  html += `<div class="view-action" onclick="expandAllCards()">Expand all</div>`;
  html += `<div class="view-action" onclick="collapseAllCards()">Collapse all</div>`;
  html += `<div class="view-action" onclick="resetCardLayout()">Reset to defaults</div>`;
  menu.innerHTML = html;
  document.body.appendChild(menu);

  setTimeout(() => {
    function onDocClick(e) {
      if (!menu.contains(e.target)) {
        closeViewMenu();
        document.removeEventListener('click', onDocClick);
      }
    }
    document.addEventListener('click', onDocClick);
  }, 50);
}
function closeViewMenu() {
  document.querySelectorAll('.view-menu').forEach(el => el.remove());
}
function expandAllCards() {
  document.querySelectorAll('[data-card]').forEach(el => el.classList.remove('is-collapsed'));
  const states = getCardStates();
  document.querySelectorAll('[data-card]').forEach(el => {
    const id = el.dataset.card;
    states[id] = Object.assign({}, states[id] || {}, { collapsed: false });
  });
  saveCardStates(states);
  closeViewMenu();
}
function collapseAllCards() {
  document.querySelectorAll('[data-card]').forEach(el => el.classList.add('is-collapsed'));
  const states = getCardStates();
  document.querySelectorAll('[data-card]').forEach(el => {
    const id = el.dataset.card;
    states[id] = Object.assign({}, states[id] || {}, { collapsed: true });
  });
  saveCardStates(states);
  closeViewMenu();
}
function resetCardLayout() {
  localStorage.removeItem('wp_card_states');
  applyCardStates();
  closeViewMenu();
}

// Apply card states on initial load
document.addEventListener('DOMContentLoaded', applyCardStates);
// Also apply right now in case DOMContentLoaded already fired
applyCardStates();

// ── v2 personal: small padlock in topbar + modal on click ──────────────────

let personalState = null;

async function fetchPersonal() {
  try {
    const r = await fetch('/api/v2/personal/status');
    personalState = await r.json();
    updatePersonalLockIcon(personalState);
  } catch (e) {
    // Silent — the padlock just stays in its locked state
  }
}

function updatePersonalLockIcon(s) {
  const btn = document.getElementById('personal-lock-btn');
  if (!btn || !s) return;
  if (s.unlocked) {
    btn.textContent = '🔓';
    btn.style.opacity = '1';
    btn.title = 'Personal (unlocked, click to lock)';
  } else if (s.password_set) {
    btn.textContent = '🔒';
    btn.style.opacity = '0.6';
    btn.title = 'Personal (locked, click to unlock)';
  } else {
    btn.textContent = '🔒';
    btn.style.opacity = '0.35';
    btn.title = 'Personal — click to set a password';
  }
}

function openPersonalModal() {
  // If already unlocked, fast-lock instead of opening the modal
  if (personalState && personalState.unlocked) {
    personalLock();
    return;
  }
  document.getElementById('personal-modal').style.display = 'flex';
  renderPersonalModal();
  // Focus the password input after render
  setTimeout(() => {
    const inp = document.getElementById('personal-pwd') ||
                document.getElementById('personal-new-pwd');
    if (inp) inp.focus();
  }, 50);
}

function closePersonalModal() {
  document.getElementById('personal-modal').style.display = 'none';
}

function renderPersonalModal() {
  const body = document.getElementById('personal-modal-body');
  const s = personalState || {password_set: false, unlocked: false};
  if (!s.password_set) {
    body.innerHTML = `
      <p style="font-size:13px; color:var(--text-soft); margin:0 0 12px 0;">
        Set a password to protect your personal browsing (banking, healthcare,
        personal email, social, etc.). Minimum 4 characters.
      </p>
      <form onsubmit="personalSetPassword(event); return false;" style="display:flex; gap:8px;">
        <input type="password" id="personal-new-pwd" placeholder="new password"
               style="flex:1; padding:8px 12px; border:1px solid var(--border, #ddd); border-radius:6px; font-size:14px;" />
        <button type="submit"
                style="padding:8px 14px; border:0; border-radius:6px; background:#6b7280; color:#fff; font-weight:600; cursor:pointer;">
          Set
        </button>
      </form>`;
    return;
  }
  if (!s.unlocked) {
    body.innerHTML = `
      <p style="font-size:13px; color:var(--text-soft); margin:0 0 12px 0;">
        Your personal browsing is hidden from the dashboard. Enter your password
        to view what's been visited today.
      </p>
      <form onsubmit="personalUnlock(event); return false;" style="display:flex; gap:8px;">
        <input type="password" id="personal-pwd" placeholder="password" autocomplete="current-password"
               style="flex:1; padding:8px 12px; border:1px solid var(--border, #ddd); border-radius:6px; font-size:14px;" />
        <button type="submit"
                style="padding:8px 14px; border:0; border-radius:6px; background:#6b7280; color:#fff; font-weight:600; cursor:pointer;">
          Unlock
        </button>
      </form>
      <div id="personal-err" style="margin-top:8px; font-size:12px; color:#dc2626;"></div>`;
    return;
  }
  // Unlocked — fetch the data
  body.innerHTML = '<div class="empty">Loading…</div>';
  fetch('/api/v2/personal/data').then(r => r.json()).then(d => {
    let html = '<div style="font-size:12px; color:var(--text-soft); margin-bottom:10px;">Auto-locks in 30 min · session-only</div>';
    if (d.domains_today && d.domains_today.length) {
      html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Private-tier domains today:</div>';
      html += '<ul style="margin:0 0 14px 18px; padding:0;">';
      d.domains_today.forEach(it => {
        html += `<li style="margin-bottom:3px;"><strong>${escapeHtml(it.domain)}</strong> · ${it.visits} visit${it.visits === 1 ? '' : 's'}</li>`;
      });
      html += '</ul>';
    } else {
      html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:14px;">No private-tier browsing recorded today.</div>';
    }
    html += `<button onclick="personalLock()" style="padding:6px 14px; border:0; border-radius:6px; background:var(--border, #ddd); color:var(--text); font-size:13px; cursor:pointer;">Lock now</button>`;
    body.innerHTML = html;
  });
}

async function personalSetPassword(ev) {
  if (ev) ev.preventDefault();
  const pwd = document.getElementById('personal-new-pwd').value;
  if (!pwd) return false;
  const r = await fetch('/api/v2/personal/set-password', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pwd}),
  });
  if (!r.ok) {
    const d = await r.json();
    alert('Failed: ' + (d.error || r.status));
    return false;
  }
  await fetchPersonal();
  renderPersonalModal();
  return false;
}

async function personalUnlock(ev) {
  if (ev) ev.preventDefault();
  const pwd = document.getElementById('personal-pwd').value;
  const err = document.getElementById('personal-err');
  if (!pwd) return false;
  const r = await fetch('/api/v2/personal/unlock', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pwd}),
  });
  if (!r.ok) {
    if (err) err.textContent = 'wrong password';
    return false;
  }
  await fetchPersonal();
  renderPersonalModal();
  if (typeof fetchToday === 'function') fetchToday();
  return false;
}

async function personalLock() {
  await fetch('/api/v2/personal/lock', {method: 'POST'});
  await fetchPersonal();
  closePersonalModal();
  if (typeof fetchToday === 'function') fetchToday();
}

// Close modal on Escape or click outside
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const m = document.getElementById('personal-modal');
    if (m && m.style.display === 'flex') closePersonalModal();
  }
});
document.addEventListener('click', function(e) {
  const m = document.getElementById('personal-modal');
  if (m && m.style.display === 'flex' && e.target === m) closePersonalModal();
});

// ── v2 capture bar: post → POST /api/v2/capture → refresh ──────────────────

async function submitCapture(ev) {
  if (ev) ev.preventDefault();
  const input = document.getElementById('capture-input');
  const status = document.getElementById('capture-status');
  const body = (input.value || '').trim();
  if (!body) return false;
  status.textContent = 'capturing…';
  status.style.color = 'var(--text-soft)';
  try {
    const r = await fetch('/api/v2/capture', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({body: body, pin: 'auto'}),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    input.value = '';
    const pinNote = d.pinned_kind ? ` (pinned to ${d.pinned_kind})` : '';
    status.textContent = '✓ captured' + pinNote;
    status.style.color = '#16a34a';
    // Refresh the day panels so the new capture surfaces immediately
    if (typeof fetchToday === 'function') fetchToday();
    setTimeout(() => { status.textContent = ''; }, 4000);
  } catch (e) {
    status.textContent = 'failed';
    status.style.color = '#dc2626';
  }
  return false;
}

// Keyboard shortcut: Cmd/Ctrl+Shift+K focuses the capture box
document.addEventListener('keydown', function(e) {
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    const el = document.getElementById('capture-input');
    if (el) el.focus();
  }
});

// ── v2 brain layer: "Who you are" card (the living profile, Step A) ────────

let profileState = null;

async function fetchProfile() {
  try {
    const r = await fetch('/api/v2/profile');
    if (!r.ok) throw new Error('http ' + r.status);
    profileState = await r.json();
    renderProfile(profileState);
  } catch (e) {
    document.getElementById('profile-panel').innerHTML =
      '<div class="empty">Could not load the profile.</div>';
  }
}

function renderProfile(d) {
  const panel = document.getElementById('profile-panel');
  const meta  = document.getElementById('profile-meta');
  const summary = document.getElementById('profile-summary');
  if (!d || !d.exists) {
    panel.innerHTML =
      `<div class="empty">No profile yet. Run <code>python -m workpulse.core.profile update</code>, or wait for tonight&rsquo;s dream cycle.</div>`;
    meta.textContent = '';
    if (summary) summary.textContent = 'No profile yet.';
    return;
  }
  const fm = d.frontmatter || {};
  const updated = fm.last_updated ? new Date(fm.last_updated).toLocaleString() : '';
  const window  = fm.window || '';
  const hours   = fm.total_tracked_hours;
  const bits = [];
  if (window) bits.push(window);
  if (hours !== undefined) bits.push(`${hours} h tracked`);
  if (updated) bits.push(`updated ${updated}`);
  meta.textContent = bits.join('  ·  ');

  // One-line summary for collapsed state: pull the first paragraph from
  // the Identity section, truncated.
  if (summary) {
    const body = d.body || '';
    const m = body.match(/##\s+Identity\s*\n+([^\n#]+)/);
    if (m) {
      let line = m[1].trim().replace(/\*\*/g, '');
      if (line.length > 180) line = line.slice(0, 180) + '…';
      summary.textContent = line;
    } else if (hours !== undefined) {
      summary.textContent = `${hours} h over ${window || 'recent days'}.`;
    } else {
      summary.textContent = '';
    }
  }

  // Render the markdown body. Light renderer: section headers + paragraphs +
  // bullet lists. Avoid pulling in a markdown library; the profile shape is
  // controlled and predictable.
  const body = d.body || '';
  panel.innerHTML = renderProfileMarkdown(body);
}

function renderProfileMarkdown(md) {
  // Split by ## headings into sections, render each
  const sections = [];
  let cur = { heading: '', lines: [] };
  md.split('\n').forEach(line => {
    const m = line.match(/^##\s+(.+?)\s*$/);
    if (m) {
      if (cur.heading || cur.lines.length) sections.push(cur);
      cur = { heading: m[1].trim(), lines: [] };
    } else if (line.match(/^#\s/)) {
      // ignore the title (already in the eyebrow)
    } else {
      cur.lines.push(line);
    }
  });
  if (cur.heading || cur.lines.length) sections.push(cur);

  let html = '<div style="display:flex; flex-direction:column; gap:14px;">';
  sections.forEach(s => {
    if (!s.heading && !s.lines.some(l => l.trim())) return;
    html += '<div>';
    if (s.heading) {
      html += `<div style="font-size:13px; color:var(--text-soft); font-weight:600; margin-bottom:6px;">${escapeHtml(s.heading)}</div>`;
    }
    // Inline render: bullets → ul; ### → bold; blank → paragraph
    let inUl = false;
    const out = [];
    const flushUl = () => { if (inUl) { out.push('</ul>'); inUl = false; } };
    s.lines.forEach(rawLine => {
      const line = rawLine.replace(/\s+$/, '');
      const sub = line.match(/^###\s+(.+?)$/);
      const bullet = line.match(/^[-*]\s+(.+)$/);
      if (sub) {
        flushUl();
        out.push(`<div style="font-weight:600; margin-top:6px;">${renderInline(sub[1])}</div>`);
      } else if (bullet) {
        if (!inUl) { out.push('<ul style="margin:0 0 0 18px; padding:0;">'); inUl = true; }
        out.push(`<li style="margin-bottom:3px;">${renderInline(bullet[1])}</li>`);
      } else if (line.trim() === '') {
        flushUl();
        out.push('');
      } else {
        flushUl();
        out.push(`<p style="margin:6px 0;">${renderInline(line)}</p>`);
      }
    });
    flushUl();
    html += out.filter(s => s !== undefined).join('\n');
    html += '</div>';
  });
  html += '</div>';
  return html;
}

function renderInline(text) {
  // **bold**, *italic*, `code`, basic safety
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="background:var(--bg-soft, #f5efe2); padding:1px 5px; border-radius:3px; font-size:90%;">$1</code>')
    .replace(/\[([0-9]{4}-[0-9]{2}-[0-9]{2})\]/g, '<span style="color:var(--text-faint); font-family:var(--font-mono,monospace); font-size:90%;">[$1]</span>');
}

// ── v2 brain view: "What you worked on today" card ──────────────────────────

let todayState = null;  // last response from GET /api/v2/today

async function fetchToday() {
  try {
    const r = await fetch('/api/v2/today');
    if (!r.ok) throw new Error('http ' + r.status);
    todayState = await r.json();
    renderToday(todayState);
  } catch (e) {
    document.getElementById('today-panel').innerHTML =
      '<div class="empty">Could not load the brain view. The v2 SQLite store may not be initialized yet. Run <code>python -m workpulse.core.db init</code> from the repo.</div>';
  }
}

function renderToday(d) {
  const panel = document.getElementById('today-panel');
  const meta  = document.getElementById('today-meta');
  const summary = document.getElementById('today-summary');
  if (!d) { panel.innerHTML = '<div class="empty">No data.</div>'; return; }
  meta.textContent = `${d.date} · ${d.total_hours.toFixed(1)} h tracked`;

  // One-line summary used when the card is collapsed
  if (summary) {
    if (d.total_hours < 0.05) {
      summary.textContent = `Nothing tracked yet today.`;
    } else {
      const top = (d.by_stream || []).slice(0, 3)
        .map(s => `${s.stream} ${s.hours.toFixed(1)}h`)
        .join(' · ');
      summary.textContent = top ? `${d.total_hours.toFixed(1)}h · ${top}` :
                                  `${d.total_hours.toFixed(1)}h tracked`;
    }
  }

  if (d.total_hours < 0.05) {
    panel.innerHTML =
      '<div class="empty">Nothing tracked yet today. Open something, or run <code>python -m workpulse.core.capture "thought" --auto-pin</code> to capture a note.</div>';
    return;
  }

  let html = '';

  // Stream bar: a single segmented bar showing per-stream hours
  if (d.by_stream && d.by_stream.length) {
    const total = d.total_hours || 0.001;
    html += '<div style="margin: 10px 0 16px 0;">';
    html += '<div style="display:flex; height:14px; border-radius:6px; overflow:hidden; background:var(--bg-soft, #f5efe2);">';
    d.by_stream.forEach((s, i) => {
      const pct = Math.max(2, Math.round((s.hours / total) * 100));
      const color = todayStreamColor(s.stream, i);
      html += `<div title="${escapeHtml(s.stream)}: ${s.hours.toFixed(1)} h" style="width:${pct}%; background:${color};"></div>`;
    });
    html += '</div>';
    html += '<div style="display:flex; flex-wrap:wrap; gap:14px; margin-top:8px; font-size:12px;">';
    d.by_stream.forEach((s, i) => {
      const color = todayStreamColor(s.stream, i);
      html += `<span style="display:inline-flex; align-items:center; gap:6px;"><span style="display:inline-block; width:10px; height:10px; background:${color}; border-radius:2px;"></span><strong>${escapeHtml(s.stream)}</strong> ${s.hours.toFixed(1)} h</span>`;
    });
    html += '</div></div>';
  }

  // Top named clusters — each with a side-channel context line (git + captures + skill runs)
  if (d.top_clusters && d.top_clusters.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Top clusters today</div>';
    html += '<div style="display:flex; flex-direction:column; gap:8px; margin-bottom:14px;">';
    d.top_clusters.slice(0, 5).forEach(c => {
      const name = c.name ? escapeHtml(c.name) : '<em style="color:var(--text-faint);">(unnamed cluster)</em>';
      const oneliner = c.one_liner ? ` — ${escapeHtml(c.one_liner)}` : '';

      // Fix 1.7: project assignment + confidence badge + correction dropdown
      let assignBadge = '';
      const label = c.assigned_label || c.assigned_stream || '—';
      const conf = c.assignment_confidence;
      const src = c.assignment_source;
      let badgeBg = '#e5e7eb';
      let badgeFg = '#374151';
      let confText = '';
      if (src === 'user') {
        badgeBg = '#dcfce7'; badgeFg = '#166534';
        confText = '· user';
      } else if (src === 'agent') {
        badgeBg = conf >= 0.7 ? '#dbeafe' : (conf >= 0.4 ? '#fef3c7' : '#fee2e2');
        badgeFg = conf >= 0.7 ? '#1e40af' : (conf >= 0.4 ? '#92400e' : '#991b1b');
        confText = '· ' + (conf * 100).toFixed(0) + '%';
      } else if (src === 'fallback') {
        confText = '· fallback';
      } else {
        confText = '· not assigned';
      }
      assignBadge = `<span style="display:inline-block; padding:2px 8px; background:${badgeBg}; color:${badgeFg}; border-radius:10px; font-size:11px; font-weight:600; cursor:pointer;" onclick="openCorrection('${escapeHtml(c.cluster_id)}', '${escapeHtml(c.assigned_stream || '')}', this)" title="Click to correct">${escapeHtml(label)} ${escapeHtml(confText)}</span>`;

      // Context line: "what was actually happening" — git + captures + brain calls
      let ctxLine = '';
      if (c.summary) {
        ctxLine = `<div style="margin-top:4px; font-size:12px; color:var(--text-soft);">↳ ${escapeHtml(c.summary)}</div>`;
      }
      // Top commit subjects (one-line each, max 3)
      let commitsLine = '';
      if (c.top_commits && c.top_commits.length) {
        commitsLine = '<ul style="margin:6px 0 0 18px; padding:0; font-size:12px; color:var(--text-soft);">';
        c.top_commits.forEach(g => {
          commitsLine += `<li><span style="font-family:var(--font-mono,monospace); color:var(--text-faint);">${escapeHtml(g.sha)}</span> ${escapeHtml(g.subject)}</li>`;
        });
        commitsLine += '</ul>';
      }

      html += `<div data-cluster="${escapeHtml(c.cluster_id)}" style="padding:10px 12px; background:var(--surface, #fff); border:1px solid var(--border, #eee5d2); border-radius:6px;">
        <div style="display:flex; align-items:baseline; justify-content:space-between; gap:12px;">
          <div><strong>${name}</strong>${oneliner}</div>
          <div style="text-align:right; white-space:nowrap;"><span style="font-variant-numeric:tabular-nums;">${c.hours.toFixed(1)} h</span> &nbsp;${assignBadge}</div>
        </div>
        ${ctxLine}
        ${commitsLine}
      </div>`;
    });
    html += '</div>';
  }

  // Plan vs actual (if any flagged items today)
  if (d.plan_vs_actual && d.plan_vs_actual.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Plan vs actual</div><ul style="margin:0 0 14px 18px; padding:0;">';
    d.plan_vs_actual.forEach(it => {
      const flagColor = (it.flag === 'overrun') ? '#d97706'
                       : (it.flag === 'underrun') ? '#dc2626'
                       : 'var(--text-soft)';
      html += `<li style="margin-bottom:4px;"><span style="color:${flagColor}; font-weight:600;">[${escapeHtml(it.flag)}]</span> ${escapeHtml(it.name)} — planned ${it.planned_min}m, actual ${it.actual_min}m</li>`;
    });
    html += '</ul>';
  }

  // Captures
  if (d.captures && d.captures.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Captures today</div>';
    html += '<ul style="margin:0 0 14px 18px; padding:0;">';
    d.captures.forEach(c => {
      const t = c.time ? `<span style="color:var(--text-faint); font-variant-numeric:tabular-nums; margin-right:8px;">${escapeHtml(c.time)}</span>` : '';
      const author = c.author === 'system' ? '<span style="color:var(--text-faint); font-size:11px; margin-left:6px;">(system)</span>' : '';
      html += `<li style="margin-bottom:4px;">${t}${escapeHtml(c.body)}${author}</li>`;
    });
    html += '</ul>';
  } else {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:14px;">No captures today. Run <code>python -m workpulse.core.capture "thought" --auto-pin</code> to add one.</div>';
  }

  // Untagged buckets — surface only the largest one as a one-liner
  if (d.untagged_buckets && d.untagged_buckets.length) {
    const top = d.untagged_buckets[0];
    if (top.minutes >= 5) {
      html += `<div style="font-size:12px; color:var(--text-soft); padding:6px 10px; background:var(--bg-warm, #fff6e0); border-radius:6px;">⚠️ ${top.minutes.toFixed(0)} min of untagged "${escapeHtml(top.sample_titles && top.sample_titles[0] || top.token)}" — want to tag it?</div>`;
    }
  }

  panel.innerHTML = html;
}

// ── Fix 1.7 correction UI: dropdown opens on badge click ────────────────────
let _streamsCache = null;
async function _getStreams() {
  if (_streamsCache) return _streamsCache;
  try {
    const r = await fetch('/api/v2/streams');
    const d = await r.json();
    _streamsCache = d.streams || [];
  } catch (e) {
    _streamsCache = [];
  }
  return _streamsCache;
}

async function openCorrection(clusterId, currentStream, anchor) {
  // Remove any existing popovers
  document.querySelectorAll('.wp-correction-popover').forEach(el => el.remove());
  const streams = await _getStreams();
  const rect = anchor.getBoundingClientRect();
  const pop = document.createElement('div');
  pop.className = 'wp-correction-popover';
  pop.style.cssText = `position:fixed; top:${rect.bottom + 6}px; left:${rect.left}px;
    background:var(--surface, #fff); border:1px solid var(--border, #ddd0b3);
    border-radius:8px; padding:8px; box-shadow:0 4px 20px rgba(0,0,0,0.15);
    z-index:9999; min-width:200px; max-height:300px; overflow:auto;`;
  let html = '<div style="font-size:11px; color:var(--text-soft); margin-bottom:6px; padding:0 4px;">Set this cluster to:</div>';
  streams.forEach(s => {
    const isCurrent = s.key === currentStream;
    html += `<div style="padding:6px 10px; cursor:pointer; border-radius:4px; ${isCurrent ? 'background:var(--bg-soft, #f5efe2); font-weight:600;' : ''}"
                onmouseover="this.style.background='var(--bg-warm, #fff6e0)'"
                onmouseout="this.style.background='${isCurrent ? 'var(--bg-soft, #f5efe2)' : 'transparent'}'"
                onclick="correctCluster('${clusterId}', '${s.key}', this)">
       <span style="font-weight:600;">${escapeHtml(s.label || s.key)}</span>
       <span style="color:var(--text-faint); font-size:11px; margin-left:6px;">${escapeHtml(s.key)}</span>
     </div>`;
  });
  pop.innerHTML = html;
  document.body.appendChild(pop);
  // Click outside to close
  setTimeout(() => {
    function onDocClick(e) {
      if (!pop.contains(e.target)) {
        pop.remove();
        document.removeEventListener('click', onDocClick);
      }
    }
    document.addEventListener('click', onDocClick);
  }, 50);
}

async function correctCluster(clusterId, toStream, anchor) {
  try {
    const r = await fetch(`/api/v2/cluster/${clusterId}/correct`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({to_stream: toStream}),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    // Close popover + refresh
    document.querySelectorAll('.wp-correction-popover').forEach(el => el.remove());
    if (typeof fetchToday === 'function') fetchToday();
  } catch (e) {
    alert('Correction failed: ' + e.message);
  }
}

// Stable color per stream (matches v1 tinting where possible)
function todayStreamColor(stream, i) {
  const palette = ['#3866d0', '#bc38d0', '#d08838', '#38d09a', '#d03866', '#7a7a7a'];
  if (stream === '<untagged>' || !stream) return '#bbb';
  // Deterministic hash → palette
  let h = 0;
  for (let k = 0; k < stream.length; k++) h = (h * 31 + stream.charCodeAt(k)) >>> 0;
  return palette[h % palette.length];
}

// ── Weekly heatmap (last 14 days) ──────────────────────────────────────────
async function fetchHeatmap() {
  let d;
  try {
    const r = await fetch('/api/calendar?days=14');
    d = await r.json();
  } catch (e) { return; }
  const days = d.days || [];
  if (days.length === 0) {
    document.getElementById('heatmap').innerHTML = '<div class="empty">No data yet.</div>';
    return;
  }
  const maxMin = Math.max(1, ...days.map(x => x.total_minutes));
  const activeISO = activeDate();
  document.getElementById('heatmap').innerHTML = days.map(day => {
    const top = day.by_stream && day.by_stream[0];
    const color = top ? top.color : 'var(--border)';
    const heightPct = day.total_minutes > 0
      ? Math.max(6, Math.round(day.total_minutes / maxMin * 100)) : 0;
    const dayLabel = (new Date(day.date + 'T12:00:00'))
      .toLocaleDateString(undefined, {day:'numeric'});
    const cls = 'hm-col' + (day.total_minutes === 0 ? ' empty' : '')
              + (day.date === activeISO ? ' selected' : '');
    const tip = `${day.weekday} ${day.date} — ${fmtMins(day.total_minutes)}`;
    return `<div class="${cls}" title="${tip}" onclick="selectDate('${day.date}')">
              <div class="hm-bar-wrap">
                <div class="hm-bar" style="background:${color};height:${heightPct}%"></div>
              </div>
              <div class="hm-day">${dayLabel}</div>
            </div>`;
  }).join('');
}

// ── Loop A: tag-as dropdown ────────────────────────────────────────────────
function toggleTagMenu(i) {
  // close all others first
  document.querySelectorAll('.tag-menu.open').forEach(m => {
    if (m.id !== 'tag-menu-' + i) m.classList.remove('open');
  });
  const m = document.getElementById('tag-menu-' + i);
  if (m) m.classList.toggle('open');
}

document.addEventListener('click', e => {
  if (!e.target.closest('.attn-tag')) {
    document.querySelectorAll('.tag-menu.open').forEach(m => m.classList.remove('open'));
  }
});

async function learn(rawTitle, streamKey) {
  try {
    const r = await fetch('/api/learn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ raw_title: rawTitle, stream: streamKey })
    });
    const d = await r.json();
    if (d.ok) {
      const n = d.retagged || 1;
      showToast(streamKey
        ? `Tagged as ${streamKey}. Attributed ${n} matching window${n === 1 ? '' : 's'}; WorkPulse remembers.`
        : `Will ignore this window in future.`);
      await fetchRealWork();
    } else {
      showToast('Error: ' + (d.error || 'could not save rule'));
    }
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// ── Settings modal ─────────────────────────────────────────────────────────
function openSettings() {
  document.getElementById('settings-modal').classList.add('open');
  fetch('/api/system').then(r => r.json()).then(d => {
    const cfg = d || {};
    // Backend summary block
    const llm = cfg.llm || {};
    const ol  = llm.ollama || {};
    const anth = (llm.anthropic || {});
    let lines = [];
    const active = cfg.ai_backend || 'none';
    const activeLabel = active === 'anthropic' ? 'Cloud (Anthropic Claude)'
                      : active === 'ollama'    ? `Local (Ollama, ${ol.want_model || ''})`
                      :                          'OFF — no backend configured';
    lines.push(`<div><strong>Active:</strong> ${activeLabel}</div>`);
    lines.push(`<div style="margin-top:6px"><strong>Anthropic:</strong> ${anth.available ? 'key configured' : 'not configured'}</div>`);
    if (ol.installed) {
      const m = (ol.models || []).length;
      lines.push(`<div><strong>Ollama:</strong> running, ${m} model${m===1?'':'s'} pulled${ol.model_ready ? ' (incl. '+ol.want_model+')' : ' (need '+ol.want_model+')'}</div>`);
      if (!ol.model_ready) {
        lines.push(`<div style="margin-top:4px;color:var(--text-faint)">Run <code>ollama pull ${ol.want_model}</code> to enable the local backend.</div>`);
      }
    } else {
      lines.push(`<div><strong>Ollama:</strong> not detected. <a href="https://ollama.com/download" target="_blank" rel="noopener" style="color:var(--text-soft)">Install Ollama →</a></div>`);
    }
    document.getElementById('backend-summary').innerHTML = lines.join('');
    document.getElementById('anthropic-status').className =
      'status-dot' + (cfg.secrets.anthropic_key.configured ? '' : ' off');
    document.getElementById('smtp-status').className =
      'status-dot' + (cfg.secrets.smtp_password.configured ? '' : ' off');
    document.getElementById('email-enabled').checked = !!cfg.email_enabled;
  });
}
function closeSettings() {
  document.getElementById('settings-modal').classList.remove('open');
  // Clear inputs so secrets aren't sitting in DOM
  document.getElementById('anthropic-input').value = '';
  document.getElementById('smtp-input').value = '';
}

async function saveSettings() {
  const anth = document.getElementById('anthropic-input').value.trim();
  const smtp = document.getElementById('smtp-input').value.trim();
  const smtpUser = document.getElementById('smtp-user-input').value.trim();
  const emailOn = document.getElementById('email-enabled').checked;

  const ops = [];
  if (anth) ops.push(fetch('/api/settings/secret', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name:'anthropic_key', value: anth })
  }));
  if (smtp) ops.push(fetch('/api/settings/secret', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name:'smtp_password', value: smtp })
  }));
  const emailPayload = { enabled: emailOn };
  if (smtpUser) { emailPayload.smtp_user = smtpUser; emailPayload.from_addr = smtpUser; emailPayload.to_addr = smtpUser; }
  ops.push(fetch('/api/settings/email', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(emailPayload)
  }));

  try {
    const results = await Promise.all(ops);
    const allOk = results.every(r => r.ok);
    if (allOk) {
      showToast('Settings saved.');
      closeSettings();
      await fetchSystem();
    } else {
      const errs = await Promise.all(results.map(r => r.ok ? null : r.json().catch(() => ({error:'unknown'}))));
      const firstErr = errs.find(e => e && e.error);
      showToast('Save failed: ' + (firstErr ? firstErr.error : 'unknown'));
    }
  } catch (e) {
    showToast('Save failed: ' + e.message);
  }
}

// ── Refresh orchestration ──────────────────────────────────────────────────
async function refreshDayPanels() {
  await Promise.all([fetchHealth(), fetchPersonal(), fetchProfile(), fetchToday(), fetchRealWork(), fetchLastActive(), fetchAI()]);
  await fetchHeatmap();   // re-render so selected day highlights
}

async function refresh() {
  await fetchSystem();
  if (todayISO) renderDateBar();
  setHeaderDate();
  await refreshDayPanels();
  document.getElementById('refresh-label').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 30000);


// ── First-run guided tour ───────────────────────────────────────────────────
// Coach-marks that spotlight each part of the dashboard and end by opening
// stream setup. Shown once (localStorage 'wp.tourDone'); replay via the Tour
// button. Steps target real element ids so the highlight tracks the layout.
const WP_TOUR_STEPS = [
  { title: "Welcome to WorkPulse",
    body: "WorkPulse quietly notices what you work on and files it under your projects. Here are the three things that matter — then we'll set up your projects together. Takes about a minute." },
  { target: "#capture-bar", title: "Say what you're working on",
    body: "Type one line, anytime. It sharpens how your time gets attributed — especially work that isn't tied to a file, like a call or planning." },
  { target: "#ask-card", title: "Ask it anything",
    body: "Once it knows your work, just ask. Try \"where is my client proposal?\" or \"what have I worked on this week?\" and it answers from your own activity, with the files as evidence. It works offline; add an API key or Ollama for richer answers." },
  { target: "#today-card", title: "Your day, by project",
    body: "Everything you did today, grouped by project. This is only as sharp as your project list — which is why the last step matters most." },
  { target: "#profile-card", title: "What WorkPulse learns",
    body: "Overnight it builds a short profile of how you actually work — your rhythms, your focus, and what's been neglected." },
  { target: "#health-indicator", title: "System health",
    body: "A green dot means the sensors are running and your data is fresh. If something silently breaks, it turns amber or red so you know." },
  { target: "#wp-settings-btn", title: "Set up your streams",
    body: "Your projects — we call them <b>streams</b> — live in Settings. Set them up now: without them, your time lands in a vague 'untagged' pile. Aim for the <b>8–12</b> areas you actually spend time on.",
    ctaLabel: "Set up my streams →", cta: true },
];
let wpTourIdx = 0;

function maybeStartTour() {
  if (window.__wpTourActive) return;              // don't restart mid-tour on refresh
  if (!systemSnapshot) return;
  if (!localStorage.getItem('wp.tourDone')) { startTour(); return; }
  maybeAutoOpenTaxonomy();                         // seen the tour already — old nudge
}

function startTour() {
  wpTourIdx = 0;
  window.__wpTourActive = true;
  let el = document.getElementById('wp-tour');
  if (!el) {
    el = document.createElement('div');
    el.id = 'wp-tour';
    el.innerHTML = '<div id="wp-tour-backdrop"></div>'
                 + '<div id="wp-tour-spot"></div><div id="wp-tour-tip"></div>';
    document.body.appendChild(el);
  }
  el.classList.add('open');
  window.addEventListener('resize', renderTourStep);
  renderTourStep();
}

function renderTourStep() {
  const step = WP_TOUR_STEPS[wpTourIdx];
  const spot = document.getElementById('wp-tour-spot');
  const tip  = document.getElementById('wp-tour-tip');
  if (!step || !tip) return;
  const isLast   = wpTourIdx === WP_TOUR_STEPS.length - 1;
  const dots     = WP_TOUR_STEPS.map((_, i) => `<i class="${i === wpTourIdx ? 'on' : ''}"></i>`).join('');
  const backBtn  = wpTourIdx > 0 ? `<button class="wp-tour-btn ghost" onclick="tourBack()">Back</button>` : '';
  const nextLbl  = step.cta ? step.ctaLabel : (isLast ? 'Finish' : 'Next');
  const nextCall = step.cta ? 'tourFinishToStreams()' : (isLast ? 'endTour(true)' : 'tourNext()');
  tip.innerHTML =
    `<div class="wp-tour-eyebrow">${wpTourIdx + 1} of ${WP_TOUR_STEPS.length}</div>`
  + `<div class="wp-tour-title">${step.title}</div>`
  + `<div class="wp-tour-body">${step.body}</div>`
  + `<div class="wp-tour-foot">`
  +   `<div class="wp-tour-dots">${dots}</div>`
  +   `<div class="wp-tour-btns">`
  +     `<button class="wp-tour-btn ghost" onclick="endTour(false)">Skip</button>`
  +     backBtn
  +     `<button class="wp-tour-btn primary" onclick="${nextCall}">${nextLbl}</button>`
  +   `</div>`
  + `</div>`;

  const target = step.target ? document.querySelector(step.target) : null;
  if (!target) {                                   // centered welcome / fallback
    spot.style.display = 'none';
    tip.classList.add('center');
    return;
  }
  target.scrollIntoView({ block: 'center', inline: 'nearest' });
  requestAnimationFrame(() => {
    const r = target.getBoundingClientRect();
    spot.style.display = 'block';
    spot.style.left   = (r.left - 6) + 'px';
    spot.style.top    = (r.top - 6) + 'px';
    spot.style.width  = (r.width + 12) + 'px';
    spot.style.height = (r.height + 12) + 'px';
    tip.classList.remove('center');
    const th = tip.offsetHeight, tw = tip.offsetWidth;
    const vh = window.innerHeight, vw = window.innerWidth, gap = 14;
    // Prefer below the target; flip above if it would overflow.
    let top = r.bottom + gap;
    if (top + th > vh - 10) top = Math.max(10, r.top - gap - th);
    // Align left with the target, clamped to the viewport.
    let left = Math.min(Math.max(10, r.left), vw - tw - 10);
    tip.style.left = left + 'px';
    tip.style.top  = top + 'px';
  });
}

function tourNext() { if (wpTourIdx < WP_TOUR_STEPS.length - 1) { wpTourIdx++; renderTourStep(); } }
function tourBack() { if (wpTourIdx > 0) { wpTourIdx--; renderTourStep(); } }

function closeTourNow() {
  localStorage.setItem('wp.tourDone', '1');
  window.__wpTourActive = false;
  window.removeEventListener('resize', renderTourStep);
  const el = document.getElementById('wp-tour');
  if (el) el.classList.remove('open');
}

function endTour(completed) {
  // Soft gate: if they try to skip while no streams exist yet, confirm once —
  // set-up is optional, but they should know their time stays untagged.
  if (!completed && systemSnapshot && systemSnapshot.taxonomy_trivial) {
    renderTourSkipConfirm();
    return;
  }
  closeTourNow();
}

function renderTourSkipConfirm() {
  const spot = document.getElementById('wp-tour-spot');
  const tip  = document.getElementById('wp-tour-tip');
  if (spot) spot.style.display = 'none';
  if (!tip) return;
  tip.classList.add('center');
  tip.innerHTML =
    `<div class="wp-tour-eyebrow">Before you go</div>`
  + `<div class="wp-tour-title">Set up streams later?</div>`
  + `<div class="wp-tour-body">You can skip this, but until you add your projects, everything you do is`
  + ` filed as <b>untagged</b> — the dashboard won't be able to tell you what you spent time on.</div>`
  + `<div class="wp-tour-foot">`
  +   `<span></span>`
  +   `<div class="wp-tour-btns">`
  +     `<button class="wp-tour-btn ghost" onclick="closeTourNow()">Skip anyway</button>`
  +     `<button class="wp-tour-btn primary" onclick="tourFinishToStreams()">Set up my streams →</button>`
  +   `</div>`
  + `</div>`;
}

function tourFinishToStreams() {
  closeTourNow();
  openTaxonomy('first-load');
}

// ── Ask WorkPulse ────────────────────────────────────────────────────────────

function askBackendLabel(b) {
  if (b === 'anthropic') return 'Hosted · Claude';
  if (b === 'ollama') return 'Local · Ollama';
  return 'Manual';
}

// Minimal, safe markdown → HTML: escape first, then apply a small subset.
function renderMarkdownLite(md) {
  let h = escapeHtml(md || '');
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  const lines = h.split('\n');
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const ln of lines) {
    if (/^###\s+/.test(ln))      { closeList(); out.push('<h4>' + ln.replace(/^###\s+/, '') + '</h4>'); }
    else if (/^##\s+/.test(ln))  { closeList(); out.push('<h3>' + ln.replace(/^##\s+/, '') + '</h3>'); }
    else if (/^#\s+/.test(ln))   { closeList(); out.push('<h2>' + ln.replace(/^#\s+/, '') + '</h2>'); }
    else if (/^\s*[-*]\s+/.test(ln))   { if (!inList) { out.push('<ul>'); inList = true; } out.push('<li>' + ln.replace(/^\s*[-*]\s+/, '') + '</li>'); }
    else if (/^\s*\d+\.\s+/.test(ln))  { if (!inList) { out.push('<ul>'); inList = true; } out.push('<li>' + ln.replace(/^\s*\d+\.\s+/, '') + '</li>'); }
    else if (ln.trim() === '')   { closeList(); }
    else                          { closeList(); out.push('<p>' + ln + '</p>'); }
  }
  closeList();
  return out.join('');
}

async function submitAsk(event) {
  event.preventDefault();
  const input = document.getElementById('ask-input');
  const q = (input.value || '').trim();
  if (!q) return false;
  const result = document.getElementById('ask-result');
  const answer = document.getElementById('ask-answer');
  const evidence = document.getElementById('ask-evidence');
  const badge = document.getElementById('ask-backend');
  result.style.display = 'block';
  answer.innerHTML = '<div class="empty">Thinking…</div>';
  evidence.innerHTML = '';
  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q }),
    });
    const data = await r.json();
    badge.textContent = askBackendLabel(data.backend);
    if (r.status === 401 && data.locked) {
      answer.innerHTML = renderMarkdownLite(data.answer) +
        '<p><button class="ask-btn" onclick="openPersonalModal()">Unlock</button></p>';
      return false;
    }
    if (!r.ok) {
      answer.innerHTML = '<div class="empty">' +
        escapeHtml(data.error || 'Something went wrong.') + '</div>';
      return false;
    }
    answer.innerHTML = renderMarkdownLite(data.answer);
    const ev = data.evidence || [];
    if (ev.length) {
      evidence.innerHTML = ev.map(function (e) {
        if (e.type === 'work') {
          const h = e.hours ? ' · ' + e.hours + 'h' : '';
          return '<span class="ask-chip">' + escapeHtml((e.name || 'work') + h) + '</span>';
        }
        if (e.type === 'file') {
          return '<span class="ask-chip" title="' + escapeHtml(e.path || '') + '">' +
                 escapeHtml(e.basename || e.path || 'file') + '</span>';
        }
        if (e.type === 'atom') {
          return '<span class="ask-chip">' + escapeHtml(e.atom_kind || 'atom') + '</span>';
        }
        return '';
      }).join('');
    }
  } catch (err) {
    answer.innerHTML = '<div class="empty">Could not reach WorkPulse.</div>';
  }
  return false;
}
