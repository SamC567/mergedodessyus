// Action history + permission centre. ES6 module.
import { applyTheme, h, toast, api, fmtWhen } from './jarvis-common.js';

applyTheme();

let toolState = new Map();  // id -> enabled
let hiddenDisabled = [];    // disabled names not in the toggle list (e.g. MCP tools) - kept as-is on save

async function loadActions() {
  const status = document.getElementById('status').value;
  const tool = document.getElementById('tool').value;
  const qs = new URLSearchParams({ limit: '200' });
  if (status) qs.set('status', status);
  if (tool) qs.set('tool', tool);
  const { actions } = await api('/api/jarvis/actions?' + qs);
  const body = document.getElementById('rows');
  body.replaceChildren(...(actions.length ? actions.map(a => h('tr', {},
    h('td', { style: 'white-space:nowrap' }, fmtWhen(a.created_at)),
    h('td', {}, a.tool + (a.action ? `.${a.action}` : '')),
    h('td', {}, h('span', { class: `state ${a.status}` }, a.status.replace('_', ' '))),
    h('td', { class: 'mono' }, [a.detail, a.summary].filter(Boolean).join(' — ')),
    h('td', { class: 'muted' }, a.duration_ms != null ? `${a.duration_ms} ms` : '')))
    : [h('tr', {}, h('td', { colspan: '5', class: 'empty' }, 'No actions recorded yet.'))]));
}

async function loadPermissions() {
  const p = await api('/api/jarvis/permissions');
  document.getElementById('policy-summary').textContent = p.approval_policy.summary;
  const rows = Object.entries(p.approval_policy.tools).sort(([a], [b]) => a.localeCompare(b));
  document.getElementById('policy').replaceChildren(...rows.map(([tool, t]) => {
    const asks = [...t.reads, ...t.writes].filter(x => !t.no_prompt.includes(x) && !t.no_prompt.includes('*'));
    return h('tr', {},
      h('td', {}, tool),
      h('td', {}, t.no_prompt.length ? t.no_prompt.join(', ') : h('span', { class: 'muted' }, '—')),
      h('td', {}, asks.map(a => h('span', { class: 'chip' + (t.destructive.includes(a) ? ' hot' : ''),
        title: t.destructive.includes(a) ? 'destructive' : '' }, a))));
  }));
  document.getElementById('privacy').textContent =
    (p.cloud_privacy_guard ? 'Cloud privacy guard is ON: ' : 'Cloud privacy guard is OFF: ') +
    `${p.cloud_private_tools.length} private tools (quests, ledger, memory, email, files…) are never offered to cloud models.`;

  const att = document.getElementById('attention');
  att.replaceChildren(...(p.recent_attention.length ? [h('ul', { class: 'list' }, p.recent_attention.slice(0, 8).map(a =>
    h('li', {}, h('span', { class: `state ${a.status}` }, a.status.replace('_', ' ')),
      h('div', { class: 'grow' }, h('div', {}, a.tool + (a.action ? `.${a.action}` : '')),
        h('div', { class: 'muted' }, [fmtWhen(a.created_at), a.detail].filter(Boolean).join(' · '))))))]
    : [h('span', { class: 'empty' }, 'No recent errors, blocks or approval requests.')]));

  const toolSel = document.getElementById('tool');
  const current = toolSel.value;
  toolSel.replaceChildren(h('option', { value: '' }, 'All tools'), ...p.tools.map(t => h('option', { value: t.id }, t.id)));
  toolSel.value = current;
  toolState = new Map(p.tools.map(t => [t.id, t.enabled]));
  hiddenDisabled = p.disabled.filter(id => !toolState.has(id));
  renderToggles();
}

function renderToggles() {
  const f = document.getElementById('tool-filter').value.trim().toLowerCase();
  document.getElementById('toggles').replaceChildren(...[...toolState.entries()]
    .filter(([id]) => !f || id.includes(f))
    .map(([id, on]) => {
      const box = h('input', { type: 'checkbox', 'aria-label': id });
      box.checked = on;
      box.addEventListener('change', () => toolState.set(id, box.checked));
      return h('label', { class: 'inline' }, box, id);
    }));
}

document.getElementById('status').addEventListener('change', () => loadActions().catch(e => toast(e.message, true)));
document.getElementById('tool').addEventListener('change', () => loadActions().catch(e => toast(e.message, true)));
document.getElementById('tool-filter').addEventListener('input', renderToggles);
document.getElementById('save-tools').addEventListener('click', async () => {
  const disabled = [...hiddenDisabled, ...[...toolState.entries()].filter(([, on]) => !on).map(([id]) => id)];
  try {
    await api('/api/tools', { method: 'POST', body: { disabled } });
    toast(`Saved: ${disabled.length} tools off`);
    await loadPermissions();
  } catch (e) { toast(e.message, true); }
});

Promise.all([loadActions(), loadPermissions()]).catch(e => toast(e.message, true));
