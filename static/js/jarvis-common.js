// Shared helpers for the JARVIS pages (command centre, ledger, actions).
// ES6 module. All user text goes through textContent; nothing is parsed as HTML.

// Reuse the main app's saved theme colours without loading its UI modules.
export function applyTheme() {
  try {
    const saved = JSON.parse(localStorage.getItem('odysseus-theme') || 'null');
    const colors = saved && saved.colors;
    if (!colors) return;
    const s = document.documentElement.style;
    for (const key of ['bg', 'fg', 'panel', 'border', 'red']) {
      if (colors[key]) s.setProperty(`--${key}`, colors[key]);
    }
    if (saved.font) s.setProperty('--font-family', saved.font);
  } catch (e) { /* default palette */ }
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function toast(msg, isErr = false, link = null) {
  let t = document.getElementById('toast');
  if (!t) { t = h('div', { id: 'toast', role: 'status', 'aria-live': 'polite' }); document.body.append(t); }
  t.replaceChildren(msg);
  if (link) t.append(' ', h('a', { href: link.href }, link.text));
  t.className = isErr ? 'err' : '';
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.style.display = 'none'; }, link ? 9000 : (isErr ? 5000 : 2500));
}

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: opts.body && !(opts.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {},
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Not signed in');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${res.status})`);
  }
  return data;
}

export function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function fmtMinutes(m) {
  if (m === null || m === undefined) return '';
  if (m < 60) return `${m} min`;
  const hrs = Math.floor(m / 60), rest = m % 60;
  return rest ? `${hrs} h ${rest} min` : `${hrs} h`;
}

export function questPrompt(q, campaignTitle) {
  const lines = [`Work on this quest from my Quest Log: "${q.title}".`];
  if (campaignTitle) lines.push(`It's part of the campaign "${campaignTitle}".`);
  if (q.notes) lines.push(`Notes: ${q.notes}`);
  lines.push(
    'Do as much of it as you can with the tools you are allowed to use. As you go, record what you did or ' +
    'found with manage_quests action=log_progress on this quest. Do not mark it complete: finish with a short ' +
    'summary of what is done, what is left, and anything you need from me.');
  return lines.join('\n');
}

// AI Task Executor: start a background agent run in a new chat (same pattern
// as Notes' "solve this todo"), link it to the quest, and return the chat id.
export async function askJarvis(prompt, { questId = null, label = 'Jarvis task', allowWeb = false } = {}) {
  const dc = await api('/api/default-chat');
  if (!dc.endpoint_url || !dc.model) throw new Error('No default chat model configured');
  const fd = new FormData();
  fd.append('name', 'Jarvis: ' + label.slice(0, 40));
  fd.append('endpoint_url', dc.endpoint_url);
  fd.append('model', dc.model);
  if (dc.endpoint_id) fd.append('endpoint_id', dc.endpoint_id);
  fd.append('skip_validation', 'true');
  const sess = await api('/api/session', { method: 'POST', body: fd });
  if (questId) {
    await api(`/api/quests/${questId}/delegate`, { method: 'POST', body: { session_id: sess.id } });
  }
  const run = new FormData();
  run.append('message', prompt);
  run.append('session', sess.id);
  run.append('mode', 'agent');
  if (allowWeb) run.append('allow_web_search', 'true');
  // Drain the stream so the server runs the loop to completion and saves it.
  fetch('/api/chat_stream', { method: 'POST', credentials: 'same-origin', body: run })
    .then(async (res) => {
      if (!res.ok || !res.body) return;
      const reader = res.body.getReader();
      while (true) { const { done } = await reader.read(); if (done) break; }
    })
    .catch(() => {});
  return sess.id;
}

export function levelBar(stats, badgeEl, barEl, textEl) {
  const span = stats.next_level_xp - stats.level_floor;
  const pct = span > 0 ? Math.round(100 * (stats.xp - stats.level_floor) / span) : 0;
  badgeEl.textContent = `Lv ${stats.level}`;
  barEl.firstElementChild.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  barEl.setAttribute('aria-valuenow', String(pct));
  textEl.textContent = `${stats.xp} / ${stats.next_level_xp} XP`;
}
