// Quest Log page — campaigns, quests, XP and "next up" recommendations.
// ES6 module. All user text goes through textContent; nothing is parsed as HTML.
import { askJarvis, questPrompt } from './jarvis-common.js';

const API = '/api/quests';
const STATUS_ICON = { todo: '○', doing: '◐', done: '●', dropped: '×' };
const EFFORT = { 1: 'S', 2: 'M', 3: 'L' };

let board = null;
let editingId = null;
let completingId = null;

// --- theme: reuse the main app's saved colors without loading its UI modules
(function applyTheme() {
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
})();

// --- tiny DOM helper
function h(tag, attrs = {}, ...children) {
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

function toast(msg, isErr = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.style.display = 'none'; }, isErr ? 5000 : 2500);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
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

async function act(promise, okMsg, onOk) {
  try {
    const out = await promise;
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(out) : okMsg);
    if (onOk) onOk(out);
    await refresh();
    return out;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

// --- rendering
function campaignName(id) {
  const c = board.campaigns.find(x => x.id === id);
  return c ? c.title : null;
}

function questActions(q) {
  const blocked = q.blocked_by.length > 0;
  const btns = [];
  if (q.status === 'todo' && !blocked) {
    btns.push(h('button', { class: 'ghost', title: 'Start', onclick: () => act(api(`/${q.id}/start`, { method: 'POST' })) }, '▶'));
  }
  if ((q.status === 'todo' || q.status === 'doing') && !blocked) {
    btns.push(h('button', { class: 'ghost', title: 'Ask Jarvis to work on this', onclick: async () => {
      try {
        const sid = await askJarvis(questPrompt(q, campaignName(q.campaign_id)), { questId: q.id, label: q.title });
        toast('Jarvis is working on it. Open it from the chat list.');
        await refresh();
        return sid;
      } catch (e) { toast(e.message, true); }
    } }, '🤖'));
    btns.push(h('button', { class: 'ghost', title: `Complete (+${q.xp_value} XP)`, onclick: () => openComplete(q) }, '✓'));
  }
  if (q.agent_session_id) {
    btns.push(h('a', { class: 'ghost', href: `/#${q.agent_session_id}`, title: 'Open Jarvis chat for this quest' }, '💬'));
  }
  if (q.status === 'todo' || q.status === 'doing') {
    btns.push(h('button', { class: 'ghost', title: 'Drop', onclick: () => {
      if (confirm(`Drop "${q.title}"?`)) act(api(`/${q.id}/drop`, { method: 'POST' }), 'Quest dropped');
    } }, '×'));
  }
  if (q.status === 'done' || q.status === 'dropped') {
    btns.push(h('button', { class: 'ghost', title: 'Reopen (reverses its XP)', onclick: () => {
      if (confirm(`Reopen "${q.title}"? Any XP it earned is reversed.`)) act(api(`/${q.id}/reopen`, { method: 'POST' }), 'Reopened');
    } }, '↺'));
  }
  if (q.status !== 'done') {
    btns.push(h('button', { class: 'ghost', title: 'Delete', onclick: () => {
      if (confirm(`Delete "${q.title}"? This can't be undone.`)) act(api(`/${q.id}`, { method: 'DELETE' }), 'Deleted');
    } }, '🗑'));
  }
  return btns;
}

function questRow(q) {
  const blocked = q.blocked_by.length > 0;
  const sub = [];
  sub.push(`${EFFORT[q.effort]} · ${q.xp_value} XP`);
  if (q.is_boss) sub.push('boss');
  if (q.skill) sub.push(q.skill);
  if (q.due_date && q.status !== 'done') sub.push(`due ${q.due_date}`);
  if (blocked) sub.push('blocked by ' + q.blocked_by.map(b => b.title).join(', '));
  if (q.status === 'done' && q.outcome) sub.push('→ ' + q.outcome);
  return h('li', { class: `${q.status}${blocked && q.status !== 'done' ? ' blocked' : ''}` },
    h('span', { class: 'q-status', title: q.status }, blocked && q.status === 'todo' ? '⊘' : STATUS_ICON[q.status]),
    h('div', { class: 'q-main' },
      h('div', { class: 'q-title', onclick: () => openQuest(q) }, q.is_boss ? '⚔ ' + q.title : q.title),
      h('div', { class: 'q-sub' }, sub.join(' · '))),
    h('div', { class: 'q-actions' }, questActions(q)));
}

function questList(quests) {
  const open = quests.filter(q => q.status === 'todo' || q.status === 'doing');
  const finished = quests.filter(q => q.status === 'done' || q.status === 'dropped');
  const out = [];
  out.push(open.length ? h('ul', { class: 'quests' }, open.map(questRow)) : h('div', { class: 'empty' }, 'No open quests.'));
  if (finished.length) {
    out.push(h('details', { class: 'finished' },
      h('summary', {}, `${finished.length} finished`),
      h('ul', { class: 'quests' }, finished.map(questRow))));
  }
  return out;
}

function renderLevel(stats) {
  const span = stats.next_level_xp - stats.level_floor;
  const pct = span > 0 ? Math.round(100 * (stats.xp - stats.level_floor) / span) : 0;
  document.getElementById('lvl').textContent = `Lv ${stats.level}`;
  const bar = document.getElementById('xp-bar');
  bar.firstElementChild.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  bar.setAttribute('aria-valuenow', String(pct));
  document.getElementById('xp-text').textContent = `${stats.xp} / ${stats.next_level_xp} XP`;
  document.getElementById('done-today').textContent =
    stats.done_today ? `· ${stats.done_today} done today` : '';
}

function renderNext() {
  const box = document.getElementById('next');
  box.replaceChildren();
  if (!board.next.length) {
    const hasOpen = board.quests.some(q => q.status === 'todo' || q.status === 'doing');
    box.append(h('div', { class: 'panel empty' }, hasOpen
      ? 'Everything open is blocked or in a paused campaign.'
      : 'No open quests. Add one to get a recommendation.'));
    return;
  }
  for (const rec of board.next) {
    const q = board.quests.find(x => x.id === rec.quest_id);
    if (!q) continue;
    const hot = new Set(['due today', 'due tomorrow']);
    box.append(h('div', { class: 'panel mission' },
      h('div', { class: 'title' }, q.is_boss ? '⚔ ' + q.title : q.title),
      q.campaign_id ? h('div', { class: 'camp-label' }, campaignName(q.campaign_id)) : null,
      h('div', { class: 'chips' }, rec.reasons.map(r =>
        h('span', { class: 'chip' + (r.startsWith('overdue') || hot.has(r) ? ' hot' : '') }, r))),
      h('div', { class: 'actions' },
        q.status === 'todo' ? h('button', { onclick: () => act(api(`/${q.id}/start`, { method: 'POST' }), 'Started') }, 'Start') : null,
        h('button', { class: 'primary', onclick: () => openComplete(q) }, `Complete +${q.xp_value} XP`))));
  }
}

function renderCampaigns() {
  const box = document.getElementById('campaigns');
  box.replaceChildren();
  for (const c of board.campaigns) {
    const quests = board.quests.filter(q => q.campaign_id === c.id);
    const pct = c.quest_count ? Math.round(100 * c.done_count / c.quest_count) : 0;
    const status = h('select', { class: 'status', title: 'Campaign status', onchange: (e) =>
      act(api(`/campaigns/${c.id}`, { method: 'PUT', body: { status: e.target.value } })) },
      ['active', 'paused', 'done'].map(s => {
        const o = h('option', { value: s }, s);
        if (s === c.status) o.selected = true;
        return o;
      }));
    box.append(h('div', { class: `panel camp ${c.status}` },
      h('div', { class: 'camp-head' },
        h('div', { class: 'name' }, c.title),
        status,
        h('button', { class: 'ghost', title: 'Add quest to this campaign', onclick: () => openQuest(null, c.id) }, '+')),
      h('div', { class: 'camp-meta' },
        `${c.done_count}/${c.quest_count} quests · ${c.xp} XP` + (c.description ? ` · ${c.description}` : '')),
      h('div', { class: 'mini-bar' }, h('span', { style: `width:${pct}%` })),
      questList(quests),
      (!c.quest_count) ? h('button', { class: 'ghost', onclick: () => {
        if (confirm(`Delete campaign "${c.title}"?`)) act(api(`/campaigns/${c.id}`, { method: 'DELETE' }), 'Campaign deleted');
      } }, 'Delete campaign') : null));
  }
  const loose = board.quests.filter(q => !q.campaign_id);
  if (loose.length || !board.campaigns.length) {
    box.append(h('div', { class: 'panel camp' },
      h('div', { class: 'camp-head' },
        h('div', { class: 'name' }, board.campaigns.length ? 'Side quests' : 'Start here'),
        h('button', { class: 'ghost', title: 'Add quest', onclick: () => openQuest(null, '') }, '+')),
      !board.campaigns.length && !loose.length
        ? h('div', { class: 'empty' }, 'Create a campaign for a big goal, then break it into quests.')
        : questList(loose)));
  }
}

function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const EVENT_VERB = {
  created: 'Added', started: 'Started', completed: 'Completed',
  reopened: 'Reopened', dropped: 'Dropped', deleted: 'Deleted',
};

function renderHistory(events) {
  const box = document.getElementById('history');
  box.replaceChildren();
  if (!events.length) { box.append(h('li', { class: 'empty' }, 'Nothing yet.')); return; }
  for (const e of events) {
    const title = e.quest_title || (e.kind === 'deleted' ? e.detail : '(removed quest)');
    const xp = e.xp_delta ? h('span', { class: 'xp' + (e.xp_delta < 0 ? ' neg' : '') }, `${e.xp_delta > 0 ? '+' : ''}${e.xp_delta} XP`) : null;
    box.append(h('li', { title: e.kind === 'completed' && e.detail ? `Outcome: ${e.detail}` : '' },
      h('span', { class: 'when' }, fmtWhen(e.created_at)),
      h('span', {}, `${EVENT_VERB[e.kind] || e.kind}: ${title}`),
      xp));
  }
}

function renderSkills(skills) {
  const box = document.getElementById('skills');
  box.replaceChildren();
  if (!skills.length) { box.append(h('div', { class: 'empty' }, 'Complete quests tagged with a skill to level it.')); return; }
  for (const s of skills) box.append(h('span', { class: 'skill' }, s.skill, ' ', h('b', {}, `${s.xp} XP`)));
}

async function refresh() {
  const [b, hist] = await Promise.all([api('/board'), api('/history?limit=40')]);
  board = b;
  renderLevel(b.stats);
  renderNext();
  renderCampaigns();
  renderHistory(hist.events);
  renderSkills(b.stats.skills);
}

// --- dialogs
function fillCampaignSelect(select, selectedId) {
  select.replaceChildren(h('option', { value: '' }, '— Side quest —'));
  for (const c of board.campaigns) {
    const o = h('option', { value: c.id }, c.title);
    if (c.id === selectedId) o.selected = true;
    select.append(o);
  }
}

function openQuest(q, campaignId = '') {
  const form = document.getElementById('quest-form');
  editingId = q ? q.id : null;
  document.getElementById('quest-dialog-title').textContent = q ? 'Edit quest' : 'New quest';
  form.reset();
  fillCampaignSelect(form.campaign_id, q ? q.campaign_id : campaignId);
  form.elements.title.value = q ? q.title : '';
  form.due_date.value = q && q.due_date ? q.due_date : '';
  form.impact.value = String(q ? q.impact : 2);
  form.effort.value = String(q ? q.effort : 2);
  form.skill.value = q && q.skill ? q.skill : '';
  form.is_boss.checked = !!(q && q.is_boss);
  form.notes.value = q && q.notes ? q.notes : '';
  // Effort and boss status are frozen once XP has been awarded.
  form.effort.disabled = form.is_boss.disabled = !!(q && q.status === 'done');

  const deps = form.depends_on;
  deps.replaceChildren();
  for (const other of board.quests) {
    if (q && other.id === q.id) continue;
    if (other.status === 'dropped') continue;
    const o = h('option', { value: other.id }, (other.status === 'done' ? '✓ ' : '') + other.title);
    if (q && q.depends_on.includes(other.id)) o.selected = true;
    deps.append(o);
  }
  const skills = document.getElementById('skill-list');
  skills.replaceChildren(...[...new Set(board.quests.map(x => x.skill).filter(Boolean))].map(s => h('option', { value: s })));
  document.getElementById('quest-dialog').showModal();
  form.elements.title.focus();
}

function openComplete(q) {
  completingId = q.id;
  const form = document.getElementById('complete-form');
  form.reset();
  document.getElementById('complete-title').textContent = q.title;
  document.getElementById('complete-submit').textContent = `Complete +${q.xp_value} XP`;
  document.getElementById('complete-dialog').showModal();
  form.outcome.focus();
}

document.getElementById('quest-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = {
    title: form.elements.title.value,
    campaign_id: form.campaign_id.value || null,
    due_date: form.due_date.value || null,
    impact: Number(form.impact.value),
    skill: form.skill.value || null,
    notes: form.notes.value || null,
    depends_on: [...form.depends_on.selectedOptions].map(o => o.value),
  };
  if (!form.effort.disabled) {
    body.effort = Number(form.effort.value);
    body.is_boss = form.is_boss.checked;
  }
  const req = editingId
    ? api(`/${editingId}`, { method: 'PUT', body })
    : api('', { method: 'POST', body });
  await act(req, editingId ? 'Quest updated' : 'Quest added',
    () => document.getElementById('quest-dialog').close());
});

document.getElementById('complete-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const outcome = e.target.outcome.value.trim();
  if (!outcome) { toast('Describe what actually changed.', true); return; }
  await act(api(`/${completingId}/complete`, { method: 'POST', body: { outcome } }),
    out => `+${out.xp_awarded} XP`,
    () => document.getElementById('complete-dialog').close());
});

document.getElementById('campaign-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  await act(api('/campaigns', { method: 'POST', body: {
    title: form.elements.title.value, description: form.description.value || null,
  } }), 'Campaign created', () => { form.reset(); document.getElementById('campaign-dialog').close(); });
});

for (const btn of document.querySelectorAll('[data-close]')) {
  btn.addEventListener('click', () => btn.closest('dialog').close());
}
document.getElementById('new-quest').addEventListener('click', () => openQuest(null, ''));
document.getElementById('new-campaign').addEventListener('click', () => {
  document.getElementById('campaign-dialog').showModal();
  document.getElementById('campaign-form').elements.title.focus();
});

refresh().catch(e => toast(e.message, true));
