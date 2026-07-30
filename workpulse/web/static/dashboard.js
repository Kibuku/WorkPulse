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

function jsAttr(s) {
  // Safe as an argument inside  onclick="f('...')"  — escape for a JS
  // single-quoted string first (backslash, then quote), then HTML-encode the
  // characters that would break the attribute. Meeting titles carry '|', '&',
  // and sometimes apostrophes, which plain escapeHtml would mangle here.
  return (s || '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
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
let availableUpdate = null;

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
  checkForUpdate();
}

async function checkForUpdate() {
  try {
    const r = await fetch('/api/update');
    const d = await r.json();
    availableUpdate = d.update_available ? d : null;
    const btn = document.getElementById('update-available-btn');
    if (btn) btn.style.display = availableUpdate ? 'inline-flex' : 'none';
  } catch (_) {
    // Updating is optional and must never make the local dashboard unhealthy.
  }
}

function openUpdateModal() {
  if (!availableUpdate) return;
  document.getElementById('update-title').textContent =
    `WorkPulse ${availableUpdate.latest_version} is available`;
  document.getElementById('update-description').textContent =
    `You have ${availableUpdate.current_version}. Your work history, private memory and settings stay on this device.`;
  document.getElementById('update-notes').textContent =
    availableUpdate.notes || 'This update includes reliability and experience improvements.';
  document.getElementById('update-modal').style.display = 'flex';
}

function closeUpdateModal() {
  document.getElementById('update-modal').style.display = 'none';
}

async function installUpdate() {
  const btn = document.getElementById('install-update-btn');
  btn.disabled = true;
  btn.textContent = 'Downloading and verifying...';
  try {
    const r = await fetch('/api/update/install', {
      method: 'POST',
      headers: {'X-WorkPulse-Action': 'install-update'}
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'The update could not be prepared.');
    if (d.launched) {
      btn.textContent = 'Installer opened';
      showToast('Approve the WorkPulse installer to finish updating.');
      setTimeout(closeUpdateModal, 1600);
    } else {
      btn.textContent = 'Already up to date';
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Try again';
    showToast(err.message || 'Update failed. Try again.');
  }
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
    if (todayState && todayState.total_hours >= 0.05) {
      renderHeroFromToday(todayState);
      return;
    }
    document.getElementById('hero-headline').textContent =
      isViewingToday() ? 'Quiet day so far.' : `No tracked activity on ${relativeDateLabel(d.date)}.`;
    document.getElementById('hero-sub').textContent =
      isViewingToday() ? 'Once you start working in a tracked app, this view will fill up.'
                       : 'Either WorkPulse wasn\'t running, or no work happened.';
    document.getElementById('donut-panel').innerHTML = '<div class="empty">No focused activity.</div>';
    document.getElementById('attn-panel').innerHTML = '<div class="empty">Nothing to review.</div>';
    document.getElementById('apps-panel').innerHTML = '<div class="empty">No apps tracked.</div>';
    document.getElementById('sessions-panel').innerHTML = '<div class="empty">No sessions.</div>';
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

}

function renderHeroFromToday(d) {
  const total = `${d.total_hours.toFixed(1)}h`;
  const known = (d.by_stream || []).filter(s => s.stream !== '<untagged>');
  const unknown = (d.by_stream || []).find(s => s.stream === '<untagged>');
  const top = known[0];
  const labelFor = key => todayStreamLabel(key);

  let headline = `WorkPulse found <span class="strong">${total}</span> of activity today`;
  if (top) headline += `, mostly connected to <span class="strong">${escapeHtml(labelFor(top.stream))}</span>.`;
  else headline += '.';
  document.getElementById('hero-headline').innerHTML = headline;

  const sub = [];
  if (known.length) sub.push(`${known.length} output area${known.length === 1 ? '' : 's'} recognised`);
  if (unknown && unknown.hours > 0) sub.push(`${unknown.hours.toFixed(1)}h needs your review`);
  else sub.push('all substantial activity attributed');
  document.getElementById('hero-sub').textContent = sub.join(' · ');

  const streams = (d.by_stream || []).filter(s => s.hours > 0);
  let cumulative = 0;
  const slices = streams.map((s, i) => {
    const pct = (s.hours / d.total_hours) * 100;
    const from = cumulative;
    cumulative += pct;
    const color = s.stream === '<untagged>' ? '#d7dee9' : todayStreamColor(s.stream, i);
    return `${color} ${from}% ${cumulative}%`;
  }).join(', ');
  const legend = streams.map((s, i) => {
    const color = s.stream === '<untagged>' ? '#d7dee9' : todayStreamColor(s.stream, i);
    const label = s.stream === '<untagged>' ? 'Needs review' : labelFor(s.stream);
    return `<div class="lg-row">
      <span class="lg-dot" style="background:${color}"></span>
      <span class="lg-name">${escapeHtml(label)}</span>
      <span class="lg-min">${s.hours.toFixed(1)}h</span>
    </div>`;
  }).join('');
  document.getElementById('donut-panel').innerHTML = `
    <div class="donut-wrap">
      <div class="donut" style="background:conic-gradient(${slices || '#d7dee9 0% 100%'})">
        <div class="donut-c"><div class="num">${total}</div><div class="lbl">understood</div></div>
      </div>
      <div class="legend">${legend}</div>
    </div>`;
}

function timelineClock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(11, 16);
  return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function renderV2Timeline(d) {
  const panel = document.getElementById('timeline');
  const summary = document.getElementById('timeline-summary');
  const meta = document.getElementById('timeline-meta');
  if (!panel || !summary) return;
  if (meta) meta.textContent = `${d.date} · ${Number(d.tracked_hours || 0).toFixed(1)} h`;
  summary.innerHTML = `
    <div><strong>${d.work_blocks || 0}</strong><span>work blocks</span></div>
    <div><strong>${d.meetings || 0}</strong><span>calendar commitments</span></div>
    <div><strong>${d.app_switches || 0}</strong><span>app transitions</span></div>`;
  const events = d.events || [];
  if (!events.length) {
    panel.innerHTML = '<div class="empty">No work blocks or calendar commitments for this day.</div>';
    return;
  }
  panel.innerHTML = events.map(ev => {
    const start = timelineClock(ev.started_at);
    const end = timelineClock(ev.ended_at);
    if (ev.kind === 'meeting') {
      const project = ev.project_label || 'Needs review';
      return `<article class="timeline-event meeting">
        <div class="timeline-time">${escapeHtml(start)}<small>${escapeHtml(end)}</small></div>
        <div class="timeline-rail"><span></span></div>
        <div class="timeline-event-body">
          <div class="timeline-kind">Calendar</div>
          <h3>${escapeHtml(ev.title || 'Calendar commitment')}</h3>
          <div class="timeline-event-meta"><span>${escapeHtml(project)}</span>${ev.location ? `<span>${escapeHtml(ev.location)}</span>` : ''}</div>
        </div>
      </article>`;
    }
    const apps = (ev.apps || []).slice(0, 3)
      .map(a => `${escapeHtml(a.app)} ${fmtMins(a.minutes)}`).join(' · ');
    const domains = (ev.browser_domains || []).slice(0, 3)
      .map(x => escapeHtml(x.domain)).join(' · ');
    const confidence = ev.assignment_source === 'user' ? 'Confirmed'
      : (ev.assignment_source === 'fallback' ? 'Needs review'
      : (ev.confidence == null ? 'Unassigned' : `${Math.round(ev.confidence * 100)}% confidence`));
    const project = ev.project_label || 'Needs review';
    return `<article class="timeline-event work">
      <div class="timeline-time">${escapeHtml(start)}<small>${escapeHtml(end)}</small></div>
      <div class="timeline-rail"><span></span></div>
      <div class="timeline-event-body">
        <div class="timeline-event-top">
          <div><div class="timeline-kind">Work block · ${fmtMins(ev.minutes || 0)}</div><h3>${escapeHtml(ev.title || 'Unclear work block')}</h3></div>
          <button class="timeline-project" onclick="openCorrection('${escapeHtml(ev.id)}', '${escapeHtml(ev.project || '')}', this)" title="${escapeHtml(ev.attribution_method || '')}: ${escapeHtml(ev.attribution_reason || '')}">${escapeHtml(project)} · ${escapeHtml(confidence)}</button>
        </div>
        <div class="timeline-evidence">${escapeHtml(ev.title_evidence || 'Observed activity')}</div>
        <details class="timeline-details">
          <summary>Evidence and transitions</summary>
          <div class="timeline-detail-grid">
            <div><b>Apps</b><span>${apps || 'No app detail'}</span></div>
            <div><b>Transitions</b><span>${ev.app_switches || 0} within this block</span></div>
            <div><b>Browser</b><span>${domains || 'No browser evidence'}</span></div>
            <div><b>Other signals</b><span>${ev.file_events || 0} file changes · ${ev.captures || 0} notes</span></div>
          </div>
        </details>
      </div>
    </article>`;
  }).join('');
}

async function fetchV2Timeline() {
  const url = '/api/v2/timeline' + (currentDate ? '?date=' + currentDate : '');
  try {
    const r = await fetch(url);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    renderV2Timeline(d);
  } catch (e) {
    const panel = document.getElementById('timeline');
    if (panel) panel.innerHTML = '<div class="empty">Could not build the chronology.</div>';
  }
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
  } else if (v === 'snapshot') {
    dot.textContent = '◉'; dot.style.color = '#6366f1';
    btn.title = 'Snapshot preview, live tracker unchanged';
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
  const verdictLabel = {ok: 'All systems healthy', snapshot: 'Snapshot mode', warn: 'Needs a look',
                        fail: "Something's wrong", unknown: 'Status unknown'}[d.verdict] || 'Status unknown';
  const verdictColor = {ok: '#16a34a', snapshot: '#6366f1', warn: '#d97706', fail: '#dc2626',
                        unknown: 'var(--text-soft)'}[d.verdict] || 'var(--text-soft)';
  if (title) title.innerHTML = `System health <span style="color:${verdictColor}; font-weight:500;">· ${verdictLabel}</span>`;

  if (d.verdict === 'snapshot') {
    body.innerHTML = `<div class="empty">${escapeHtml(d.summary || 'This is an isolated preview snapshot.')}</div>`;
    return;
  }
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
  timeline:    { collapsed: false, visible: true },
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
    const memory = (d && d.memory) || {};
    panel.innerHTML =
      `<div class="brain-memory-empty">
        <div><strong>${memory.active_days || 0}</strong><span>active days observed</span></div>
        <div><strong>${memory.projects_observed || 0}</strong><span>project areas recognised</span></div>
        <div><strong>${memory.corrections || 0}</strong><span>things you have taught it</span></div>
      </div>
      <p class="brain-memory-note">WorkPulse has evidence, but it has not yet consolidated it into a living profile.</p>
      <button class="ask-btn" onclick="refreshProfile()">Build my memory snapshot</button>`;
    meta.textContent = '';
    if (summary) summary.textContent = `${memory.active_days || 0} active days observed · profile not consolidated`;
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
  panel.innerHTML = renderProfileMarkdown(body) +
    '<div class="brain-memory-actions"><button class="quiet-btn" onclick="refreshProfile()">Refresh from recent evidence</button></div>';
}

async function refreshProfile() {
  const panel = document.getElementById('profile-panel');
  panel.innerHTML = '<div class="empty">Consolidating your local work evidence…</div>';
  try {
    const r = await fetch('/api/v2/profile/refresh', {method: 'POST'});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await fetchProfile();
    showToast('Memory snapshot refreshed.');
  } catch (e) {
    panel.innerHTML = '<div class="empty">Could not refresh the memory snapshot.</div>';
  }
}

// ── Correction inbox: only uncertain or contradictory project decisions ───

async function fetchReview() {
  const panel = document.getElementById('attn-panel');
  const meta = document.getElementById('review-meta');
  try {
    const r = await fetch('/api/v2/review?days=7');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    if (meta) meta.textContent = d.count ? `${d.count} decisions` : 'All clear';
    if (!d.items || !d.items.length) {
      panel.innerHTML =
        '<div class="review-clear"><strong>Nothing needs teaching.</strong><span>WorkPulse has enough evidence for the recent work it filed.</span></div>';
      return;
    }
    panel.innerHTML = `<div class="review-intro">Correct only what matters. Each answer becomes a teaching example for future work.</div>` +
      d.items.map(item => {
        const isConflict = item.kind === 'contradiction';
        const kindLabel = isConflict ? 'Conflicting signals'
          : item.kind === 'unassigned' ? 'Not filed' : 'Low confidence';
        const current = item.assigned_label || 'Not filed';
        const suggested = item.suggested_label || '';
        let actions = '';
        const clusterIds = (item.cluster_ids || [item.cluster_id]).join(',');
        if (isConflict && item.suggested_stream) {
          actions += `<button class="review-primary" onclick="correctReview('${jsAttr(clusterIds)}','${jsAttr(item.suggested_stream)}')">${escapeHtml(suggested)} looks right</button>`;
        }
        if (item.assigned_stream) {
          actions += `<button class="review-secondary" onclick="correctReview('${jsAttr(clusterIds)}','${jsAttr(item.assigned_stream)}')">Keep ${escapeHtml(current)}</button>`;
        }
        actions += `<button class="review-link" onclick="openCorrection('${jsAttr(item.cluster_id)}','${jsAttr(item.assigned_stream || '')}',this)">Choose another</button>`;
        return `<article class="review-item review-${escapeHtml(item.kind)}">
          <div class="review-topline"><span class="review-kind">${kindLabel}</span><span>${fmtMins(item.minutes)} · ${item.instances > 1 ? `${item.instances} similar blocks` : escapeHtml((item.started_at || '').slice(0,10))}</span></div>
          <h4>${escapeHtml(item.title)}</h4>
          <p>${escapeHtml(item.reason)}</p>
          <div class="review-filing"><span>Filed as <strong>${escapeHtml(current)}</strong></span>${suggested ? `<span>Title suggests <strong>${escapeHtml(suggested)}</strong></span>` : ''}</div>
          <div class="review-actions">${actions}</div>
        </article>`;
      }).join('');
  } catch (e) {
    if (meta) meta.textContent = '';
    panel.innerHTML = '<div class="empty">Could not load decisions for review.</div>';
  }
}

let workflowLearningState = null;

async function fetchWorkflowLearning() {
  const card = document.getElementById('workflow-card');
  const panel = document.getElementById('workflow-panel');
  try {
    const r = await fetch('/api/v2/workflows/proposal');
    const d = await r.json();
    if (!d.enabled) {
      card.style.display = 'none';
      return;
    }
    workflowLearningState = d;
    card.style.display = '';
    const heldOutStages = d.held_out?.stages || [];
    panel.innerHTML = `
      <div class="workflow-demo-head">
        <div><span class="permission-state local">Learned locally</span><span class="workflow-status">${escapeHtml(d.status)}</span></div>
        <span>${d.examples.length} example journeys</span>
      </div>
      <div class="workflow-demo-grid">
        <div>
          <span class="showcase-label">CANDIDATE PERSONAL METHOD</span>
          <h3>${escapeHtml(d.name)}</h3>
          <p>${escapeHtml(d.basis)}</p>
          <div class="workflow-mini-steps">${d.steps.map((step, i) =>
            `<span class="${heldOutStages.includes(step.action_type) ? 'seen' : ''}" title="${escapeHtml(step.name)}">${i + 1}</span>`
          ).join('')}</div>
        </div>
        <div class="workflow-nudge">
          <span class="showcase-label">CONTEXTUAL NUDGE</span>
          <strong>${escapeHtml(d.held_out?.label || 'Held-out proposal journey')}</strong>
          <p>${escapeHtml(d.nudge || d.gap)}</p>
        </div>
      </div>
      <button class="text-action" onclick="openWorkflowModal()">Inspect evidence and method →</button>`;
  } catch (e) {
    card.style.display = 'none';
  }
}

function openWorkflowModal() {
  if (!workflowLearningState) return;
  const d = workflowLearningState;
  const body = document.getElementById('workflow-modal-body');
  body.innerHTML = `
    <span class="permission-state local">Learned from real local evidence</span>
    <h2 id="workflow-modal-title">${escapeHtml(d.name)}</h2>
    <div class="sub">${escapeHtml(d.privacy)} Client names and filenames are not shown in this demonstration.</div>
    <div class="workflow-evidence-grid">
      <div><span class="showcase-label">OBSERVED EXAMPLES</span>${d.examples.map(ex =>
        `<article><strong>${escapeHtml(ex.label)}</strong><small>${escapeHtml(ex.period)} · ${ex.evidence_markers} distinct evidence markers</small><small>${ex.stages.map(s => escapeHtml(s.replace(/_/g,' '))).join(' → ')}</small></article>`
      ).join('')}</div>
      <div><span class="showcase-label">PROPOSED METHOD</span><ol>${d.steps.map(step =>
        `<li><strong>${escapeHtml(step.name)}${step.optional ? ' (optional)' : ''}</strong><small>Expected marker: ${escapeHtml(step.expected_evidence)}</small><small>Observed in ${step.journeys_observed} journey${step.journeys_observed === 1 ? '' : 's'} · ${Math.round(step.confidence * 100)}% evidence coverage</small></li>`
      ).join('')}</ol></div>
    </div>
    <div class="workflow-observations"><span class="showcase-label">WHAT THE EVIDENCE TAUGHT WORKPULSE</span>${(d.observations || []).map(item =>
      `<article><strong>${escapeHtml(item.finding)}</strong><small>${escapeHtml(item.evidence)}</small></article>`
    ).join('')}<p>${escapeHtml(d.ordering_note || '')}</p></div>
    <div class="workflow-nudge modal-nudge"><span class="showcase-label">HELD-OUT JOURNEY TEST</span><strong>${escapeHtml(d.held_out?.label || 'Most recent observed journey')}</strong><p>${escapeHtml(d.nudge || d.gap)}</p></div>
    <p class="workflow-gap"><strong>What WorkPulse still cannot know:</strong> ${escapeHtml(d.gap)}</p>`;
  const btn = document.getElementById('workflow-confirm-btn');
  btn.textContent = d.status === 'confirmed' ? 'Method confirmed' : 'Confirm this method';
  btn.disabled = d.status === 'confirmed';
  document.getElementById('workflow-modal').classList.add('open');
}

function closeWorkflowModal() {
  document.getElementById('workflow-modal').classList.remove('open');
}

async function confirmLearnedWorkflow() {
  const btn = document.getElementById('workflow-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Confirming locally…';
  try {
    const r = await fetch('/api/v2/workflows/proposal/confirm', {method: 'POST'});
    if (!r.ok) throw new Error('confirmation failed');
    const d = await r.json();
    workflowLearningState = d.workflow;
    btn.textContent = 'Method confirmed';
    showToast('Observed proposal method confirmed locally.');
    await fetchWorkflowLearning();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Try again';
  }
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
    const isIdentity = s.heading === 'Identity';
    if (s.heading && !isIdentity) {
      html += `<details class="brain-memory-section"><summary>${escapeHtml(s.heading)}</summary><div class="brain-memory-section-body">`;
    } else {
      html += '<div class="brain-memory-identity">';
    }
    if (s.heading) {
      if (isIdentity) {
        html += `<div style="font-size:13px; color:var(--text-soft); font-weight:600; margin-bottom:6px;">${escapeHtml(s.heading)}</div>`;
      }
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
    html += s.heading && !isIdentity ? '</div></details>' : '</div>';
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

function todayStreamLabel(key) {
  if (key === '<untagged>') return 'Needs review';
  if (key === 'workpulse') return 'WorkPulse';
  const found = availableStreams.find(s => s.key === key);
  return found ? found.label : TITLE_CASE((key || '').replace(/-/g, ' '));
}

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
        .map(s => `${todayStreamLabel(s.stream)} ${s.hours.toFixed(1)}h`)
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
      html += `<div title="${escapeHtml(todayStreamLabel(s.stream))}: ${s.hours.toFixed(1)} h" style="width:${pct}%; background:${color};"></div>`;
    });
    html += '</div>';
    html += '<div style="display:flex; flex-wrap:wrap; gap:14px; margin-top:8px; font-size:12px;">';
    d.by_stream.forEach((s, i) => {
      const color = todayStreamColor(s.stream, i);
      html += `<span style="display:inline-flex; align-items:center; gap:6px;"><span style="display:inline-block; width:10px; height:10px; background:${color}; border-radius:2px;"></span><strong>${escapeHtml(todayStreamLabel(s.stream))}</strong> ${s.hours.toFixed(1)} h</span>`;
    });
    html += '</div></div>';
  }

  // Top named clusters — each with a side-channel context line (git + captures + skill runs)
  if (d.top_clusters && d.top_clusters.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Work blocks today</div>';
    html += '<div style="display:flex; flex-direction:column; gap:8px; margin-bottom:14px;">';
    d.top_clusters.slice(0, 5).forEach(c => {
      const name = escapeHtml(c.output_title || c.name || 'Unclear work block');
      const evidence = escapeHtml(c.evidence_label || 'Observed activity');

      // Fix 1.7: project assignment + confidence badge + correction dropdown
      let assignBadge = '';
      const label = c.assigned_label || c.assigned_stream || 'Needs review';
      const conf = c.assignment_confidence;
      const src = c.assignment_source;
      let badgeBg = '#e5e7eb';
      let badgeFg = '#374151';
      let confText = '';
      if (src === 'user') {
        badgeBg = '#dcfce7'; badgeFg = '#166534';
        confText = '· confirmed';
      } else if (src === 'agent') {
        badgeBg = conf >= 0.7 ? '#dbeafe' : (conf >= 0.4 ? '#fef3c7' : '#fee2e2');
        badgeFg = conf >= 0.7 ? '#1e40af' : (conf >= 0.4 ? '#92400e' : '#991b1b');
        confText = '· ' + (conf * 100).toFixed(0) + '%';
      } else if (src === 'fallback') {
        confText = '· review';
      } else {
        confText = '· not assigned';
      }
      const method = c.attribution_method || 'WorkPulse rules';
      const reason = c.attribution_reason || 'Click to review or correct';
      assignBadge = `<span style="display:inline-block; padding:2px 8px; background:${badgeBg}; color:${badgeFg}; border-radius:10px; font-size:11px; font-weight:600; cursor:pointer;" onclick="openCorrection('${escapeHtml(c.cluster_id)}', '${escapeHtml(c.assigned_stream || '')}', this)" title="${escapeHtml(method)}: ${escapeHtml(reason)}. Click to correct.">${escapeHtml(label)} ${escapeHtml(confText)}</span>`;

      // Context line: "what was actually happening" — git + captures + brain calls
      let ctxLine = '';
      if (c.evidence_kind === 'commit' && c.summary) {
        ctxLine = `<div style="margin-top:4px; font-size:12px; color:var(--text-soft);">↳ ${escapeHtml(c.summary)}</div>`;
      } else if (c.captures_count) {
        ctxLine = `<div style="margin-top:4px; font-size:12px; color:var(--text-soft);">↳ ${c.captures_count} contextual note${c.captures_count === 1 ? '' : 's'}</div>`;
      } else if (c.file_events_count >= 10) {
        ctxLine = `<div style="margin-top:4px; font-size:12px; color:var(--text-soft);">↳ ${c.file_events_count} related file changes observed</div>`;
      }
      // Top commit subjects (one-line each, max 3)
      let commitsLine = '';
      if (c.evidence_kind === 'commit' && c.top_commits && c.top_commits.length) {
        commitsLine = '<ul style="margin:6px 0 0 18px; padding:0; font-size:12px; color:var(--text-soft);">';
        c.top_commits.forEach(g => {
          commitsLine += `<li><span style="font-family:var(--font-mono,monospace); color:var(--text-faint);">${escapeHtml(g.sha)}</span> ${escapeHtml(g.subject)}</li>`;
        });
        commitsLine += '</ul>';
      }

      html += `<div data-cluster="${escapeHtml(c.cluster_id)}" style="padding:10px 12px; background:var(--surface, #fff); border:1px solid var(--border, #eee5d2); border-radius:6px;">
        <div style="display:flex; align-items:baseline; justify-content:space-between; gap:12px;">
          <div><strong>${name}</strong><div style="margin-top:3px; font-size:11px; color:var(--text-faint);">${evidence}</div></div>
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
  let html = '<div style="font-size:11px; color:var(--text-soft); margin-bottom:6px; padding:0 4px;">Set this work block to:</div><div style="font-size:11px; color:var(--text-faint); margin:0 4px 8px;">Your correction is saved as a teaching example.</div>';
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
    showToast('Correction saved. WorkPulse will treat your choice as the source of truth.');
    if (typeof fetchToday === 'function') fetchToday();
    if (typeof fetchReview === 'function') fetchReview();
  } catch (e) {
    alert('Correction failed: ' + e.message);
  }
}

async function correctReview(clusterIds, toStream) {
  const ids = (clusterIds || '').split(',').filter(Boolean);
  try {
    const responses = await Promise.all(ids.map(clusterId =>
      fetch(`/api/v2/cluster/${clusterId}/correct`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({to_stream: toStream}),
      })
    ));
    if (responses.some(r => !r.ok)) throw new Error('one or more corrections failed');
    showToast(ids.length > 1
      ? `${ids.length} similar work blocks taught.`
      : 'Correction saved as a teaching example.');
    await Promise.all([fetchReview(), fetchToday(), fetchProfile()]);
  } catch (e) {
    showToast('Could not save this correction.');
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
      const mtg = d.meetings_retagged || 0;
      const mtgBit = mtg ? ` and ${mtg} meeting${mtg === 1 ? '' : 's'}` : '';
      showToast(streamKey
        ? `Tagged as ${streamKey}. Attributed ${n} window${n === 1 ? '' : 's'}${mtgBit}; WorkPulse remembers.`
        : `Will ignore this in future.`);
      await Promise.all([fetchRealWork(), fetchMeetings()]);
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

// ── Trust, organization preview, and product feedback ─────────────────────
function openTrustModal() { document.getElementById('trust-modal').classList.add('open'); }
function closeTrustModal() { document.getElementById('trust-modal').classList.remove('open'); }

let organizationSnapshot = null;
function closeOrganizationModal() { document.getElementById('organization-modal').classList.remove('open'); }
async function openOrganizationModal() {
  document.getElementById('organization-modal').classList.add('open');
  const panel = document.getElementById('organization-preview');
  panel.innerHTML = '<div class="empty">Loading...</div>';
  try {
    const date = activeDate() ? `?date=${encodeURIComponent(activeDate())}` : '';
    const [preview, context] = await Promise.all([
      fetch('/api/v2/organization/preview' + date).then(r => r.json()),
      fetch('/api/v2/organization/context').then(r => r.json()),
    ]);
    organizationSnapshot = preview;
    const rows = (preview.projects || []).map(p =>
      `<div class="org-row"><span>${escapeHtml(TITLE_CASE(p.label))}</span><b>${Number(p.hours || 0).toFixed(1)}h</b></div>`
    ).join('');
    panel.innerHTML = rows || '<div class="empty">No attributed project work yet.</div>';
    panel.innerHTML += `<div class="org-row org-total"><span>Total shareable work</span><b>${Number(preview.total_hours || 0).toFixed(1)}h</b></div>`;
    document.getElementById('manager-priorities').value = context.priorities || '';
    document.getElementById('manager-outcomes').value = context.expected_outcomes || '';
    document.getElementById('manager-feedback').value = context.feedback || '';
  } catch (e) {
    panel.innerHTML = '<div class="empty">Could not build the update preview.</div>';
  }
}

async function saveManagerContext() {
  const payload = {
    priorities: document.getElementById('manager-priorities').value,
    expected_outcomes: document.getElementById('manager-outcomes').value,
    feedback: document.getElementById('manager-feedback').value,
  };
  const r = await fetch('/api/v2/organization/context', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  showToast(r.ok ? 'Manager context saved locally.' : 'Could not save context.');
}

function organizationUpdateText() {
  const d = organizationSnapshot || {projects:[], total_hours:0, date:activeDate()};
  const lines = [`Work update - ${d.date || ''}`, ''];
  (d.projects || []).forEach(p => lines.push(`- ${TITLE_CASE(p.label)}: ${Number(p.hours || 0).toFixed(1)}h`));
  lines.push('', `Total: ${Number(d.total_hours || 0).toFixed(1)}h`);
  const note = document.getElementById('org-update').value.trim();
  if (note) lines.push('', 'Update:', note);
  lines.push('', 'Shared by the user from WorkPulse. Raw activity and personal data excluded.');
  return lines.join('\n');
}

async function copyOrganizationUpdate() {
  try {
    await navigator.clipboard.writeText(organizationUpdateText());
    showToast('Update copied. Nothing was sent automatically.');
  } catch (e) {
    showToast('Clipboard unavailable. Select and copy the preview manually.');
  }
}

let feedbackRating = 0;
let feedbackStatusChecked = false;
function openFeedbackModal() { document.getElementById('feedback-modal').classList.add('open'); }
function openFeedbackFromInvite() {
  document.getElementById('feedback-invite').classList.remove('open');
  openFeedbackModal();
}
function dismissFeedbackInvite() {
  document.getElementById('feedback-invite').classList.remove('open');
  localStorage.setItem('wp.feedback.snoozeUntil', String(Date.now() + 86400000));
}
function closeFeedbackModal() {
  document.getElementById('feedback-modal').classList.remove('open');
  localStorage.setItem('wp.feedback.snoozeUntil', String(Date.now() + 86400000));
}
function setFeedbackRating(n) {
  feedbackRating = n;
  document.querySelectorAll('#feedback-rating button').forEach((b, i) => b.classList.toggle('selected', i < n));
}
async function maybePromptFeedback() {
  if (feedbackStatusChecked || window.__wpTourActive) return;
  feedbackStatusChecked = true;
  const snooze = Number(localStorage.getItem('wp.feedback.snoozeUntil') || 0);
  if (Date.now() < snooze) return;
  try {
    const d = await (await fetch('/api/v2/feedback/status')).json();
    if (d.due) document.getElementById('feedback-invite').classList.add('open');
  } catch (e) { /* feedback must never disturb the dashboard */ }
}
async function dismissFeedbackForever() {
  await fetch('/api/v2/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({opt_out:true})});
  document.getElementById('feedback-modal').classList.remove('open');
  showToast('Feedback prompts turned off.');
}
async function sendFeedback() {
  const answers = {
    rating: feedbackRating,
    useful: document.getElementById('feedback-useful').value.trim(),
    missing: document.getElementById('feedback-missing').value.trim(),
    expected: document.getElementById('feedback-expected').value.trim(),
    contact_email: document.getElementById('feedback-email').value.trim(),
    contact_ok: document.getElementById('feedback-contact-ok').checked,
  };
  const status = document.getElementById('feedback-send-status');
  status.textContent = 'Saving your feedback...';
  try {
    const r = await fetch('/api/v2/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({answers})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'could not save');
    status.textContent = d.delivered
      ? 'Thank you. Your feedback was sent.'
      : 'Thank you. Your feedback is saved privately on this machine; developer delivery is not configured yet.';
    setTimeout(() => document.getElementById('feedback-modal').classList.remove('open'), 1800);
  } catch (e) { status.textContent = 'Could not save feedback: ' + e.message; }
}

// ── Meetings to file (calendar side of the tag-the-untagged loop) ───────────
// Surfaces meetings the calendar knows about that aren't tied to a project yet.
// Tagging one reuses learn(), which now also files matching meetings + windows.
async function fetchMeetings() {
  const panel = document.getElementById('meetings-panel');
  const card = document.getElementById('meetings-card');
  if (!panel) return;
  let d;
  try {
    d = await (await fetch('/api/meetings/untagged')).json();
  } catch (e) {
    panel.innerHTML = '<div class="empty">Could not load meetings.</div>';
    return;
  }
  const meetings = d.meetings || [];
  if (!meetings.length) {
    // Nothing to file (or no calendar configured) — keep the dashboard quiet.
    if (card) card.style.display = 'none';
    return;
  }
  if (card) card.style.display = '';
  panel.innerHTML = meetings.map((m, i) => {
    const meta = m.count > 1 ? `${m.count}×` : relativeDateLabel((m.last_at || '').slice(0, 10));
    return `
      <div class="attn-row">
        <div class="attn-title" title="${escapeHtml(m.title)}">${escapeHtml(m.title)}</div>
        <div class="attn-min">${escapeHtml(meta)}</div>
        <div class="attn-tag">
          <button class="tag-btn" onclick="toggleTagMenu('mtg${i}')">Tag as ▾</button>
          <div class="tag-menu" id="tag-menu-mtg${i}">
            ${availableStreams.map(s => `
              <div class="tag-opt" onclick="learn('${jsAttr(m.title)}', '${s.key}')">
                <span class="lg-dot" style="background:${s.color}"></span>
                <span>${escapeHtml(s.label)}</span>
              </div>`).join('')}
            <div class="tag-opt ignore" onclick="learn('${jsAttr(m.title)}', null)">
              Not a project (ignore)
            </div>
          </div>
        </div>
      </div>`;
  }).join('');
}

// ── Refresh orchestration ──────────────────────────────────────────────────
async function refreshDayPanels() {
  await Promise.all([fetchHealth(), fetchPersonal(), fetchProfile(), fetchWorkflowLearning(), fetchToday(), fetchV2Timeline(), fetchLastActive(), fetchAI(), fetchMeetings()]);
  await fetchRealWork();  // Today is the fallback source when legacy activity is empty.
  await fetchReview();    // replaces the legacy title list with teachable decisions
  await fetchHeatmap();   // re-render so selected day highlights
}

// Greeting follows the viewer's local clock — morning/afternoon/evening/night —
// instead of the hard-coded "Good morning" the page ships with.
function setGreeting() {
  const h = new Date().getHours();
  let text, icon;
  if (h < 5)       { text = 'Still up';       icon = '🌙'; }
  else if (h < 12) { text = 'Good morning';   icon = '☀️'; }
  else if (h < 17) { text = 'Good afternoon'; icon = '🌤️'; }
  else if (h < 22) { text = 'Good evening';   icon = '🌇'; }
  else             { text = 'Winding down';   icon = '🌙'; }
  const t = document.getElementById('hero-greeting-text');
  const i = document.getElementById('hero-icon');
  if (t) t.textContent = text;
  if (i) i.textContent = icon;
}

async function refresh() {
  setGreeting();
  await fetchSystem();
  if (todayISO) renderDateBar();
  setHeaderDate();
  await refreshDayPanels();
  document.getElementById('refresh-label').textContent =
    'Updated ' + new Date().toLocaleTimeString();
  await maybePromptFeedback();
}

refresh();
setInterval(refresh, 30000);


// ── First-run guided tour ───────────────────────────────────────────────────
// Coach-marks that spotlight each part of the dashboard and end by opening
// stream setup. Shown once (localStorage 'wp.tourDone'); replay via the Tour
// button. Steps target real element ids so the highlight tracks the layout.
const WP_TOUR_STEPS = [
  { title: "Your work memory, on your device",
    body: "<b>WorkPulse is useful before you sign in or connect to anyone.</b><br><br>It builds a private record of what moved, what still needs context, and what your work patterns may teach you. This takes about one minute.",
    workspace: "today" },
  { target: "#privacy-workspace", title: "It observes signals, not your screen",
    body: "WorkPulse uses the active app and window, idle state, file-change metadata, and optional browser or calendar context.<br><br><b>It does not record keystrokes or continuously take screenshots.</b> Raw evidence stays on this device.",
    workspace: "privacy" },
  { target: "#today-card", title: "Observation becomes a proposed work story",
    body: "Today groups those signals into projects and outputs. A title or app is evidence, not proof. When WorkPulse cannot establish the meaning, it says so instead of guessing.",
    workspace: "today" },
  { target: "[data-card=\"attention\"]", title: "Teach only the important mistakes",
    body: "Needs your attention shows contradictory or uncertain filings. Confirm or correct a decision once; WorkPulse saves that answer as a teaching example for similar work.",
    workspace: "brain" },
  { target: "#ask-card", title: "Ask your own work",
    body: "Ask what moved today, where an output was left, or how you approached previous work. The fast answer is deterministic. <b>Local Ollama is optional</b> for deeper interpretation.",
    workspace: "brain" },
  { target: "#organization-workspace", title: "An organisation is a separate permission",
    body: "No manager is connected by default. If you opt in later, the organisation receives only the approved output-level fields you review—not raw titles, URLs, files, personal activity, or your private Brain.",
    workspace: "organization" },
  { target: "#wp-settings-btn", title: "Name the work areas that matter",
    body: "Add the projects and responsibilities you actually work across. This gives the deterministic brain a starting vocabulary; it will still expose uncertainty and learn from corrections.",
    workspace: "today", ctaLabel: "Set up my projects", cta: true },
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
  if (step.workspace) {
    showWorkspace(step.workspace, {skipPersist: true, instant: true});
  }
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
  if (b === 'workflow-memory') return 'Learned method';
  return 'Manual';
}

function askSuggestion(question) {
  const input = document.getElementById('ask-input');
  if (!input) return;
  input.value = question;
  document.getElementById('ask-form').requestSubmit();
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
  const gap = document.getElementById('ask-gap');
  const evidenceWrap = document.getElementById('ask-evidence-wrap');
  const evidenceSummary = document.getElementById('ask-evidence-summary');
  const badge = document.getElementById('ask-backend');
  result.style.display = 'block';
  answer.innerHTML = '<div class="empty">Thinking…</div>';
  evidence.innerHTML = '';
  gap.style.display = 'none';
  gap.innerHTML = '';
  evidenceWrap.style.display = 'none';
  try {
    const useAI = !!document.getElementById('ask-use-ai')?.checked;
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), 45000);
    const progressTimer = useAI ? setTimeout(() => {
      answer.innerHTML =
        '<div class="empty">Ollama is reading your local evidence. Richer answers can take up to 35 seconds…</div>';
    }, 7000) : null;
    let r;
    let fallbackNotice = '';
    try {
      r = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({question: q, use_ai: useAI}),
        signal: controller.signal,
      });
    } catch (err) {
      if (!useAI || err.name !== 'AbortError') throw err;
      fallbackNotice =
        '<div class="ask-timeout-note">Ollama took too long, so WorkPulse returned the fast local answer instead.</div>';
      r = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({question: q, use_ai: false}),
      });
    } finally {
      clearTimeout(abortTimer);
      if (progressTimer) clearTimeout(progressTimer);
    }
    const data = await r.json();
    if (useAI && data.fallback && !fallbackNotice) {
      fallbackNotice =
        '<div class="ask-timeout-note">Local AI could not improve this answer safely, so WorkPulse used the complete grounded summary instead.</div>';
    }
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
    answer.innerHTML = fallbackNotice + renderMarkdownLite(data.answer);
    if (data.gap) {
      gap.style.display = 'block';
      gap.innerHTML = `<b>What WorkPulse still doesn’t know</b><p>${escapeHtml(data.gap)}</p>`;
    }
    const ev = data.evidence || [];
    if (ev.length) {
      evidenceWrap.style.display = 'block';
      evidenceSummary.textContent = `${ev.length} evidence item${ev.length === 1 ? '' : 's'} used`;
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
          const label = e.content || e.stream || e.atom_kind || 'Observed activity';
          const meta = [e.date, e.stream, e.atom_kind].filter(Boolean).join(' · ');
          return '<span class="ask-chip" title="' + escapeHtml(meta) + '">' +
                 escapeHtml(label) + '</span>';
        }
        return '';
      }).join('');
    }
  } catch (err) {
    answer.innerHTML = '<div class="empty">Could not reach WorkPulse.</div>';
  }
  return false;
}

// ── Product workspace navigation ───────────────────────────────────────────
// The existing cards remain the data-rendering primitives. This layer gives
// them a product-shaped information architecture without duplicating APIs.
const WORKSPACE_COPY = {
  today: {
    kicker: 'PERSONAL WORKPULSE',
    badge: 'Live',
    badgeClass: 'live',
    title: 'Today',
    description: 'What moved forward, what needs attention, and what WorkPulse still does not know.',
    action: 'Add context',
  },
  timeline: {
    kicker: 'EVIDENCE',
    badge: 'Live',
    badgeClass: 'live',
    title: 'Timeline',
    description: 'Calendar, work blocks, meetings and app-switch evidence in one chronology.',
    action: 'Review gaps',
  },
  brain: {
    kicker: 'PERSONAL MEMORY',
    badge: 'Live',
    badgeClass: 'live',
    title: 'Brain',
    description: 'Ask about previous work, inspect reusable methods, and correct what WorkPulse has learned.',
    action: 'Ask WorkPulse',
  },
  organization: {
    kicker: 'OPTIONAL CONNECTION',
    badge: 'Not connected',
    badgeClass: 'consent',
    title: 'Organisation opt-in',
    description: 'Personal WorkPulse is complete without this layer. Connect only when the benefit and permissions are clear.',
    action: 'Review consent',
  },
  classroom: {
    kicker: 'WORKPULSE CLASSROOM',
    badge: '',
    badgeClass: '',
    title: 'Classroom',
    description: 'Scheduled learning policies activate automatically on enrolled devices. WorkPulse remains quiet until a decision needs attention.',
    action: 'Open Session Console',
  },
  privacy: {
    kicker: 'DATA BOUNDARY',
    badge: 'Live policy',
    badgeClass: 'live',
    title: 'Privacy',
    description: 'See what remains personal, what may be shared, and how retention limits the memory.',
    action: 'Open vault',
  },
};

const WORKSPACE_SELECTORS = {
  today: ['#capture-bar', '#today-card', '[data-card="donut"]', '[data-card="last-active"]'],
  timeline: ['[data-card="heatmap"]', '[data-card="timeline"]', '[data-card="apps"]',
             '[data-card="meetings"]', '#raw-sessions-card'],
  brain: ['#ask-card', '#profile-card', '#workflow-card', '[data-card="attention"]', '[data-card="ai"]'],
  organization: ['#organization-workspace'],
  classroom: ['#classroom-workspace'],
  privacy: ['#privacy-workspace'],
};

let activeWorkspace = 'today';

function allWorkspaceElements() {
  const selectors = Object.values(WORKSPACE_SELECTORS).flat();
  return Array.from(new Set(selectors.flatMap(sel => Array.from(document.querySelectorAll(sel)))));
}

function showWorkspace(name, options) {
  const next = WORKSPACE_COPY[name] ? name : 'today';
  activeWorkspace = next;
  const opts = options || {};
  allWorkspaceElements().forEach(el => el.classList.add('workspace-hidden'));
  (WORKSPACE_SELECTORS[next] || []).forEach(sel => {
    document.querySelectorAll(sel).forEach(el => el.classList.remove('workspace-hidden'));
  });
  if (next === 'timeline') {
    const chronology = document.querySelector('[data-card="timeline"]');
    if (chronology) {
      chronology.classList.remove('hidden', 'is-collapsed');
      if (chronology.parentElement) chronology.parentElement.prepend(chronology);
    }
  }
  if (next === 'brain') {
    const profile = document.getElementById('profile-card');
    if (profile) profile.classList.remove('hidden', 'is-collapsed');
  }
  if (next === 'classroom' && !opts.keepClassroomMode) {
    showClassroomConsole('session');
    fetchClassroomStatus();
  }

  document.querySelectorAll('[data-workspace-target]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.workspaceTarget === next);
  });

  const copy = WORKSPACE_COPY[next];
  document.getElementById('workspace-kicker').innerHTML =
    escapeHtml(copy.kicker) + (copy.badge
      ? ' <span class="capability-badge ' + copy.badgeClass + '">' + escapeHtml(copy.badge) + '</span>'
      : '');
  document.getElementById('workspace-title').textContent = copy.title;
  document.getElementById('workspace-description').textContent = copy.description;
  const workspaceAction = document.getElementById('workspace-primary-action');
  workspaceAction.textContent = copy.action;
  workspaceAction.classList.toggle('workspace-hidden', next === 'classroom');

  const isPersonalDay = next === 'today' || next === 'timeline';
  document.querySelectorAll('.datebar').forEach(el => el.classList.toggle('workspace-hidden', !isPersonalDay));
  const aiBanner = document.getElementById('ai-banner');
  if (aiBanner) aiBanner.classList.toggle('workspace-hidden', next === 'organization' || next === 'classroom' || next === 'privacy');
  const streamsBanner = document.getElementById('streams-banner');
  if (streamsBanner) streamsBanner.classList.toggle('workspace-hidden', next !== 'today');
  const hero = document.querySelector('.hero');
  if (hero) hero.classList.toggle('workspace-hidden', next !== 'today');
  const untagged = document.getElementById('untagged-alert');
  if (untagged && next !== 'today') untagged.classList.add('workspace-hidden');
  else if (untagged) untagged.classList.remove('workspace-hidden');

  if (!opts.skipPersist) localStorage.setItem('wp_active_workspace', next);
  window.scrollTo({ top: 0, behavior: opts.instant ? 'auto' : 'smooth' });
}

function focusPrimaryAction() {
  if (activeWorkspace === 'today') {
    const input = document.getElementById('capture-input');
    if (input) input.focus();
  } else if (activeWorkspace === 'timeline') {
    showWorkspace('brain');
    const attn = document.querySelector('[data-card="attention"]');
    if (attn) attn.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } else if (activeWorkspace === 'brain') {
    const input = document.getElementById('ask-input');
    if (input) input.focus();
  } else if (activeWorkspace === 'organization') {
    openOrganizationModal();
  } else if (activeWorkspace === 'classroom') {
    showClassroomConsole('session');
  } else if (activeWorkspace === 'privacy') {
    openPersonalModal();
  }
}

let classroomDecision = '';
let activeClassroomMode = 'overview';
let classroomPollTimer = null;

function showClassroomConsole(name) {
  const next = ['session', 'student', 'policy'].includes(name) ? name : 'session';
  document.querySelectorAll('.classroom-console-panel').forEach(el => el.classList.add('workspace-hidden'));
  const panel = document.getElementById('classroom-console-' + next);
  if (panel) panel.classList.remove('workspace-hidden');
  document.querySelectorAll('[data-classroom-console]').forEach(button => {
    button.classList.toggle('active', button.dataset.classroomConsole === next);
  });
  if (next === 'policy') loadClassroomPolicy();
  fetchClassroomStatus();
}

function policyLines(id) {
  const field = document.getElementById(id);
  return (field ? field.value : '').split(/\r?\n|,/)
    .map(value => value.trim()).filter(Boolean);
}

async function loadClassroomPolicy() {
  const response = await fetch('/api/v2/classroom/policy?mode=class');
  const result = await response.json();
  const policy = result.policy || {};
  const fields = {
    'classroom-policy-allowed-domains': policy.allowed_domains,
    'classroom-policy-allowed-apps': policy.allowed_apps,
    'classroom-policy-blocked-domains': policy.blocked_domains,
    'classroom-policy-blocked-apps': policy.blocked_apps,
  };
  Object.entries(fields).forEach(([id, values]) => {
    const field = document.getElementById(id);
    if (field) field.value = (values || []).join('\n');
  });
}

async function saveClassroomPolicy() {
  const response = await fetch('/api/v2/classroom/policy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      mode: 'class',
      policy: {
        allowed_domains: policyLines('classroom-policy-allowed-domains'),
        allowed_apps: policyLines('classroom-policy-allowed-apps'),
        blocked_domains: policyLines('classroom-policy-blocked-domains'),
        blocked_apps: policyLines('classroom-policy-blocked-apps'),
      },
    }),
  });
  const result = await response.json();
  if (!response.ok) {
    showToast(result.error || 'Could not save policy');
    return;
  }
  const saved = document.getElementById('classroom-policy-saved');
  if (saved) saved.textContent = 'Saved now · the next class session will send this policy to enrolled devices.';
  showToast('Class policy saved');
}

async function createClassroomPairing() {
  const response = await fetch('/api/v2/classroom/pairing', {method: 'POST'});
  const result = await response.json();
  const box = document.getElementById('classroom-pairing-result');
  if (!box) return;
  const args = ' classroom-agent enroll --server ' + result.server +
    ' --code ' + result.code + ' --name "Lab Laptop"';
  const macCommand = '/Applications/WorkPulse.app/Contents/MacOS/WorkPulse --cli' + args;
  const windowsCommand = '"%LOCALAPPDATA%\\Programs\\WorkPulse\\WorkPulse.exe" --cli' + args;
  box.classList.remove('workspace-hidden');
  box.innerHTML =
    '<span>ONE-TIME CODE</span><strong>' + escapeHtml(result.code) + '</strong>' +
    '<small>Expires ' + escapeHtml(new Date(result.expires_at).toLocaleTimeString()) + '</small>' +
    '<label>macOS</label><code>' + escapeHtml(macCommand) + '</code>' +
    '<label>Windows</label><code>' + escapeHtml(windowsCommand) + '</code>' +
    '<p>After enrolment, keep the agent running with the matching WorkPulse executable and <b>--cli classroom-agent run</b>.</p>';
}

function showClassroomMode(name) {
  const next = ['overview', 'normal', 'class', 'exam'].includes(name) ? name : 'overview';
  activeClassroomMode = next;
  document.querySelectorAll('.classroom-mode-panel').forEach(el => el.classList.add('workspace-hidden'));
  const panel = document.getElementById(
    next === 'overview' ? 'classroom-mode-overview' : 'classroom-' + next + '-mode'
  );
  if (panel) panel.classList.remove('workspace-hidden');
  if (next === 'class') showClassroomPhase('before');
  const action = document.getElementById('workspace-primary-action');
  if (action && activeWorkspace === 'classroom') {
    action.textContent = next === 'overview' ? 'Open classroom'
                       : next === 'normal' ? 'Return to modes'
                       : next === 'class' ? 'Start class flow'
                       : 'Return to modes';
  }
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function showClassroomPhase(name) {
  const next = ['before', 'during', 'after'].includes(name) ? name : 'before';
  document.querySelectorAll('.classroom-phase').forEach(el => el.classList.add('workspace-hidden'));
  const panel = document.getElementById('classroom-' + next);
  if (panel) panel.classList.remove('workspace-hidden');
  document.querySelectorAll('[data-classroom-phase]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.classroomPhase === next);
  });
  if (next === 'during') showToast('Session active · WorkPulse surfaces only policy exceptions');
  if (next === 'after') showToast('Baseline restored · raw event retention has started');
}

function approveClassroomResource(button) {
  button.textContent = 'Allowed for this class ✓';
  button.disabled = true;
  showToast('Class-scoped exception recorded');
}

function resolveClassroomRequest(scope) {
  const labels = {
    class: 'Allowed for this class until 3:30 PM',
    once: 'Allowed once for Device 14',
    decline: 'Declined; the current policy remains active',
  };
  classroomDecision = labels[scope] || labels.decline;
  const card = document.getElementById('classroom-request-card');
  if (card) {
    card.innerHTML =
      '<div class="showcase-label">REQUEST RESOLVED</div>' +
      '<h4>' + escapeHtml(classroomDecision) + '</h4>' +
      '<div class="classroom-request-resource"><span class="resource-mark">O</span>' +
      '<p><b>ourworldindata.org</b><small>Scope, lecturer decision and expiry recorded.</small></p></div>' +
      '<div class="classroom-honesty"><b>WorkPulse language</b><span>This is a policy decision, not a judgment about the student.</span></div>';
  }
  const report = document.getElementById('classroom-report-decision');
  if (report) report.textContent = classroomDecision;
  showToast(classroomDecision);
}

function saveClassroomMarker() {
  const button = document.getElementById('classroom-save-marker');
  if (!button) return;
  button.textContent = 'Proposed for course-owner review ✓';
  button.disabled = true;
  showToast('Proposed only · WorkPulse does not silently change policy');
}

function classroomSignalLabel(signal) {
  if (!signal) return 'No recent signal available';
  return [signal.app, signal.domain || signal.title].filter(Boolean).join(' · ');
}

function renderClassroomStatus(status) {
  if (!status) return;
  const active = status.active_session;
  const scheduled = status.scheduled_session;
  const events = status.events || [];
  const activeEvents = active ? events.filter(event => event.session_id === active.id) : [];
  const devices = status.devices || [];
  const signal = status.latest_signal;
  const badge = document.getElementById('classroom-policy-badge');
  const title = document.getElementById('classroom-console-title');
  const time = document.getElementById('classroom-console-time');
  const heading = document.getElementById('classroom-session-heading');
  const copy = document.getElementById('classroom-session-copy');
  const remaining = document.getElementById('classroom-time-remaining');
  const startButton = document.getElementById('classroom-start-now');
  const endButton = document.getElementById('classroom-end-session');
  if (active) {
    const mins = Math.max(0, Math.ceil((new Date(active.ends_at) - Date.now()) / 60000));
    if (badge) badge.textContent = active.mode === 'exam' ? 'Exam policy active' : 'Class policy active';
    if (title) title.textContent = active.title;
    if (time) time.textContent = 'Restores Normal learning automatically at ' + new Date(active.ends_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    if (heading) heading.textContent = active.title + ' is active';
    if (copy) copy.textContent = 'WorkPulse is evaluating enrolled devices against the declared policy. Allowed activity remains quiet; only exceptions require attention.';
    if (remaining) remaining.textContent = mins + 'm';
    if (startButton) startButton.classList.add('workspace-hidden');
    if (endButton) endButton.classList.remove('workspace-hidden');
  } else {
    if (badge) badge.textContent = 'Normal learning';
    if (title) title.textContent = 'Institutional learning policy';
    if (time) time.textContent = scheduled ? 'Next session starts automatically ' + new Date(scheduled.started_at).toLocaleString() : "Enrolled devices follow the institution's baseline access rules";
    if (heading) heading.textContent = 'Normal learning is active';
    if (copy) copy.textContent = 'WorkPulse monitors session status and surfaces exceptions that need attention.';
    if (remaining) remaining.textContent = 'Always';
    if (startButton) startButton.classList.remove('workspace-hidden');
    if (endButton) endButton.classList.add('workspace-hidden');
  }
  const liveSignal = document.getElementById('classroom-live-signal');
  if (liveSignal) liveSignal.textContent = classroomSignalLabel(signal);
  const deviceHealth = document.getElementById('classroom-device-health');
  if (deviceHealth) deviceHealth.textContent = signal
    ? (signal.device_name || signal.device_id) + ' · ' + classroomSignalLabel(signal)
    : 'No enrolled device is reporting';
  const deviceCount = document.getElementById('classroom-device-count');
  if (deviceCount) deviceCount.textContent = String(devices.length);
  const deviceList = document.getElementById('classroom-device-list');
  if (deviceList) {
    deviceList.innerHTML = devices.length ? devices.map(device => {
      const seen = device.last_seen ? new Date(device.last_seen) : null;
      const online = seen && (Date.now() - seen.getTime()) < 30000;
      return '<div class="classroom-enrolled-device"><span class="' + (online ? 'online' : '') + '"></span>' +
        '<p><b>' + escapeHtml(device.name) + '</b><small>' +
        escapeHtml(device.id + ' · ' + device.platform + ' · ' + (online ? 'Connected now' : 'Last seen ' + (seen ? seen.toLocaleTimeString() : 'never'))) +
        '</small><em>' + escapeHtml([device.last_app, device.last_domain].filter(Boolean).join(' · ') || 'Waiting for signal') +
        '</em></p></div>';
    }).join('') : '<p class="classroom-card-copy">No student devices have paired with this console.</p>';
  }
  const eventCount = document.getElementById('classroom-event-count');
  if (eventCount) eventCount.textContent = String(activeEvents.length);
  const decisionCount = document.getElementById('classroom-decision-count');
  if (decisionCount) decisionCount.textContent = String(activeEvents.filter(event => event.severity === 'urgent').length);
  const attention = document.getElementById('classroom-attention-heading');
  if (attention) attention.textContent = activeEvents.length ? activeEvents.length + ' policy exception' + (activeEvents.length === 1 ? '' : 's') : 'No decisions needed';
  const eventBox = document.getElementById('classroom-live-events');
  if (eventBox) {
    eventBox.innerHTML = activeEvents.length ? activeEvents.slice(0, 6).map(event =>
      '<div class="classroom-event-example"><span>!</span><p><b>' +
      '<em class="classroom-event-device">' + escapeHtml(event.device_id || 'Unknown device') + '</em>' +
      escapeHtml(event.rule) + '</b><small>' +
      escapeHtml([
        event.app,
        event.domain,
        (event.occurrence_count || 1) > 1 ? 'Seen ' + event.occurrence_count + ' times' : '',
        new Date(event.last_seen || event.ts).toLocaleTimeString()
      ].filter(Boolean).join(' · ')) +
      '</small></p></div>'
    ).join('') : '<p class="classroom-card-copy">No policy or device-health exception needs attention.</p>';
  }
  const report = document.getElementById('classroom-report-decision');
  if (report) report.textContent = events.length
    ? events.length + ' factual policy event' + (events.length === 1 ? '' : 's') + ' recorded'
    : 'No policy events recorded';
  const reportSession = document.getElementById('classroom-report-session');
  if (reportSession) reportSession.textContent = active
    ? 'Delivered to ' + devices.length + ' enrolled device' + (devices.length === 1 ? '' : 's') +
      ' until ' + new Date(active.ends_at).toLocaleString()
    : scheduled ? 'Scheduled for ' + new Date(scheduled.started_at).toLocaleString()
    : 'Baseline rules delivered to enrolled devices';
  const scheduledSummary = document.getElementById('classroom-scheduled-summary');
  if (scheduledSummary && scheduled) scheduledSummary.textContent =
    scheduled.title + ' will activate automatically at ' + new Date(scheduled.started_at).toLocaleString() +
    ' and restore Normal learning at ' + new Date(scheduled.ends_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) + '.';
  const studentTitle = document.getElementById('classroom-student-title');
  const studentMessage = document.getElementById('classroom-student-message');
  const studentExpiry = document.getElementById('classroom-student-expiry');
  const studentDevice = document.getElementById('classroom-student-device');
  const studentConnection = document.getElementById('classroom-student-connection');
  const selectedDevice = devices[0] || null;
  const selectedSeen = selectedDevice && selectedDevice.last_seen ? new Date(selectedDevice.last_seen) : null;
  const selectedOnline = selectedSeen && (Date.now() - selectedSeen.getTime()) < 30000;
  if (studentDevice) studentDevice.textContent = selectedDevice
    ? selectedDevice.name + ' · ' + selectedDevice.id
    : 'NO DEVICE SELECTED';
  if (studentTitle) studentTitle.textContent = selectedDevice
    ? (active ? active.title : 'Normal learning')
    : 'No enrolled device';
  if (studentMessage) studentMessage.textContent = selectedDevice
    ? (active
      ? 'Use the approved class resources normally. WorkPulse will guide you when a resource falls outside the session policy.'
      : 'You can use this computer normally under the institution’s learning policy.')
    : 'Pair a classroom computer to see its live policy view.';
  if (studentConnection) studentConnection.textContent = selectedDevice
    ? (selectedOnline ? 'Agent connected' : 'Offline · last seen ' + (selectedSeen ? selectedSeen.toLocaleTimeString() : 'never'))
    : 'Waiting for enrolment';
  if (studentExpiry) studentExpiry.textContent = selectedDevice
    ? (active
      ? 'Restores Normal learning at ' + new Date(active.ends_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})
      : 'Normal learning policy')
    : '';
  const studentPolicy = document.getElementById('classroom-student-policy');
  if (studentPolicy) {
    if (!selectedDevice) {
      studentPolicy.innerHTML = '<div><span>i</span><p><b>No live device</b>' +
        '<small>Generate a pairing code and enrol a classroom computer.</small></p><em>Not connected</em></div>';
    } else if (!active) {
      studentPolicy.innerHTML = '<div><span>i</span><p><b>Normal learning</b>' +
        '<small>No timed class policy is active.</small></p><em>Available</em></div>';
    } else {
      const policy = active.policy || {};
      const domains = (policy.allowed_domains || []).slice(0, 4);
      const apps = (policy.allowed_apps || []).slice(0, 4);
      studentPolicy.innerHTML =
        '<div><span>✓</span><p><b>Approved websites</b><small>' +
        escapeHtml(domains.join(' · ') || 'No website allowlist') +
        '</small></p><em>Allowed</em></div>' +
        '<div><span>✓</span><p><b>Approved applications</b><small>' +
        escapeHtml(apps.join(' · ') || 'No application allowlist') +
        '</small></p><em>Allowed</em></div>';
    }
  }
}

async function fetchClassroomStatus() {
  try {
    const response = await fetch('/api/v2/classroom/status');
    if (!response.ok) throw new Error('status ' + response.status);
    const status = await response.json();
    renderClassroomStatus(status);
    return status;
  } catch (error) {
    showToast('Classroom engine is unavailable');
    return null;
  }
}

async function startClassroomSession(mode) {
  const isExam = mode === 'exam';
  const response = await fetch('/api/v2/classroom/session/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      mode: isExam ? 'exam' : 'class',
      title: isExam ? 'Controlled assessment' : ((document.getElementById('classroom-session-name') || {}).value || 'Research Methods'),
      duration_minutes: isExam ? 60 : 90,
    }),
  });
  const status = await response.json();
  if (!response.ok) {
    showToast(status.error || 'Could not start session');
    return;
  }
  renderClassroomStatus(status);
  showToast((isExam ? 'Exam' : 'Class') + ' session started');
}

async function scheduleClassroomSession() {
  const startInput = document.getElementById('classroom-session-start');
  const startValue = startInput && startInput.value;
  if (!startValue) {
    showToast('Choose when the session should start');
    return;
  }
  const response = await fetch('/api/v2/classroom/session/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      mode: 'class',
      title: (document.getElementById('classroom-session-name').value || 'Class session').trim(),
      duration_minutes: Number(document.getElementById('classroom-session-duration').value || 60),
      starts_at: new Date(startValue).toISOString(),
    }),
  });
  const status = await response.json();
  if (!response.ok) {
    showToast(status.error || 'Could not schedule session');
    return;
  }
  renderClassroomStatus(status);
  showToast('Session scheduled · WorkPulse will activate it automatically');
}

async function evaluateClassroomNow() {
  const response = await fetch('/api/v2/classroom/evaluate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}',
  });
  const status = await response.json();
  renderClassroomStatus(status);
  const decision = status.latest_decision && status.latest_decision.decision;
  showToast(decision === 'policy_event' ? 'Policy event recorded' : 'Current signal is allowed');
}

async function endClassroomSession() {
  const response = await fetch('/api/v2/classroom/session/end', {method: 'POST'});
  const status = await response.json();
  renderClassroomStatus(status);
  showToast('Normal learning baseline restored');
}

function syncSidebarHealth() {
  const source = document.getElementById('health-dot');
  const target = document.getElementById('sidebar-health-dot');
  const label = document.getElementById('sidebar-health-label');
  if (!source || !target || !label) return;
  const text = (source.textContent || '').trim();
  const color = getComputedStyle(source).color;
  target.style.background = color;
  label.textContent = text === '●' ? 'Signals available'
                    : (text === '◉' ? 'Snapshot mode' : 'Needs attention');
}

document.addEventListener('DOMContentLoaded', function () {
  const saved = localStorage.getItem('wp_active_workspace') || 'today';
  showWorkspace(saved, { skipPersist: true, instant: true });
  setInterval(syncSidebarHealth, 2500);
  classroomPollTimer = setInterval(function () {
    if (activeWorkspace === 'classroom') {
      fetch('/api/v2/classroom/status')
        .then(r => r.json()).then(renderClassroomStatus).catch(() => {});
    }
  }, 5000);
});
