// Life Ledger review page: approve / reject / edit / merge pending memories,
// browse and correct approved ones. ES6 module.
import { applyTheme, h, toast, api, fmtWhen } from './jarvis-common.js';

applyTheme();

const KINDS = ['decision', 'preference', 'fact', 'reminder', 'inference'];
let entries = [];

async function load() {
  const data = await api('/api/jarvis/ledger');
  entries = data.entries;
  const pill = document.getElementById('ledger-pill');
  pill.hidden = !data.pending;
  pill.textContent = data.pending || '';
  render();
}

async function act(promise, msg) {
  try { await promise; if (msg) toast(msg); await load(); } catch (e) { toast(e.message, true); }
}

function meta(e) {
  const bits = [e.kind, `from ${e.source}`];
  if (e.confidence && e.source !== 'user') bits.push(`${e.confidence} confidence`);
  if (e.source_ref) bits.push(e.source_ref);
  if (e.relevant_until) bits.push(`until ${e.relevant_until}`);
  bits.push(fmtWhen(e.created_at));
  return bits.join(' · ');
}

function pendingCard(e) {
  const text = h('textarea', { maxlength: '1000', 'aria-label': 'Entry text' });
  text.value = e.text;
  const kind = h('select', { 'aria-label': 'Kind' }, KINDS.map(k => {
    const o = h('option', { value: k }, k);
    if (k === e.kind) o.selected = true;
    return o;
  }));
  const card = h('div', { class: 'card pending' },
    text,
    h('div', { class: 'muted small' }, meta(e)),
    e.supersedes_id ? h('div', { class: 'conflict' }, 'Replaces an older entry once approved.') : null);
  if (e.conflicts && e.conflicts.length) {
    card.append(h('div', { class: 'conflict' }, 'Similar approved entries:'));
    for (const c of e.conflicts) {
      card.append(h('div', { class: 'row small' }, h('span', { class: 'grow' }, `“${c.text}”`),
        h('button', { class: 'ghost', title: 'Fold this proposal into that entry',
          onclick: () => act(api(`/api/jarvis/ledger/${e.id}/merge`, { method: 'POST', body: { into_id: c.id } }), 'Merged') },
        'Merge into this')));
    }
  }
  card.append(h('div', { class: 'row' },
    kind,
    h('span', { class: 'spacer' }),
    h('button', { onclick: () => act(api(`/api/jarvis/ledger/${e.id}/review`, { method: 'POST',
      body: { decision: 'reject' } }), 'Rejected') }, 'Reject'),
    h('button', { class: 'primary', onclick: () => act(api(`/api/jarvis/ledger/${e.id}/review`, { method: 'POST',
      body: { decision: 'approve', text: text.value, kind: kind.value } }), 'Approved') }, 'Approve')));
  return card;
}

function render() {
  const inbox = document.getElementById('inbox');
  const pending = entries.filter(e => e.status === 'pending');
  inbox.replaceChildren(...(pending.length ? pending.map(pendingCard)
    : [h('div', { class: 'panel empty' }, 'Inbox clear. When Jarvis notices a decision or preference worth keeping, it lands here.')]));

  const filter = document.getElementById('filter').value.trim().toLowerCase();
  const approved = entries.filter(e => e.status === 'approved' && (!filter || e.text.toLowerCase().includes(filter)));
  const list = document.getElementById('approved');
  list.replaceChildren(...(approved.length ? approved.map(e => h('li', {},
    h('div', { class: 'grow' }, h('div', {}, e.text), h('div', { class: 'muted small' }, meta(e))),
    h('button', { class: 'ghost', title: 'Correct (keeps the old version in history)', onclick: () => {
      const text = prompt('Corrected version:', e.text);
      if (text === null || !text.trim() || text.trim() === e.text) return;
      act(api(`/api/jarvis/ledger/${e.id}/correct`, { method: 'POST', body: { text } }), 'Corrected');
    } }, 'Correct'))) : [h('li', { class: 'empty' }, filter ? 'No matches.' : 'Nothing approved yet.')]));

  const old = entries.filter(e => e.status === 'rejected' || e.status === 'superseded');
  document.getElementById('history').replaceChildren(...(old.length ? old.map(e => h('li', {},
    h('span', { class: `state ${e.status === 'rejected' ? 'error' : 'done'}` }, e.status),
    h('div', { class: 'grow' }, h('div', {}, e.text), h('div', { class: 'muted' }, meta(e)))))
    : [h('li', { class: 'empty' }, 'No history yet.')]));
}

document.getElementById('filter').addEventListener('input', render);
document.getElementById('add').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  const body = { text: f.text.value, kind: f.kind.value };
  if (f.relevant_until.value) body.relevant_until = f.relevant_until.value;
  await act(api('/api/jarvis/ledger', { method: 'POST', body }), 'Saved');
  ev.target.reset();
});

load().catch(e => toast(e.message, true));
