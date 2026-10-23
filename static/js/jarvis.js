// Jarvis Command Centre: today's missions, recommendation, capture, resources,
// system health and recent actions. ES6 module.
import { applyTheme, h, toast, api, fmtWhen, fmtMinutes, questPrompt, askJarvis, levelBar } from './jarvis-common.js';

applyTheme();

const STATE_LABEL = { ready: 'Ready', in_progress: 'In progress', locked: 'Locked', done: 'Done' };
let today = null;
let campaigns = [];

function allowWeb() { return document.getElementById('allow-web').checked; }

async function act(promise, okMsg) {
  try {
    const out = await promise;
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(out) : okMsg);
    await loadToday();
    return out;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

async function delegate(q) {
  try {
    const sid = await askJarvis(questPrompt(q, q.campaign_title), { questId: q.id, label: q.title, allowWeb: allowWeb() });
    toast('Jarvis is working on it.', false, { href: `/#${sid}`, text: 'Open the chat' });
    await loadToday();
  } catch (e) { toast(e.message, true); }
}

function complete(q) {
  const outcome = prompt(`What actually changed? (completes "${q.title}", +${q.xp_value} XP)`);
  if (outcome === null) return;
  if (!outcome.trim()) { toast('Describe the outcome to earn the XP.', true); return; }
  act(api(`/api/quests/${q.id}/complete`, { method: 'POST', body: { outcome } }), out => `+${out.xp_awarded} XP`);
}

function questButtons(q, { primary = false } = {}) {
  const out = [];
  if (q.status === 'todo' && !q.blocked_by.length) {
    out.push(h('button', { onclick: () => act(api(`/api/quests/${q.id}/start`, { method: 'POST' }), 'Started') }, 'Start'));
  }
  if ((q.status === 'todo' || q.status === 'doing') && !q.blocked_by.length) {
    out.push(h('button', { class: primary ? 'primary' : '', onclick: () => delegate(q),
      title: 'Jarvis works on this in a new chat and logs progress' }, 'Ask Jarvis to do it'));
    out.push(h('button', { onclick: () => complete(q) }, `Complete +${q.xp_value} XP`));
  }
  if (q.agent_session_id) {
    out.push(h('a', { class: 'small muted', href: `/#${q.agent_session_id}` }, 'Jarvis chat →'));
  }
  return out;
}

function renderRecommend() {
  const box = document.getElementById('recommend');
  box.replaceChildren();
  const focus = today.focus_campaign;
  box.append(h('div', { class: 'row' },
    h('span', { class: 'title' }, focus ? focus.title : 'No active campaign'),
    focus ? h('span', { class: 'state ok' }, focus.status) : null,
    focus ? h('span', { class: 'muted small' }, `${focus.done_count}/${focus.quest_count} quests · ${focus.xp} XP`) : null));
  const top = today.next[0];
  if (!top) {
    box.append(h('p', { class: 'empty' }, 'Nothing to recommend yet. Tell Jarvis about a goal in chat, or capture a task below.'));
    return;
  }
  const q = today.missions.find(m => m.id === top.quest_id) || null;
  box.append(
    h('div', { class: 'muted small', style: 'margin-top:10px' }, 'Jarvis recommends'),
    h('div', { class: 'big' }, top.title),
    h('div', { class: 'chips' }, top.reasons.map(r => h('span', { class: 'chip' + (/overdue|due today/.test(r) ? ' hot' : '') }, r))));
  if (q) {
    if (q.est_minutes_effective) box.append(h('div', { class: 'muted small', style: 'margin-top:6px' }, `Estimated: ${fmtMinutes(q.est_minutes_effective)}${q.est_minutes ? '' : ' (rough, from effort)'}`));
    box.append(h('div', { class: 'row', style: 'margin-top:10px' }, questButtons(q, { primary: true })));
  } else {
    box.append(h('div', { class: 'row', style: 'margin-top:10px' }, h('a', { href: '/quests' }, 'Open in the Quest Log →')));
  }
}

function renderMissions() {
  document.getElementById('today-date').textContent = today.date;
  document.getElementById('energy').value = today.plan.energy;
  const m = document.getElementById('minutes');
  if (document.activeElement !== m) m.value = today.plan.minutes_available ?? '';
  const planned = today.plan.minutes_planned;
  const avail = today.plan.minutes_available;
  document.getElementById('planned').textContent =
    planned ? `${fmtMinutes(planned)} planned` + (avail ? ` of ${fmtMinutes(avail)}` : '') : '';
  const box = document.getElementById('missions');
  box.replaceChildren();
  if (!today.missions.length) {
    box.append(h('li', { class: 'empty' }, 'No missions: no open, unblocked quests in active campaigns.'));
    return;
  }
  today.missions.forEach((q, i) => {
    const sub = [];
    if (q.campaign_title) sub.push(q.campaign_title);
    if (q.est_minutes_effective) sub.push(fmtMinutes(q.est_minutes_effective));
    if (q.blocked_by.length) sub.push('after ' + q.blocked_by.map(b => b.title).join(', '));
    if (q.state === 'done' && q.outcome) sub.push('→ ' + q.outcome);
    box.append(h('li', {},
      h('span', { class: 'num' }, String(i + 1).padStart(2, '0')),
      h('div', { class: 'grow' },
        h('div', { class: 'title' }, q.is_boss ? '⚔ ' + q.title : q.title),
        h('div', { class: 'muted small' }, sub.join(' · ')),
        q.state !== 'done' && q.state !== 'locked' ? h('div', { class: 'row', style: 'margin-top:6px' }, questButtons(q)) : null),
      h('span', { class: `state ${q.state}` }, STATE_LABEL[q.state] || q.state)));
  });
}

function renderAttention() {
  const box = document.getElementById('attention');
  box.replaceChildren();
  const groups = [
    ['Overdue', today.overdue, q => `due ${q.due_date}`, 'bad-text'],
    ['Due soon', today.due_soon, q => `due ${q.due_date}`, ''],
    ['Blocked', today.blocked, q => 'waiting on ' + q.blocked_by.map(b => b.title).join(', '), 'muted'],
  ];
  let any = false;
  for (const [label, items, why, cls] of groups) {
    if (!items.length) continue;
    any = true;
    box.append(h('div', { class: 'muted small', style: 'margin:6px 0 2px' }, label));
    box.append(h('ul', { class: 'list' }, items.map(q =>
      h('li', {}, h('div', { class: 'grow' }, h('span', { class: 'title' }, q.title), ' ', h('span', { class: `small ${cls}` }, why(q)))))));
  }
  if (today.ledger_pending) {
    any = true;
    box.append(h('div', { style: 'margin-top:8px' }, h('a', { href: '/ledger' },
      `${today.ledger_pending} memor${today.ledger_pending === 1 ? 'y' : 'ies'} waiting for your review →`)));
  }
  if (!any) box.append(h('span', { class: 'empty' }, 'Nothing overdue, due soon or blocked.'));
  const pill = document.getElementById('ledger-pill');
  pill.hidden = !today.ledger_pending;
  pill.textContent = today.ledger_pending || '';
}

async function loadToday() {
  today = await api('/api/jarvis/today');
  levelBar(today.stats, document.getElementById('lvl'), document.getElementById('xp-bar'), document.getElementById('xp-text'));
  renderRecommend();
  renderMissions();
  renderAttention();
}

async function loadHealth() {
  const box = document.getElementById('health');
  try {
    const s = await api('/api/jarvis/health');
    box.replaceChildren(h('div', { class: s.ok ? 'ok-text' : 'bad-text', style: 'margin-bottom:6px' },
      s.ok ? 'All systems OK' : s.problems.join('; ')));
    const rows = [];
    for (const e of s.llm) {
      const bits = [e.ok ? 'up' : 'DOWN'];
      if (e.latency_ms !== undefined) bits.push(`${e.latency_ms} ms`);
      if (e.n_ctx) bits.push(`ctx ${e.n_ctx}`);
      if (e.busy_slots) bits.push(`${e.busy_slots} busy`);
      rows.push([`LLM ${e.name}`, bits.join(' · '), e.ok]);
      if (e.models && e.models.length) rows.push(['Model', e.models.filter(Boolean).join(', '), true]);
    }
    rows.push(['ChromaDB', s.chroma.ok ? 'up' : 'DOWN', s.chroma.ok]);
    if (s.ram.total_gb) rows.push(['RAM', `${s.ram.free_gb} GB free of ${s.ram.total_gb} GB`, s.ram.ok !== false]);
    if (s.vram && s.vram.used_mib !== undefined) rows.push(['GPU VRAM', `${s.vram.used_mib} MiB in use` + (s.vram.total_mib ? ` of ${s.vram.total_mib}` : ''), true]);
    if (s.disk.total_gb) rows.push(['Disk', `${s.disk.free_gb} GB free`, s.disk.ok !== false]);
    if (s.routing && s.routing.model) rows.push(['Default model', s.routing.model, true]);
    box.append(h('table', {}, h('tbody', {}, rows.map(([k, v, ok]) =>
      h('tr', {}, h('td', { class: 'muted' }, k), h('td', { class: ok ? '' : 'bad-text' }, v))))));
  } catch (e) {
    box.replaceChildren(h('span', { class: 'bad-text' }, `Health check failed: ${e.message}`));
  }
}

async function loadResources() {
  const box = document.getElementById('resources');
  const [s, board] = await Promise.all([api('/api/jarvis/resources'), api('/api/quests/board')]);
  campaigns = board.campaigns;
  const sel = document.querySelector('#resource-form select[name=campaign_id]');
  sel.replaceChildren(h('option', { value: '' }, 'All campaigns'), ...campaigns.map(c => h('option', { value: c.id }, c.title)));
  box.replaceChildren();
  if (!s.resources.length) {
    box.append(h('span', { class: 'empty' }, 'Add a budget or your weekly free hours so plans fit real life.'));
    return;
  }
  for (const b of s.budgets) {
    box.append(h('div', { style: 'margin-bottom:6px' },
      h('div', {}, h('span', { class: 'title' }, b.name), b.campaign_title ? h('span', { class: 'muted' }, ` · ${b.campaign_title}`) : null),
      h('div', { class: b.over ? 'bad-text' : 'muted' },
        `${b.amount ?? '?'} ${b.unit || ''} budget · ${b.planned_spend} planned · ${b.remaining ?? '?'} left` +
        (b.uncosted_quests ? ` · ${b.uncosted_quests} quest${b.uncosted_quests === 1 ? '' : 's'} without a cost` : ''))));
  }
  const other = s.resources.filter(r => r.kind !== 'budget');
  if (s.weekly_hours) box.append(h('div', {}, `Weekly time: ${s.weekly_hours} h · open quests need ~${fmtMinutes(s.open_quest_minutes)}`));
  for (const r of other.filter(r => r.kind !== 'time')) {
    const amt = r.kind === 'savings' && r.target ? `${r.amount ?? 0} / ${r.target} ${r.unit || ''}` : (r.amount ?? '');
    box.append(h('div', { class: 'row' }, h('span', { class: 'chip' }, r.kind), h('span', { class: 'grow' }, r.name, amt !== '' ? ` · ${amt}` : ''),
      h('button', { class: 'ghost', title: 'Remove', onclick: async () => {
        if (!confirm(`Remove "${r.name}"?`)) return;
        try { await api(`/api/jarvis/resources/${r.id}`, { method: 'DELETE' }); loadResources(); } catch (e) { toast(e.message, true); }
      } }, '×')));
  }
}

async function loadActions() {
  const box = document.getElementById('actions');
  const { actions } = await api('/api/jarvis/actions?limit=8');
  box.replaceChildren();
  if (!actions.length) { box.append(h('span', { class: 'empty' }, 'Jarvis hasn’t used any tools yet.')); return; }
  box.append(h('ul', { class: 'list' }, actions.map(a => h('li', {},
    h('span', { class: `state ${a.status}` }, a.status.replace('_', ' ')),
    h('div', { class: 'grow' }, h('div', {}, a.tool + (a.action ? `.${a.action}` : '')),
      h('div', { class: 'muted' }, fmtWhen(a.created_at)))))));
}

// --- wiring
document.getElementById('replan').addEventListener('click', () => {
  const minutes = document.getElementById('minutes').value;
  act(api('/api/jarvis/today', { method: 'POST', body: {
    energy: document.getElementById('energy').value,
    minutes_available: minutes === '' ? 0 : Number(minutes),
  } }), 'Day re-planned');
});

const captureText = () => document.getElementById('capture-text');
async function capture(kind) {
  const text = captureText().value.trim();
  if (!text) { toast('Type something first.', true); return; }
  const out = await act(api('/api/jarvis/capture', { method: 'POST', body: { text, kind } }),
    kind === 'ledger' ? 'Sent to the Review Inbox' : 'Added to your quests');
  if (out) captureText().value = '';
}
document.getElementById('capture').addEventListener('submit', (e) => { e.preventDefault(); capture('quest'); });
document.getElementById('capture-ledger').addEventListener('click', () => capture('ledger'));
document.getElementById('capture-ask').addEventListener('click', async () => {
  const text = captureText().value.trim();
  if (!text) { toast('Type something first.', true); return; }
  try {
    const sid = await askJarvis(text, { label: text, allowWeb: allowWeb() });
    captureText().value = '';
    toast('Jarvis is on it.', false, { href: `/#${sid}`, text: 'Open the chat' });
  } catch (e) { toast(e.message, true); }
});

document.getElementById('resource-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = { kind: f.kind.value, name: f.name.value, campaign_id: f.campaign_id.value || null };
  if (f.amount.value !== '') body.amount = Number(f.amount.value);
  if (f.kind.value === 'budget' || f.kind.value === 'savings') body.unit = 'EUR';
  try {
    await api('/api/jarvis/resources', { method: 'POST', body });
    e.target.reset();
    toast('Resource added');
    loadResources();
  } catch (err) { toast(err.message, true); }
});
document.getElementById('health-refresh').addEventListener('click', loadHealth);

Promise.all([loadToday(), loadResources(), loadActions()]).catch(e => toast(e.message, true));
loadHealth();
