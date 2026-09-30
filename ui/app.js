/* Local-only renderer. Conversation text is always inserted as text, never HTML. */
const api = window.manager;
const $ = selector => document.querySelector(selector);
const providers = { codex: 'Codex', claude: 'Claude', gemini: 'Gemini CLI', antigravity: 'Antigravity' };
const state = { library: { contexts: [], locations: [] }, view: 'all', provider: 'all', host: 'all', query: '', selected: new Set(), active: null, tab: 'messages', messages: [], cursor: null, tools: false, compactHeader: true, generation: 0, busy: false };
const titles = { usage: 'Provider usage', setup: 'AI setup', catchup: 'Catch up', all: 'All contexts', starred: 'Starred', imported: 'Imported contexts', archived: 'Archived contexts', locations: 'Context locations', cloud: 'Cloud sync', transfer: 'Backup & teleport' };
const descriptions = { usage: 'Your remaining allowance and the next reset, reported by each provider.', setup: 'Install your AI assistant, sign in, and get back to your projects.', catchup: 'Your projects kept moving. Find your place in them again.', all: 'Pick up the thread. Every conversation, in one place.', starred: 'The conversations you want to keep close.', imported: 'Conversations brought over from another manager.', archived: 'Out of the way. Still here when you need them.', locations: 'See local stores and SSH hosts. Resume on the machine that owns the context.', cloud: 'Connect an account. Choose what travels with you.', transfer: 'Your work travels with you. Pack it up and pick it up anywhere.' };
function element(tag, className, text) { const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = text; return e; }
function button(text, className, handler) { const b = element('button', className, text); b.addEventListener('click', () => run(handler)); return b; }
function bytes(n) { if (!n) return '0 B'; const i = Math.min(3, Math.floor(Math.log(n) / Math.log(1024))); return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${['B', 'KB', 'MB', 'GB'][i]}`; }
function date(value) { const d = new Date(value); return Number.isNaN(+d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
function toast(text, error = false) { const e = $('#toast'); e.textContent = text; e.className = error ? 'error' : ''; clearTimeout(toast.timer); toast.timer = setTimeout(() => e.className = 'hidden', error ? 11000 : 6500); }
async function run(fn) { try { return await fn(); } catch (e) { toast(e.message.replace(/^Error invoking remote method '[^']+': Error: /, ''), true); } }
async function operation(label, fn) {
  if (state.busy) return toast('Another file operation is still running.');
  state.busy = true; $('#status').textContent = label;
  try { return await fn(); } finally { state.busy = false; updateStatus(); }
}
function updateStatus() { if (state.library.machine) { document.querySelector('.workspace').title = `Local execution: ${state.library.machine.name} · ${state.library.machine.home}`; document.querySelector('.breadcrumb').firstChild.textContent = state.library.machine.name + ' '; } $('#status').textContent = `${state.library.contexts.length} contexts · ${state.library.locations.filter(x => x.exists).length} connected stores${state.library.warnings?.length ? ` · ${state.library.warnings.length} scan warnings` : ''}`; $('#status').title = (state.library.warnings || []).join('\n'); }
function updateLibrary(library) {
  state.library = library;
  state.selected = new Set([...state.selected].filter(id => library.contexts.some(c => c.id === id)));
  if (state.active && !library.contexts.some(c => c.id === state.active)) state.active = null;
  render(); updateStatus();
}
function filtered() {
  const query = state.query.toLowerCase();
  return state.library.contexts.filter(c => {
    if (state.provider !== 'all' && c.provider !== state.provider) return false;
    if (state.host === 'local' && c.origin === 'remote') return false;
    if (state.host === 'ssh' && c.origin !== 'remote') return false;
    if (!['all', 'local', 'ssh'].includes(state.host) && (c.origin !== 'remote' || c.endpointId !== state.host)) return false;
    if (state.view === 'archived' ? !c.archived : c.archived) return false;
    if (state.view === 'starred' && !c.starred) return false;
    if (state.view === 'imported' && c.origin !== 'imported') return false;
    return [c.title, c.project, c.sessionId, c.provider, ...(c.tags || [])].join(' ').toLowerCase().includes(query);
  }).sort((a, b) => ({ recent: () => b.updated.localeCompare(a.updated), oldest: () => a.updated.localeCompare(b.updated), size: () => b.size - a.size, title: () => a.title.localeCompare(b.title) })[$('#sort').value]());
}
function providerLabel(provider) { const span = element('span', 'provider-label'); span.append(element('i', `provider-dot ${provider}`), document.createTextNode(providers[provider])); return span; }
function renderHostFilter() {
  const select = $('#host-filter'), endpoints = state.library.sshEndpoints || [];
  if (!['all', 'local', 'ssh'].includes(state.host) && !endpoints.some(e => e.id === state.host)) state.host = 'all';
  select.replaceChildren();
  const options = [['all', 'All hosts'], ['local', 'This machine'], ['ssh', 'All SSH hosts'], ...endpoints.map(e => [e.id, `${e.label && e.label !== e.host ? e.label + ' · ' : ''}${e.host}${e.connected ? '' : ' (offline)'}`])];
  for (const [value, label] of options) { const option = element('option', '', label); option.value = value; select.append(option); }
  select.value = state.host;
}
function render() {
  $('#crumb').textContent = titles[state.view]; $('#page-title').textContent = titles[state.view]; $('#page-description').textContent = descriptions[state.view];
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b.dataset.view === state.view));
  const library = !['locations', 'transfer', 'cloud', 'catchup', 'setup', 'usage'].includes(state.view);
  $('#library-view').classList.toggle('hidden', !library); $('#stats').classList.toggle('hidden', !library);
  $('#cloud-view').classList.toggle('hidden', state.view !== 'cloud');
  $('#catchup-view').classList.toggle('hidden', state.view !== 'catchup');
  $('#usage-view').classList.toggle('hidden', state.view !== 'usage');
  if (state.view === 'usage') loadUsage(true);
  $('#setup-view').classList.toggle('hidden', state.view !== 'setup');
  if (state.view === 'setup') loadSetup(true);
  if (state.view === 'catchup') renderCatchUp();
  $('#locations-view').classList.toggle('hidden', state.view !== 'locations'); $('#transfer-view').classList.toggle('hidden', state.view !== 'transfer');
  $('#nav-total').textContent = state.library.contexts.filter(c => !c.archived).length;
  const stats = $('#stats'); stats.replaceChildren();
  for (const [id, name] of Object.entries(providers)) {
    const items = state.library.contexts.filter(c => c.provider === id), card = element('div', 'stat'), top = element('div', 'stat-top', name);
    top.append(element('i', `provider-dot ${id}`)); const count = element('div', 'stat-value', String(items.length)); count.append(element('small', '', bytes(items.reduce((n, c) => n + c.size, 0)))); card.append(top, count); stats.append(card);
  }
  const pills = $('#provider-filters'); pills.replaceChildren();
  for (const [id, name] of [['all', 'All providers'], ...Object.entries(providers)]) pills.append(button(name, `pill ${state.provider === id ? 'active' : ''}`, () => { state.provider = id; render(); }));
  renderHostFilter(); renderRows(); renderLocations(); renderTransfer(); renderCloud(); updateSelection();
}
function updateSelection() {
  $('#selected-count').textContent = state.selected.size; $('#export-button').disabled = state.selected.size === 0;
  const list = filtered(), chosen = list.filter(c => state.selected.has(c.id)).length;
  $('#select-all').checked = list.length > 0 && chosen === list.length; $('#select-all').indeterminate = chosen > 0 && chosen < list.length;
}
function renderRows() {
  const rows = $('#context-rows'); rows.replaceChildren(); const list = filtered(); $('#result-count').textContent = `${list.length} context${list.length === 1 ? '' : 's'}`;
  if (!list.length) { const empty = element('div', 'empty'); empty.append(element('h2', '', 'No contexts here yet.'), element('p', '', state.query || state.provider !== 'all' || state.host !== 'all' ? 'Try another search, provider, or host filter.' : 'Connect a context location or import a portable archive.')); rows.append(empty); return; }
  for (const c of list) {
    const row = element('div', `context-row ${state.active === c.id ? 'selected' : ''}`); row.tabIndex = 0; row.setAttribute('role', 'button'); row.setAttribute('aria-label', `Inspect ${c.title}`);
    row.onclick = () => run(() => selectContext(c.id)); row.onkeydown = e => { if (e.target === row && ['Enter', ' '].includes(e.key)) { e.preventDefault(); run(() => selectContext(c.id)); } };
    const check = element('input'); check.type = 'checkbox'; check.checked = state.selected.has(c.id); check.setAttribute('aria-label', `Select ${c.title}`); check.onclick = e => e.stopPropagation(); check.onchange = () => { check.checked ? state.selected.add(c.id) : state.selected.delete(c.id); updateSelection(); };
    const content = element('div', 'row-content'), title = element('div', 'row-title'); title.append(element('h3', '', `${c.starred ? '★ ' : ''}${c.title}`), element('time', '', date(c.updated)));
    const meta = element('div', 'row-meta'); meta.append(providerLabel(c.provider), document.createTextNode(' / '), element('span', 'project', c.project ? c.project.split(/[\\/]/).filter(Boolean).pop() : 'Project not recorded')); meta.title = c.project;
    const bottom = element('div', 'row-bottom'); bottom.append(element('span', '', bytes(c.size)), element('span', 'tag', `${c.origin === 'remote' ? 'SSH · ' : ''}${c.machine || 'This machine'}`)); if (c.copies?.length) bottom.append(element('span', 'tag', 'Multiple copies')); if (c.offline) bottom.append(element('span', 'tag', 'Offline')); if (c.origin === 'imported') bottom.append(element('span', 'tag', 'imported')); for (const tag of (c.tags || []).slice(0, 3)) bottom.append(element('span', 'tag', tag));
    content.append(title, meta, bottom); row.append(check, content); rows.append(row);
  }
}
async function selectContext(id) { state.active = id; state.compactHeader = true; state.tab = 'messages'; state.messages = []; state.cursor = null; const generation = ++state.generation; renderRows(); renderInspector(true); const detail = await api.detail({ id, direction: 'older' }); if (state.generation !== generation) return; state.messages = detail.messages; state.cursor = detail.next; state.notice = detail.notice || (detail.skipped ? `${detail.skipped} malformed or oversized records skipped; originals remain intact.` : ''); renderInspector(); const scroll = $('#inspector .inspect-body'); scroll.setScrollPosition(scroll.scrollHeight); }
function activeContext() { return state.library.contexts.find(c => c.id === state.active); }
function setInspectorCompact(compact) {
  const pane = $('#inspector'), body = pane.querySelector('.inspect-body');
  if (!body || compact === state.compactHeader) return;
  const top = body.getBoundingClientRect().top, position = body.scrollTop;
  const atBottom = body.scrollHeight - body.clientHeight - position < 2;
  state.compactHeader = compact;
  pane.classList.toggle('compact-header', compact);
  const toggle = pane.querySelector('.inspector-toggle');
  toggle.textContent = compact ? 'Show details ▾' : 'Hide details ▴';
  toggle.setAttribute('aria-expanded', String(!compact));
  body.setScrollPosition(atBottom ? body.scrollHeight : position + body.getBoundingClientRect().top - top);
}
function bindInspectorScroll(body) {
  let last = body.scrollTop, travel = 0;
  body.setScrollPosition = position => { body.scrollTop = position; last = body.scrollTop; travel = 0; };
  body.addEventListener('scroll', () => {
    if (!body.isConnected) return;
    const current = body.scrollTop, delta = current - last;
    last = current;
    if (!delta) return;
    travel = Math.sign(travel) === Math.sign(delta) ? travel + delta : delta;
    if (travel < -18 || (current === 0 && delta < 0)) setInspectorCompact(false);
    else if (travel > 18 && current > 40) setInspectorCompact(true);
  }, { passive: true });
}
function renderInspector(loading = false) {
  const c = activeContext(); if (!c) return;
  const pane = $('#inspector'), previousScroll = pane.querySelector('.inspect-body')?.scrollTop || 0; pane.replaceChildren(); pane.classList.toggle('compact-header', state.compactHeader);
  const head = element('div', 'inspect-head'), top = element('div', 'inspect-top'); top.append(providerLabel(c.provider)); if (c.origin !== 'remote') top.append(button(c.starred ? '★ Starred' : '☆ Star', 'quiet', async () => { updateLibrary(await api.annotate({ id: c.id, starred: !c.starred })); renderInspector(); }));
  const heading = element('div', 'inspect-heading'), title = element('h2', '', c.title); title.title = c.title;
  const toggle = button(state.compactHeader ? 'Show details ▾' : 'Hide details ▴', 'quiet inspector-toggle', () => setInspectorCompact(!state.compactHeader)); toggle.setAttribute('aria-expanded', String(!state.compactHeader));
  heading.append(title, toggle); head.append(top, heading, element('div', 'project-path', c.project || 'Working directory not recorded'));
  const actions = element('div', 'inspect-actions');
  if (c.handoff) {
    const returns = state.library.contexts.filter(other => c.origin === 'local' && other.origin === 'remote' && !other.handoff && !other.offline && other.provider === c.provider && other.sessionId === c.sessionId && (c.handoff.targetMachineId ? other.machineId === c.handoff.targetMachineId : other.machine === c.handoff.target));
    if (returns.length === 1) actions.append(button(`⇥ Resume here from ${returns[0].machine}`, 'button primary', () => resumeHere(returns[0].id)));
    actions.append(button('Release handoff…', 'button', async () => { const lib = await api.handoffRelease(c.id); if (lib) { updateLibrary(lib); renderInspector(); } })); } else if (['local', 'remote'].includes(c.origin)) {
    actions.append(button(c.origin === 'remote' ? '▶ Resume remotely' : '▶ Chat here', 'button primary', () => openTerminal(c.id)), button('↗ Terminal', 'button', async () => { await api.resumeExternal(c.id); toast('Conversation opened in your terminal.'); }));
  } else actions.append(button('Restore files', 'button', () => operation('Restoring context files…', async () => { const result = await api.restoreContext(c.id); if (result) toast(`${result.count} files restored to ${result.path}`); })));
  if (c.origin === 'remote' && !c.handoff) actions.append(button('⇥ Resume here', 'button', () => resumeHere(c.id)));
  else if (c.origin !== 'remote') actions.append(button('Show file', 'button', () => api.revealContext(c.id)), button('Export', 'button', () => exportContexts([c.id])));
  head.append(element('p', 'machine-label', `${c.origin === 'remote' ? 'SSH · ' : 'This machine · '}${c.machine || ''}${c.offline ? ' · Offline snapshot' : ''}`)); head.append(actions); pane.append(head);
  const tabs = element('div', 'tabs'); for (const [id, title] of [['messages', 'Conversation'], ['files', 'Original files'], ['metadata', 'Details & notes']]) tabs.append(button(title, `tab ${state.tab === id ? 'active' : ''}`, () => { state.tab = id; renderInspector(); const scroll = pane.querySelector('.inspect-body'); scroll.setScrollPosition(id === 'messages' ? scroll.scrollHeight : 0); })); pane.append(tabs);
  const body = element('div', 'inspect-body'); body.tabIndex = 0; body.setAttribute('role', 'region'); body.setAttribute('aria-label', state.tab === 'messages' ? 'Conversation messages' : 'Context details'); pane.append(body); bindInspectorScroll(body);
  if (loading) { body.append(element('div', 'empty', 'Loading conversation…')); return; }
  if (c.handoff) body.append(element('p', 'notice', `Handed off to ${c.handoff.target}. To bring back the latest history, stop the session there and choose Resume here on its SSH copy. Refresh Context locations if that copy is missing. Release handoff only reopens this retained, older copy.`)); if (c.copies?.length) body.append(element('p', 'notice', 'Other copies exist on ' + c.copies.join(', ') + '. Avoid running the same conversation in multiple places.'));
  if (state.tab === 'messages') renderMessages(body);
  if (state.tab === 'files') {
    body.append(element('p', '', 'Original files included when you export this context.'));
    api.files({ id: c.id }).then(files => { if (state.active !== c.id || state.tab !== 'files') return; for (const f of files) { const row = element('div', 'file'); row.append(element('code', '', f.relative), element('small', '', bytes(f.size))); body.append(row); } }).catch(e => toast(e.message, true));
  }
  if (state.tab === 'metadata') renderMetadata(body, c);
  body.setScrollPosition(previousScroll);
}
function renderMessages(body) {
  const controls = element('div', 'detail-controls'), label = element('label'), check = element('input'); check.type = 'checkbox'; check.checked = state.tools; check.onchange = () => { state.tools = check.checked; renderInspector(); }; label.append(check, document.createTextNode('Show tools & system messages')); controls.append(label, element('span', '', `${state.messages.length} loaded`)); body.append(controls);
  if (state.notice) body.append(element('div', 'notice', state.notice));
  if (state.cursor !== null) { const older = element('div', 'pagination'); const b = button('↑ Load older messages', 'button', async () => {
    b.disabled = true; const generation = state.generation, c = activeContext();
    try {
      const detail = await api.detail({ id: c.id, cursor: state.cursor, direction: 'older' });
      if (generation !== state.generation) return;
      state.messages.unshift(...detail.messages); state.cursor = detail.next;
      if (state.tab !== 'messages') return;
      const previous = $('#inspector .inspect-body'), distanceFromBottom = previous.scrollHeight - previous.scrollTop;
      renderInspector(); const scroll = $('#inspector .inspect-body'); scroll.setScrollPosition(scroll.scrollHeight - distanceFromBottom);
    } finally { b.disabled = false; }
  }); older.append(b); body.append(older); }
  const visible = state.messages.filter(m => state.tools || ['user', 'assistant'].includes(m.role));
  for (const msg of visible) {
    const item = element('article', `message ${msg.role}`), meta = element('div', 'message-meta'); meta.append(element('span', '', msg.role), element('time', '', date(msg.timestamp))); item.append(meta);
    const content = element('pre', '', msg.content + (msg.truncated ? '\n\n[Display truncated; full content is in the original file.]' : ''));
    if (!['user', 'assistant'].includes(msg.role)) { const details = element('details'); details.append(element('summary', '', msg.kind || 'Expand content'), content); item.append(details); } else item.append(content);
    body.append(item);
  }
  if (!visible.length) body.append(element('p', '', state.cursor !== null ? 'No visible messages on this page. Load older messages or show system and tool messages.' : 'No readable conversation messages found. You can inspect and export the original files.'));

}
function renderMetadata(body, c) {
  if (c.origin === 'remote') { body.append(element('p', 'notice', `Stored on ${c.machine}: ${c.path}. Remote browsing is read-only. Resume remotely to keep working there, or choose Resume here for a handoff.`)); return; }
  for (const [name, value] of [['Session ID', c.sessionId], ['Source file', c.path], ['Provider root', c.root], ['Model', c.model || 'Not recorded'], ['Updated', c.updated]]) { const field = element('div', 'field'); field.append(element('label', '', name), element('div', 'meta-value', value)); body.append(field); }
  const folderActions = element('div', 'field folder-actions');
  folderActions.append(element('label', '', 'Move to another folder'), element('p', 'muted', 'Change the folder used when launching this context, or relocate its original files. Project source files and historical path references are not rewritten.'));
  folderActions.append(button('Change project folder…', 'button', async () => { const library = await api.changeProject(c.id); if (library) { updateLibrary(library); renderInspector(); toast('Project folder updated for future resumes.'); } }));
  if (c.origin === 'local') folderActions.append(button('Move original files…', 'button', () => operation('Moving context files with a recovery backup…', async () => {
    const result = await api.moveContextFiles(c.id); if (!result) return;
    state.selected.delete(c.id); updateLibrary(result.library); await selectContext(result.id);
    toast(`${result.count} context files moved. Recovery archive: ${result.backup}`);
  })));
  if (c.moveBackup) folderActions.append(element('p', 'meta-value', 'Recovery archive: ' + c.moveBackup));
  body.append(folderActions);
  const form = element('form'); const fields = {};
  for (const [name, key, value] of [['Display title', 'title', c.title], ['Tags (comma separated)', 'tags', (c.tags || []).join(', ')], ['Private notes', 'notes', c.notes || '']]) { const field = element('div', 'field'), input = element(key === 'notes' ? 'textarea' : 'input'); input.id = `edit-${key}`; input.value = value; const label = element('label', '', name); label.htmlFor = input.id; field.append(label, input); form.append(field); fields[key] = input; }
  const save = element('button', 'button primary', 'Save details'); save.type = 'submit'; form.append(save); form.onsubmit = e => { e.preventDefault(); run(async () => { updateLibrary(await api.annotate({ id: c.id, title: fields.title.value || c.title, notes: fields.notes.value, tags: fields.tags.value.split(',') })); renderInspector(); toast('Context details saved.'); }); }; body.append(form);
  body.append(element('p', 'muted', 'Library archive status only affects GPT Manager.'));
  body.append(button(c.archived ? 'Unarchive context' : 'Archive context', 'button', async () => { updateLibrary(await api.annotate({ id: c.id, archived: !c.archived })); renderInspector(); }));
}
function renderLocations() {
  const page = $('#locations-view'); page.replaceChildren(); const intro = element('div', 'section-intro'); intro.append(element('h2', '', 'Connected stores')); const controls = element('div'), select = element('select'); select.setAttribute('aria-label', 'Provider for custom location'); for (const [id, name] of Object.entries(providers)) { const option = element('option', '', name); option.value = id; select.append(option); } controls.append(select, document.createTextNode(' '), button('+ Add location', 'button', async () => { const lib = await api.addRoot(select.value); if (lib) updateLibrary(lib); })); intro.append(controls); page.append(intro);
  renderSshLocations(page);
  for (const loc of state.library.locations) { const card = element('div', 'location-card'), content = element('div'), h = element('h3'); h.append(providerLabel(loc.provider)); content.append(h, element('p', 'muted', loc.machine || state.library.machine?.name || 'This machine'), element('code', '', loc.path)); card.append(content, element('span', `location-status ${loc.exists ? '' : 'missing'}`, loc.exists ? `${loc.count} contexts · connected` : 'Not found')); page.append(card); }
  page.append(element('p', 'muted', `Manager data: ${state.library.dataDir || ''}`));
  page.append(element('p', 'muted', 'Store roots: Codex home · Claude projects directory · Gemini tmp directory · Antigravity data directory. Discovery is read-only.'));
}
function renderTransfer() {
  const page = $('#transfer-view'); page.replaceChildren(); const grid = element('div', 'transfer-grid');
  for (const [number, title, description, action, handler] of [['01 / DEPARTURE', 'Pack your context.', 'Select conversations from any provider. Export original context files, metadata, and your notes in one checked, portable archive.', 'Choose contexts →', () => { state.view = 'all'; render(); }], ['02 / ARRIVAL', 'Pick up the thread.', 'Open an archive on another GPT Manager. Inspect its contents, import to your library, then restore original files when you’re ready.', 'Import an archive →', importPreview]]) {
    const card = element('div', 'transfer-card'); card.append(element('span', 'step', number), element('h2', '', title), element('p', '', description), button(action, 'button primary', handler)); grid.append(card);
  }
  page.append(grid); const note = element('div', 'transfer-note'); note.append(element('strong', '', 'A portable archive. A deliberate restore.'), element('div', '', 'Send the .gptctx file by a channel you trust. It contains private chats and is not encrypted. Import verifies checksums and keeps a separate library copy. Restore never replaces existing files. Project source, credentials, global provider indexes, and settings are not included. Native resume may need provider-specific setup; contexts are not converted between providers.')); page.append(note);
}
async function exportContexts(ids) { if (ids.some(id => state.library.contexts.find(c => c.id === id)?.origin === 'remote')) throw new Error('For SSH contexts, use Resume here to hand off the conversation, or export on the source machine.'); return operation('Packing original files and computing checksums…', async () => { const result = await api.exportBundle(ids); if (result) toast(`Exported ${result.count} contexts · ${bytes(result.size)} · ${result.path}`); }); }
async function importPreview() { await operation('Validating archive and checksums…', async () => { const preview = await api.chooseImport(); if (!preview) return; const body = $('#import-preview'); body.replaceChildren(element('div', 'muted', `${preview.contexts.length} contexts · ${preview.files} files · ${bytes(preview.size)}`)); for (const c of preview.contexts) body.append(element('div', 'preview-row', `${providers[c.provider]} / ${c.title}`)); $('#import-dialog').showModal(); }); }
$('#navigation').addEventListener('click', e => { const button = e.target.closest('[data-view]'); if (button) { state.view = button.dataset.view; render(); } });
$('#search').oninput = e => { state.query = e.target.value; renderRows(); updateSelection(); };
$('#sort').onchange = () => renderRows();
$('#host-filter').onchange = e => { state.host = e.target.value; renderRows(); updateSelection(); };
$('#select-all').onchange = e => { for (const c of filtered()) e.target.checked ? state.selected.add(c.id) : state.selected.delete(c.id); renderRows(); updateSelection(); };
$('#refresh').onclick = () => run(() => operation('Refreshing local stores…', async () => updateLibrary(await api.scan())));
$('#export-button').onclick = () => run(() => exportContexts([...state.selected]));
$('#import-button').onclick = () => run(importPreview);
$('#confirm-import').onclick = () => run(() => operation('Importing contexts into your library…', async () => { $('#confirm-import').disabled = true; try { updateLibrary(await api.importBundle()); $('#import-dialog').close(); state.view = 'imported'; render(); toast('Contexts imported. Original files are available in the inspector.'); } finally { $('#confirm-import').disabled = false; } }));
document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); state.view = 'all'; render(); $('#search').focus(); } });

// Terminals remain mounted when navigating the library, preserving the live CLI screen.
const terminalSessions = new Map(); let activeTerminal = null;
async function openTerminal(id) {
  if (terminalSessions.has(id)) {
    const previous = terminalSessions.get(id);
    if (!previous.exited) { showTerminal(id); return; }
    await api.terminalClose(previous.token); previous.term.dispose(); previous.host.remove(); terminalSessions.delete(id);
  }
  const c = state.library.contexts.find(c => c.id === id);
  const session = id.startsWith('setup:') ? await api.providerSignIn(id.slice(6)) : await api.terminalStart(id);
  if (!session) return;
  const term = new Terminal({ cursorBlink: true, fontFamily: 'monospace', fontSize: 12, scrollback: 5000, theme: { background: '#111610', foreground: '#d7e3cf', cursor: '#d3f49a' } });
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  const host = element('div', 'terminal-host'); $('#terminal-content').append(host); term.open(host);
  terminalSessions.set(id, { term, fit, host, token: session.token, title: `${session.machine || c?.machine || 'Local'} · ${c?.title || providers[id.slice(6)] + ' sign-in'}`, exited: false });
  term.onData(data => api.terminalInput({ token: session.token, data }));
  term.onResize(({ cols, rows }) => api.terminalResize({ token: session.token, cols, rows }));
  showTerminal(id); await api.terminalAttach(session.token);
}
function showTerminal(id) {
  activeTerminal = id; $('#terminal-dock').classList.remove('hidden');
  for (const [key, t] of terminalSessions) t.host.classList.toggle('hidden', key !== id);
  const tabs = $('#terminal-tabs'); tabs.replaceChildren();
  for (const [key, t] of terminalSessions) tabs.append(button(`${t.exited ? '○' : '●'} ${t.title.slice(0, 32)}`, `terminal-tab ${key === id ? 'active' : ''}`, () => showTerminal(key)));
  requestAnimationFrame(() => { const t = terminalSessions.get(id); t.fit.fit(); t.term.focus(); });
}
api.onTerminalData(({ token, data }) => { for (const t of terminalSessions.values()) if (t.token === token) t.term.write(Uint8Array.from(atob(data), c => c.charCodeAt(0))); });
api.onTerminalExit(({ token, code }) => { for (const [id, t] of terminalSessions) if (t.token === token) { t.exited = true; if (id.startsWith('setup:')) run(async () => { setupState = await api.setupAuthRefresh({ force: true }); if (state.view === 'setup') renderSetup(); }); t.term.write(`\r\n\x1b[90m[Process exited: ${code}. Close this tab to resume again.]\x1b[0m\r\n`); } if (activeTerminal) showTerminal(activeTerminal); });
$('#terminal-hide').onclick = () => $('#terminal-dock').classList.add('hidden');
$('#terminal-expand').onclick = () => {
  const expanded = $('#terminal-dock').classList.toggle('expanded'), control = $('#terminal-expand');
  const label = expanded ? 'Restore terminal size' : 'Expand terminal';
  control.setAttribute('aria-expanded', String(expanded)); control.setAttribute('aria-label', label); control.title = label;
  control.textContent = expanded ? '↙' : '⛶';
  requestAnimationFrame(() => { const t = terminalSessions.get(activeTerminal); if (t && !$('#terminal-dock').classList.contains('hidden')) { t.fit.fit(); t.term.focus(); } });
};
$('#terminal-close').onclick = () => run(async () => { const t = terminalSessions.get(activeTerminal); if (!t) return; if (!await api.terminalClose(t.token)) return; t.term.dispose(); t.host.remove(); terminalSessions.delete(activeTerminal); activeTerminal = null; const next = terminalSessions.keys().next().value; if (next) showTerminal(next); else $('#terminal-dock').classList.add('hidden'); });
$('#terminal-reopen').onclick = () => { if (activeTerminal) showTerminal(activeTerminal); else toast('Select a context and choose “Chat here” to start a terminal.'); };
new ResizeObserver(() => { if (!$('#terminal-dock').classList.contains('hidden')) terminalSessions.get(activeTerminal)?.fit.fit(); }).observe($('#terminal-content'));
run(async () => { updateLibrary(await api.scan()); cloudState = await api.cloudStatus(); renderCloud(); window.__ready = true; });

// Cloud accounts use a private connector; the renderer receives only connection status.
let cloudState = { targets: [], googleReady: false, job: null, pending: null };
let cloudSeenJob = null, cloudPolling = false;
const cloudNames = { google: 'Google Drive', onedrive: 'OneDrive', icloud: 'iCloud Drive' };
function cloudBusy() { return cloudState.job?.status === 'running'; }
function renderCloud() {
  const page = $('#cloud-view');
  const maintainerOpen = page.querySelector('.maintainer-setup')?.open || false;
  page.replaceChildren();
  const intro = element('div', 'notice', 'Connect directly, or use a folder synced by another app. Only contexts you choose are uploaded. Incoming snapshots are verified and added to Imported; live provider files are never replaced.'); page.append(intro);
  const cards = element('div', 'cloud-cards');
  for (const [id, name] of Object.entries(cloudNames)) {
    const card = element('article', `cloud-card ${id}`); card.append(element('div', 'cloud-icon', id === 'google' ? '△' : id === 'onedrive' ? '☁' : '◌'), element('h2', '', name));
    card.append(element('p', '', id === 'icloud' ? 'Connect with your Apple Account and two-factor authentication.' : 'Sign in securely in your browser. No cloud desktop app required.'));
    const connect = button('Connect ' + name, 'button primary', () => cloudSignIn(id));
    connect.disabled = cloudBusy() || Boolean(cloudState.pending) || (id === 'google' && !cloudState.googleReady);
    card.append(connect, button('Use a synced folder instead', 'quiet folder-alternative', async () => { const result = await api.cloudFolder(id); if (result) acceptCloud(result); }));
    if (id === 'google' && !cloudState.googleReady) card.append(element('p', 'muted cloud-unavailable', 'Google sign-in is not configured in this build. The maintainer must register the app once; users should not need API keys.'));
    cards.append(card);
  }
  page.append(cards);
  if (cloudState.job) {
    const job = cloudState.job;
    const status = element('div', 'cloud-progress', job.status === 'running' ? '◌ ' + job.message : job.status === 'error' ? job.error : job.result?.exported !== undefined ? `Last sync: ${job.result.exported} uploaded · ${job.result.imported} imported · ${job.result.skipped} already received` : job.label + ' — complete');
    status.setAttribute('role', 'status'); page.append(status);
  }
  page.append(element('h2', 'cloud-section-title', 'Your connections'));
  if (!cloudState.targets.length) page.append(element('p', 'muted', 'Connect an account above to start. Connecting alone does not upload your library.'));
  for (const target of cloudState.targets) {
    const card = element('article', 'sync-target'), heading = element('div', 'sync-target-heading');
    const description = element('div'); description.append(element('h3', '', target.label), element('p', 'muted', target.mode === 'folder' ? target.path : 'Direct cloud connection · GPT Manager/v1')); heading.append(description);
    const sync = button('↻ Sync now', 'button primary', async () => acceptCloud(await api.cloudSync({ id: target.id }))); sync.disabled = cloudBusy() || Boolean(cloudState.pending); heading.append(sync); card.append(heading);
    const scopes = element('div', 'sync-options');
    const label = element('label'), all = element('input'); all.type = 'checkbox'; all.checked = target.allLocal; all.disabled = cloudBusy(); all.onchange = () => run(async () => acceptCloud(await api.cloudConfigure({ id: target.id, allLocal: all.checked }))); label.append(all, document.createTextNode('Back up all unarchived local contexts, including new ones')); scopes.append(label);
    const selected = button(`Use library selection (${state.selected.size})`, 'button', async () => { const ids = [...state.selected].filter(id => state.library.contexts.find(c => c.id === id)?.origin === 'local'); if (!ids.length) return toast('Select local contexts in the library first.'); acceptCloud(await api.cloudConfigure({ id: target.id, selection: ids })); }); selected.disabled = cloudBusy(); scopes.append(selected);
    scopes.append(element('p', 'muted', target.allLocal ? 'All local contexts will be backed up. Imported snapshots are not re-uploaded.' : target.selection.length ? `${target.selection.length} selected contexts will be backed up. Updates create new snapshots.` : 'Receive only: no contexts are selected for upload.'));
    const autoLabel = element('label'), auto = element('input'); auto.type = 'checkbox'; auto.checked = target.auto; auto.disabled = cloudBusy(); auto.onchange = () => run(async () => acceptCloud(await api.cloudConfigure({ id: target.id, auto: auto.checked }))); autoLabel.append(auto, document.createTextNode('Sync automatically every 15 minutes while GPT Manager is open')); scopes.append(autoLabel); card.append(scopes);
    const footer = element('div', 'sync-target-footer'); footer.append(element('span', '', target.lastSync ? 'Last synced ' + new Date(target.lastSync).toLocaleString() : 'Not synced yet'), button('Disconnect', 'quiet', async () => { const result = await api.cloudDisconnect(target.id); if (result) acceptCloud(result); })); card.append(footer);
    if (target.lastError) card.append(element('div', 'notice', target.lastError)); page.append(card);
  }
  page.append(element('p', 'muted', 'Sync keeps versioned archives; it does not merge live chats or propagate deletions. Archives are not encrypted by GPT Manager. Your cloud provider controls their storage. Folder connections rely on your sync app to finish uploading.'));
  const setup = element('details', 'maintainer-setup'); setup.append(element('summary', '', 'Maintainer setup · Google sign-in'), element('p', '', 'Import the OAuth Desktop app JSON for GPT Manager once. Releases can ship the app registration so users only need to sign in. Google’s shared connector credentials are being retired; this build does not rely on them.'));
  setup.open = maintainerOpen;
  setup.append(button('Import Google OAuth app JSON', 'button', async () => { const result = await api.cloudGoogleConfig(); if (result) { acceptCloud(result); toast('Google app registration configured. Users can now connect their account.'); } })); page.append(setup);
}
function acceptCloud(value) {
  cloudState = value; renderCloud();
  const job = value.job;
  if (!job) return;
  if (job.status === 'running') {
    if ($('#cloud-dialog').open && !$('#cloud-dialog-body input')) cloudProgress(job.message, job.progress);
    return;
  }
  if (cloudSeenJob === job.id) return;
  cloudSeenJob = job.id;
  if (job.status === 'error') {
    toast(job.error, true);
    if ($('#cloud-dialog').open) {
      const body = $('#cloud-dialog-body'); body.replaceChildren(element('h2', '', 'Connection needs attention'), element('p', '', job.error), button('Close', 'button', async () => { acceptCloud(await api.cloudCancel()); $('#cloud-dialog').close(); }));
    }
    return;
  }
  if (job.result?.question) { cloudQuestion(job.result.question); return; }
  if (job.result?.connected) { $('#cloud-dialog').close(); toast('Account connected. Choose contexts to back up, or sync to receive snapshots.'); }
  if (job.result?.imported !== undefined) {
    run(async () => updateLibrary(await api.library()));
    if (job.result.warnings?.length) toast(job.result.warnings[0], true);
  }
}
function cloudProgress(message, progress) {
  const body = $('#cloud-dialog-body');
  if (!body.querySelector('.setup-progress')) {
    const meter = element('progress', 'setup-progress'); meter.max = 100; meter.setAttribute('aria-label', 'Cloud connector setup');
    const status = element('p', 'setup-status'); status.setAttribute('role', 'status');
    body.replaceChildren(element('div', 'eyebrow', 'CONNECT YOUR CLOUD'), element('h2', '', 'Getting connected.'), status, meter, element('p', 'setup-download'), button('Cancel sign-in', 'button', async () => { acceptCloud(await api.cloudCancel()); $('#cloud-dialog').close(); }));
  }
  body.querySelector('.setup-status').textContent = message;
  const meter = body.querySelector('.setup-progress'), detail = body.querySelector('.setup-download');
  if (progress?.total > 0) {
    const percent = Math.min(100, Math.floor(progress.received / progress.total * 100));
    meter.value = percent;
    detail.textContent = `${percent}% · ${(progress.received / 1048576).toFixed(1)} of ${(progress.total / 1048576).toFixed(1)} MB downloaded`;
  } else {
    meter.removeAttribute('value');
    detail.textContent = progress ? `${(progress.received / 1048576).toFixed(1)} MB downloaded · total size unavailable` : '';
  }
}
function cloudSignIn(provider) {
  const dialog = $('#cloud-dialog'), body = $('#cloud-dialog-body'); body.replaceChildren();
  body.append(element('div', 'eyebrow', 'DIRECT CLOUD CONNECTION'), element('h2', '', 'Connect ' + cloudNames[provider]), element('p', '', 'The cloud connector is downloaded and verified automatically on first use. Credentials stay in this manager’s private local storage; they are never included in context archives.'));
  const form = element('form'); let apple, password;
  if (provider === 'icloud') {
    body.append(element('p', '', 'Use your regular Apple Account password, then approve the prompt on a trusted device. App-specific passwords do not work with this connector. iCloud web access must be enabled.'));
    for (const [labelText, type, id] of [['Apple Account email', 'email', 'apple-email'], ['Password', 'password', 'apple-password']]) {
      const field = element('div', 'field'), label = element('label', '', labelText), input = element('input'); input.id = id; input.type = type; input.required = true; input.autocomplete = type === 'password' ? 'current-password' : 'username'; label.htmlFor = id; field.append(label, input); form.append(field); if (type === 'email') apple = input; else password = input;
    }
  } else body.append(element('p', '', 'Your browser will open the provider’s consent screen. The connector may appear as rclone. Return here after signing in.'));
  const submit = element('button', 'button primary', provider === 'icloud' ? 'Continue to verification' : 'Open sign-in'); submit.type = 'submit'; form.append(submit);
  form.onsubmit = e => { e.preventDefault(); run(async () => { const credentials = { provider, apple_id: apple?.value || '', password: password?.value || '' }; if (password) password.value = ''; cloudProgress('Preparing cloud connector and opening sign-in…'); try { acceptCloud(await api.cloudConnect(credentials)); } catch (error) { cloudProgress(error.message); throw error; } finally { credentials.password = ''; } }); };
  body.append(form, button('Cancel', 'quiet', () => dialog.close())); dialog.showModal();
}
function cloudQuestion(question) {
  const dialog = $('#cloud-dialog'), body = $('#cloud-dialog-body'); body.replaceChildren(element('div', 'eyebrow', 'FINISH CONNECTING'), element('h2', '', question.name === 'config_2fa' ? 'Enter your verification code.' : 'Choose your cloud location.'), element('p', '', question.help));
  if (question.error) body.append(element('div', 'notice', question.error));
  const form = element('form'), field = element('div', 'field'), label = element('label', '', question.name === 'config_2fa' ? 'Verification code' : 'Your response'); label.htmlFor = 'cloud-answer';
  const input = element(question.examples.length ? 'select' : 'input'); input.id = 'cloud-answer'; if (!question.examples.length) input.type = question.password ? 'password' : 'text';
  for (const item of question.examples) { const option = element('option', '', item.label); option.value = item.value; input.append(option); }
  if (question.default) input.value = question.default; input.required = question.required; field.append(label, input); form.append(field);
  const submit = element('button', 'button primary', 'Continue'); submit.type = 'submit'; form.append(submit); form.onsubmit = e => { e.preventDefault(); const answer = input.value; input.value = ''; cloudProgress('Checking your response…'); run(async () => acceptCloud(await api.cloudAnswer({ answer }))); };
  body.append(form, button('Cancel sign-in', 'quiet', async () => { acceptCloud(await api.cloudCancel()); dialog.close(); })); if (!dialog.open) dialog.showModal(); input.focus();
}
$('#cloud-dialog').addEventListener('cancel', event => { if (cloudState.pending) { event.preventDefault(); run(async () => { acceptCloud(await api.cloudCancel()); $('#cloud-dialog').close(); }); } });
setInterval(async () => {
  if (cloudPolling || (!cloudBusy() && state.view !== 'cloud')) return;
  cloudPolling = true;
  try { acceptCloud(await api.cloudStatus()); } catch (e) { toast(e.message, true); } finally { cloudPolling = false; }
}, 1000);
setInterval(() => run(async () => acceptCloud(await api.cloudTick())), 60000);

function transientDialog(title) {
  const dialog = element('dialog'), heading = element('h2', '', title);
  const close = button('×', 'dialog-close', () => dialog.close()); close.setAttribute('aria-label', 'Close');
  dialog.append(close, heading); dialog.addEventListener('close', () => dialog.remove()); document.body.append(dialog);
  return dialog;
}
function renderSshLocations(page) {
  const heading = element('div', 'section-intro'), controls = element('div', 'ssh-actions');
  heading.append(element('h2', '', 'SSH endpoints'));
  controls.append(button('+ Add SSH endpoint', 'button', () => addSshDialog()), button('Scan local network', 'button', scanLanDialog)); heading.append(controls); page.append(heading);
  page.append(element('p', 'muted', 'Remote contexts stay on their host until you choose Resume here. SSH uses your existing config, keys and agent; first establish host trust through your system SSH client.'));
  for (const endpoint of state.library.sshEndpoints || []) {
    const card = element('div', 'location-card'), content = element('div'), actions = element('div', 'ssh-actions');
    content.append(element('h3', '', endpoint.label), element('code', '', endpoint.host), element('p', 'muted', endpoint.machine ? `Machine: ${endpoint.machine.name} · ${endpoint.machine.home}` : 'Not connected yet'));
    if (endpoint.error) content.append(element('p', 'notice', endpoint.error));
    actions.append(element('span', 'location-status', endpoint.connected ? 'Connected' : 'Offline'), button('Refresh', 'button', () => operation('Reading SSH endpoint…', async () => updateLibrary(await api.sshRefresh({ id: endpoint.id })))), button('Remove', 'quiet', async () => updateLibrary(await api.sshRemove({ id: endpoint.id }))));
    card.append(content, actions); page.append(card);
  }
}
async function addSshDialog(initialHost = '') {
  const aliases = await api.sshAliases(), dialog = transientDialog('Add SSH endpoint'), form = element('form');
  const hostField = element('div', 'field'), hostLabel = element('label', '', 'SSH alias or user@hostname'), host = element('input'); host.id = 'ssh-host'; hostLabel.htmlFor = host.id; host.required = true; host.value = initialHost; host.placeholder = 'workstation or user@192.168.1.20'; hostField.append(hostLabel, host);
  if (aliases.length) { const picker = element('select'); picker.setAttribute('aria-label', 'Saved SSH aliases'); const empty = element('option', '', 'Choose an alias from SSH config…'); empty.value = ''; picker.append(empty); for (const alias of aliases) { const option = element('option', '', alias); option.value = alias; picker.append(option); } picker.onchange = () => { if (picker.value) host.value = picker.value; }; hostField.append(picker); }
  const labelField = element('div', 'field'), labelTitle = element('label', '', 'Display name (optional)'), label = element('input'); label.id = 'ssh-label'; labelTitle.htmlFor = label.id; label.maxLength = 100; labelField.append(labelTitle, label);
  const submit = element('button', 'button primary', 'Add and connect'); submit.type = 'submit'; const status = element('p', 'muted'); status.setAttribute('role', 'status');
  form.append(hostField, labelField, element('p', 'muted', 'Use ~/.ssh/config for custom ports, jump hosts and identity files. Connections require key/agent authentication and a trusted host key. The remote host needs Python 3.10+ and its provider CLI.'), submit, status);
  form.onsubmit = event => { event.preventDefault(); run(async () => { submit.disabled = true; status.textContent = 'Connecting and discovering remote contexts…'; try { updateLibrary(await api.sshAdd({ host: host.value, label: label.value })); dialog.close(); } catch (error) { status.textContent = error.message; } finally { submit.disabled = false; } }); };
  dialog.append(form); dialog.showModal(); host.focus();
}
async function scanLanDialog() {
  const networks = await api.localNetworks(), dialog = transientDialog('Find SSH servers nearby');
  dialog.append(element('p', '', 'Scan a directly attached local subnet for SSH on port 22. At most 256 addresses are checked. No login attempts or host-key approvals are made.'));
  if (!networks.length) { dialog.append(element('p', 'notice', 'No supported local IPv4 subnet was found. Add an SSH alias or hostname instead.')); dialog.showModal(); return; }
  const select = element('select'); select.setAttribute('aria-label', 'Local subnet');
  for (const network of networks) { const option = element('option', '', `${network.interface} · ${network.cidr}`); option.value = network.cidr; select.append(option); }
  const results = element('div'), status = element('p', 'muted'); status.setAttribute('role', 'status');
  const scan = button('Scan subnet', 'button primary', async () => { scan.disabled = true; results.replaceChildren(); status.textContent = 'Checking SSH servers…'; try { const result = await api.scanNetwork({ cidr: select.value }); status.textContent = `${result.hosts.length} SSH server(s) found. Servers using other ports or blocking discovery will not appear.`; for (const server of result.hosts) { const row = element('div', 'location-card'); row.append(element('code', '', server.host), button('Add endpoint', 'button', async () => { dialog.close(); await addSshDialog(server.host); })); results.append(row); } } finally { scan.disabled = false; } });
  dialog.append(select, scan, status, results); dialog.showModal();
}
async function resumeHere(id) {
  const dialog = transientDialog('Resume on this machine'), status = element('p', '', 'Choose a project folder in the next window.'), meter = element('progress', 'setup-progress'); meter.setAttribute('aria-label', 'Context handoff');
  dialog.querySelector('.dialog-close').remove(); dialog.addEventListener('cancel', event => event.preventDefault()); status.id = 'handoff-status'; status.setAttribute('role', 'status'); dialog.append(status, meter); dialog.showModal();
  try {
    const result = await api.resumeHere(id);
    if (!result) return;
    updateLibrary(result.library); state.view = 'all'; render(); await selectContext(result.id);
    if (result.warning) toast(result.warning, true);
    await openTerminal(result.id);
  } finally { dialog.close(); }
}
api.onHandoffProgress(message => { const status = $('#handoff-status'); if (status) status.textContent = message; });

// Catch-up forms remain mounted during job polling, preserving focus and source choices.
let catchupState = { job: null, preview: null, providers: [] }, catchupLoaded = false, catchupLoading = false;
let catchupPreviewId = null, catchupSeenJob = null, catchupHistory = [], catchupRecord = null;
const catchupSelection = new Set();
function catchupRange(preset, from, to, current = new Date()) {
  const end = new Date(current), start = new Date(current); start.setHours(0, 0, 0, 0);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const labels = { today: 'Today', yesterday: 'Yesterday', seven: 'Last 7 days', week: 'Last week (Mon–Sun)' };
  if (preset === 'yesterday') { end.setHours(0, 0, 0, 0); start.setDate(start.getDate() - 1); }
  else if (preset === 'seven') start.setDate(start.getDate() - 6);
  else if (preset === 'week') { start.setDate(start.getDate() - (start.getDay() + 6) % 7); end.setTime(start.getTime()); start.setDate(start.getDate() - 7); }
  else if (preset === 'custom') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to)) throw new Error('Choose both dates.');
    const [fy, fm, fd] = from.split('-').map(Number), [ty, tm, td] = to.split('-').map(Number);
    start.setFullYear(fy, fm - 1, fd); end.setFullYear(ty, tm - 1, td); end.setHours(0, 0, 0, 0); end.setDate(end.getDate() + 1);
  }
  return { start: start.toISOString(), end: end.toISOString(), label: `${labels[preset] || `${from} through ${to}`} · ${zone}` };
}
function renderCatchUp() {
  const page = $('#catchup-view');
  if (!page.childElementCount) {
    const form = element('form', 'catchup-controls'), period = element('select'); period.id = 'catchup-period'; period.setAttribute('aria-label', 'Activity period');
    for (const [id, label] of [['yesterday', 'Yesterday'], ['today', 'Today'], ['seven', 'Last 7 days'], ['week', 'Last week (Mon–Sun)'], ['custom', 'Custom dates']]) { const option = element('option', '', label); option.value = id; period.append(option); }
    const dates = element('div', 'catchup-dates hidden'); dates.id = 'catchup-dates';
    for (const [id, title] of [['from', 'From'], ['to', 'Through']]) { const label = element('label', '', title), input = element('input'); input.type = 'date'; input.id = 'catchup-' + id; label.append(input); dates.append(label); }
    period.onchange = () => dates.classList.toggle('hidden', period.value !== 'custom');
    const remoteLabel = element('label', 'catchup-toggle'), remote = element('input'); remote.type = 'checkbox'; remote.id = 'catchup-remote'; remoteLabel.append(remote, document.createTextNode('Include connected SSH histories'));
    const review = element('button', 'button primary', 'Review activity'); review.id = 'catchup-review'; review.type = 'submit'; form.append(period, dates, remoteLabel, review);
    form.onsubmit = event => { event.preventDefault(); run(async () => { catchupRecord = null; $('#catchup-result').replaceChildren(); acceptCatchUp(await api.catchupPrepare({ ...catchupRange(period.value, $('#catchup-from').value, $('#catchup-to').value), include_remote: remote.checked })); }); };
    const status = element('div'); status.id = 'catchup-status'; status.setAttribute('role', 'status');
    const layout = element('div', 'catchup-layout'), main = element('div'), preview = element('div'), result = element('div'), history = element('aside', 'catchup-history'); preview.id = 'catchup-preview'; result.id = 'catchup-result'; history.id = 'catchup-history';
    main.append(preview, result); layout.append(main, history);
    const aside = element('aside', 'transfer-note'); aside.append(element('strong', '', 'One recap, all your services.'), element('p', '', 'You don’t need a separate summary for each service. Codex or Claude can summarize selected conversations from Codex, Claude, Gemini, and Antigravity together. The provider you choose receives those excerpts, even when they came from another service.'));
    const schedule = element('details', 'recap-schedule'); schedule.id = 'recap-schedule'; schedule.append(element('summary', '', 'Automatic daily recap'), element('div'));
    page.append(aside, schedule, form, element('p', 'muted', 'Dates use your local timezone. Review reads conversation excerpts; Generate sends only your chosen excerpts and their labels to the selected provider. Provider usage limits and charges apply.'), status, layout);
  }
  loadRecapSchedule();
  paintCatchUp();
  if (!catchupLoaded && !catchupLoading) {
    catchupLoading = true;
    run(async () => { try { const [status, history] = await Promise.all([api.catchupStatus(), api.catchupHistory()]); catchupHistory = history; catchupLoaded = true; acceptCatchUp(status); renderCatchupHistory(); } finally { catchupLoading = false; } });
  }
}
function acceptCatchUp(value) {
  const providersChanged = JSON.stringify(catchupState.providers) !== JSON.stringify(value.providers);
  catchupState = value;
  if (value.preview?.id !== catchupPreviewId) {
    catchupPreviewId = value.preview?.id;
    catchupSelection.clear(); for (const source of value.preview?.sources || []) catchupSelection.add(source.sourceId);
    renderCatchupPreview();
  }
  if (providersChanged) renderCatchupPreview();
  paintCatchUp();
  const job = value.job;
  if (job && job.status === 'complete' && job.id !== catchupSeenJob) {
    catchupSeenJob = job.id;
    if (job.result?.summaryId) run(async () => { catchupRecord = await api.catchupGet({ id: job.result.summaryId }); catchupHistory = await api.catchupHistory(); renderCatchupResult(); renderCatchupHistory(); });
  }
}
function paintCatchUp() {
  if (!$('#catchup-status')) return;
  const running = catchupState.job?.status === 'running', status = $('#catchup-status');
  $('#catchup-review').disabled = running;
  document.querySelectorAll('#catchup-preview input, #catchup-preview select, #catchup-preview button').forEach(control => control.disabled = running);
  const generate = $('#catchup-generate'); if (generate) generate.disabled = running || !catchupSelection.size || !$('#catchup-provider').value;
  const job = catchupState.job;
  if (running) {
    if (!status.querySelector('progress')) {
      const progress = element('progress', 'setup-progress'); progress.setAttribute('aria-label', 'Catch-up progress');
      status.replaceChildren(element('p', 'catchup-job-message'), progress, button('Cancel', 'quiet', async () => acceptCatchUp(await api.catchupCancel())));
    }
    status.querySelector('.catchup-job-message').textContent = job.message;
  } else status.replaceChildren(...(job?.status === 'error' ? [element('p', 'notice', job.error)] : job?.status === 'canceled' ? [element('p', 'muted', 'Canceled. No new recap was generated.')] : []));
}
function renderCatchupPreview() {
  const parent = $('#catchup-preview'); if (!parent) return; parent.replaceChildren(); const preview = catchupState.preview;
  if (!preview) { parent.append(element('div', 'empty', 'Choose a period to find the threads you left open.')); return; }
  parent.append(element('h2', '', preview.label), element('p', 'muted', `${preview.sources.length} conversations with dated activity · ${preview.scanned} histories checked · ${preview.duplicates} duplicate snapshots omitted`));
  for (const warning of preview.warnings) parent.append(element('p', 'notice', warning));
  if (!preview.sources.length) { parent.append(element('p', 'empty', 'No dated activity found in the sampled histories. Try another date range, refresh Context locations, or include connected SSH histories.')); return; }
  const selectAll = button('Select all', 'quiet', () => { for (const source of preview.sources) catchupSelection.add(source.sourceId); renderCatchupPreview(); });
  const selectNone = button('Clear selection', 'quiet', () => { catchupSelection.clear(); renderCatchupPreview(); }); parent.append(selectAll, selectNone);
  const sources = element('div', 'catchup-sources');
  for (const source of preview.sources) {
    const row = element('article', 'catchup-source'), label = element('label'), check = element('input'); check.type = 'checkbox'; check.checked = catchupSelection.has(source.sourceId); check.onchange = () => { check.checked ? catchupSelection.add(source.sourceId) : catchupSelection.delete(source.sourceId); paintCatchUp(); };
    label.append(check, document.createTextNode(source.title)); row.append(label, element('p', 'muted', `${providers[source.provider]} · ${source.machine} · ${source.project || 'Project not recorded'}`));
    const detail = element('details'); detail.append(element('summary', '', `Review excerpts · ${source.messages.length} of ${source.matched} matching messages${source.partial ? ' · sampled' : ''}`));
    for (const message of source.messages) { const item = element('div', 'catchup-excerpt'); item.append(element('small', 'muted', `${message.role} · ${new Date(message.timestamp).toLocaleString()}`), element('pre', '', message.content)); detail.append(item); }
    row.append(detail); sources.append(row);
  }
  parent.append(sources);
  parent.append(button('Install or sign in to a provider', 'quiet', () => { state.view = 'setup'; render(); }));
  const controls = element('div', 'catchup-generate'), provider = element('select'); provider.id = 'catchup-provider'; provider.setAttribute('aria-label', 'Summary provider');
  for (const option of catchupState.providers) { const item = element('option', '', `${option.id === 'codex' ? 'Codex' : 'Claude'}${option.available ? '' : ' — not installed'}`); item.value = option.available ? option.id : ''; item.disabled = !option.available; provider.append(item); }
  const model = element('input'); model.id = 'catchup-model'; model.placeholder = 'Model (optional)'; model.setAttribute('aria-label', 'Summary model, optional');
  const generate = button('Generate recap', 'button primary', async () => acceptCatchUp(await api.catchupGenerate({ preview_id: preview.id, source_ids: [...catchupSelection], provider: provider.value, model: model.value.trim() }))); generate.id = 'catchup-generate'; provider.onchange = paintCatchUp;
  controls.append(provider, model, generate); parent.append(controls, element('p', 'muted', 'Uses a separate summary session. Your original conversations are not resumed or edited. Saved recaps include their reviewed excerpts and stay on this machine.'));
  paintCatchUp();
}
function renderCatchupHistory() {
  const parent = $('#catchup-history'); if (!parent) return; parent.replaceChildren(element('h3', '', 'Saved recaps'));
  if (!catchupHistory.length) parent.append(element('p', 'muted', 'Your daily and weekly recaps will appear here.'));
  for (const item of catchupHistory) parent.append(button(`${item.label}\n${new Date(item.created).toLocaleDateString()} · ${item.provider}`, 'catchup-history-item', async () => { catchupRecord = await api.catchupGet({ id: item.id }); renderCatchupResult(); $('#catchup-result').scrollIntoView({ behavior: 'smooth', block: 'start' }); }));
}
async function openCatchupSource(source) {
  updateLibrary(await api.library());
  const c = state.library.contexts.find(c => c.id === source.contextId) || state.library.contexts.find(c => c.sessionId === source.sessionId && c.provider === source.provider && c.project === source.project);
  if (!c) return toast('This context is no longer available. Its reviewed excerpt remains in the saved recap.', true);
  state.view = c.archived ? 'archived' : 'all'; state.query = ''; $('#search').value = ''; state.provider = 'all'; render(); await selectContext(c.id);
}
function renderCatchupResult() {
  const parent = $('#catchup-result'); if (!parent || !catchupRecord) return;
  const record = catchupRecord; parent.replaceChildren();
  parent.append(element('div', 'eyebrow', 'YOUR WORK, RECONSTRUCTED'), element('h2', '', record.label), element('p', 'muted', `Generated ${new Date(record.created).toLocaleString()} · ${record.provider} · ${record.model}`), element('p', 'catchup-overview', record.overview), element('p', 'muted', 'AI recap based on the reviewed excerpts. Check the linked conversations before relying on a decision or completion claim.'));
  for (const warning of record.warnings) parent.append(element('p', 'notice', warning));
  for (const project of record.projects) {
    const sources = project.sourceIds.map(id => record.sources.find(s => s.sourceId === id)).filter(Boolean), first = sources[0], card = element('article', 'catchup-project');
    card.append(element('h3', '', first?.project?.split(/[\\/]/).filter(Boolean).pop() || first?.title || 'Project'), element('p', 'muted', `${first?.machine || ''} · ${first?.project || 'Project not recorded'}`), element('p', '', project.summary));
    for (const [key, title] of [['decisions', 'Decisions & outcomes'], ['openLoops', 'Still open'], ['nextSteps', 'Suggested next steps']]) {
      if (!project[key].length) continue;
      const list = element('ul'); for (const item of project[key]) list.append(element('li', '', item)); card.append(element('h4', '', title), list);
    }
    const links = element('div', 'catchup-links'); for (const source of sources) links.append(button('↗ ' + source.title, 'quiet', () => openCatchupSource(source))); card.append(links); parent.append(card);
  }
  const evidence = element('details', 'catchup-saved-evidence'); evidence.append(element('summary', '', 'Reviewed evidence saved with this recap'));
  for (const source of record.sources) { evidence.append(element('h4', '', `${source.sourceId} · ${source.title}`)); for (const message of source.messages) evidence.append(element('pre', '', `${message.role} · ${new Date(message.timestamp).toLocaleString()}\n${message.content}`)); } parent.append(evidence);
  parent.append(button('Copy recap', 'button', () => api.copyText([record.label, record.overview, ...record.projects.flatMap(p => [record.sources.find(s => s.projectId === p.projectId)?.project || 'Project', p.summary, ...p.decisions, ...p.openLoops, ...p.nextSteps])].join('\n\n'))), button('Delete saved recap', 'quiet', async () => { catchupHistory = await api.catchupDelete({ id: record.id }); catchupRecord = null; parent.replaceChildren(); renderCatchupHistory(); }));
}
let catchupPolling = false;
setInterval(async () => {
  if (catchupPolling || catchupState.job?.status !== 'running') return;
  catchupPolling = true;
  try { acceptCatchUp(await api.catchupStatus()); } catch (error) { toast(error.message, true); } finally { catchupPolling = false; }
}, 1000);


// Installation progress does not interrupt account sign-in terminals.
let setupState = null, setupLoading = false;
async function loadSetup(checkAuth = false) {
  if (setupLoading) return;
  setupLoading = true;
  try { setupState = checkAuth ? await api.setupAuthRefresh({ force: checkAuth === 'force' }) : await api.setupStatus(); if (setupState.job?.status === 'complete') catchupLoaded = false; if (!setupState.authChecking && setupState.providers.some(p => p.available && !p.auth?.checkedAt)) setupState = await api.setupAuthRefresh({ force: true }); renderSetup(); }
  catch (error) { toast(error.message, true); }
  finally { setupLoading = false; }
}
function renderSetup() {
  const page = $('#setup-view'); page.replaceChildren();
  page.append(element('p', 'transfer-note', '1. Use an existing CLI or install a managed copy.  2. Check sign-in.  3. Open Catch up or resume a conversation. Downloads are kept inside GPT Manager; no administrator access or manual Node setup is needed. Your provider’s subscription and usage limits still apply.'));
  const running = setupState?.job?.status === 'running';
  const cards = element('div', 'transfer-grid');
  for (const provider of setupState?.providers || []) {
    const card = element('article', 'transfer-card');
    card.append(element('h2', '', provider.id === 'antigravity' ? 'Antigravity CLI (agy)' : providers[provider.id]), element('p', 'muted', provider.available ? `Using ${provider.managed ? 'the managed copy' : 'your existing CLI'}.` : 'No usable CLI found. Install a managed copy to continue.'));
    card.append(element('p', 'muted', `Existing CLI: ${provider.existingAvailable ? 'found' : 'not found'} · Managed copy: ${provider.managedInstalled ? (provider.managedAvailable ? 'installed' : 'needs repair') + (provider.managedVersion ? ' · ' + provider.managedVersion : '') : 'not installed'}`));
    const auth = provider.auth || { state: 'unknown', label: 'Not checked yet' };
    const authStatus = element('p', 'provider-auth ' + auth.state, provider.available ? auth.label : 'Install a CLI to check sign-in.'); authStatus.setAttribute('role', 'status'); card.append(authStatus);
    if (auth.checkedAt) card.append(element('small', 'muted', 'Checked ' + new Date(auth.checkedAt * 1000).toLocaleTimeString()));
    const install = button(provider.managedInstalled ? 'Uninstall managed copy' : 'Install managed copy', 'button ' + (provider.managedInstalled ? '' : 'primary'), async () => { setupState = provider.managedInstalled ? await api.setupUninstall({ provider: provider.id }) : await api.setupInstall({ provider: provider.id }); catchupLoaded = false; renderSetup(); if (provider.managedInstalled) await loadSetup('force'); }); install.disabled = running || (provider.managedInstalled && setupState.authChecking);
    const signIn = button(provider.id === 'antigravity' ? 'Open / sign in' : auth.state === 'signed_in' || auth.state === 'configured' ? 'Sign in again' : 'Sign in', 'button', () => openTerminal('setup:' + provider.id)); signIn.disabled = !provider.available || running;
    card.append(install, signIn);
    if (provider.managedInstalled) { const update = button('Update managed copy', 'quiet', async () => { setupState = await api.setupInstall({ provider: provider.id, activate: false }); renderSetup(); }); update.disabled = running; card.append(update); }
    if (provider.existingAvailable && provider.managed) card.append(button('Use existing CLI', 'button', async () => { setupState = await api.setupPrefer({ provider: provider.id, source: 'existing' }); catchupLoaded = false; await loadSetup('force'); }));
    if (provider.managedAvailable && !provider.managed) card.append(button('Use managed copy', 'button', async () => { setupState = await api.setupPrefer({ provider: provider.id, source: 'managed' }); catchupLoaded = false; await loadSetup('force'); }));
    if (provider.executable) card.append(element('p', 'muted', 'Using: ' + provider.executable));
    cards.append(card);
  }
  page.append(cards);
  const job = setupState?.job;
  if (job) {
    const status = element('div', 'transfer-note'); status.setAttribute('role', 'status'); status.append(element('p', '', job.message));
    if (running) { const progress = element('progress', 'catchup-progress'); progress.max = 100; if (job.percent !== null) progress.value = job.percent; status.append(progress, button('Cancel installation', 'quiet', async () => { setupState = await api.setupCancel(); renderSetup(); })); }
    page.insertBefore(status, cards);
  }
  page.append(button('Refresh installation & sign-in status', 'button', () => loadSetup('force')), button('Go to Catch up', 'button', async () => { catchupLoaded = false; state.view = 'catchup'; render(); }));
  page.append(element('p', 'muted', 'An existing CLI on PATH is preferred by default. Installing a managed copy explicitly switches to it; you can switch back anytime. Running terminals keep their current client. Uninstall removes only managed packages, keeping credentials and conversations. Sign-in checks report the CLI’s local status, not whether a provider will accept your next request. Antigravity CLI opens its interactive sign-in screen; exit the terminal when finished. The Antigravity IDE is installed separately. Windows users: use native Windows installs and folders; WSL stores are separate.'));
}
setInterval(() => { if (setupState?.job?.status === 'running' || setupState?.authChecking) loadSetup(); }, 1000);


let recapScheduleLoaded = false, recapScheduleLoading = false, recapScheduleSummary = null;
async function loadRecapSchedule() {
  if (recapScheduleLoading || !$('#recap-schedule')) return;
  recapScheduleLoading = true;
  try {
    const value = await api.catchupScheduleStatus();
    if (!recapScheduleLoaded) { buildRecapSchedule(value); recapScheduleLoaded = true; }
    paintRecapSchedule(value);
  } catch (error) { toast(error.message, true); } finally { recapScheduleLoading = false; }
}
function buildRecapSchedule(value) {
  const body = $('#recap-schedule > div');
  const form = element('form', 'recap-schedule-form');
  function field(title, input) { const label = element('label', '', title); label.append(input); form.append(label); return input; }
  const enabled = element('input'); enabled.type = 'checkbox'; enabled.checked = value.enabled; field('Enable daily recaps', enabled);
  const time = element('input'); time.type = 'time'; time.required = true; time.value = value.time; field('Local time', time);
  const provider = element('select'); for (const id of ['codex', 'claude']) { const option = element('option', '', providers[id]); option.value = id; provider.append(option); } provider.value = value.provider; field('Generate with', provider);
  const model = element('input'); model.placeholder = 'Provider default'; model.value = value.model; field('Model (optional)', model);
  const remote = element('input'); remote.type = 'checkbox'; remote.checked = value.include_remote; field('Include connected SSH histories', remote);
  const save = element('button', 'button primary', 'Save schedule'); save.type = 'submit'; form.append(save);
  form.onsubmit = event => { event.preventDefault(); run(async () => { save.disabled = true; try { paintRecapSchedule(await api.catchupScheduleConfigure({ enabled: enabled.checked, time: time.value, provider: provider.value, model: model.value.trim(), include_remote: remote.checked })); toast(enabled.checked ? 'Daily recaps enabled.' : 'Daily recaps turned off.'); } finally { save.disabled = false; } }); };
  const status = element('p', 'muted'); status.id = 'recap-schedule-status'; status.setAttribute('role', 'status');
  body.append(element('p', 'muted', 'Each run summarizes yesterday across all readable local and imported chats, including archived chats. Enabling this sends sampled excerpts and project labels automatically to your chosen provider without individual review. Normal provider charges and limits apply. SSH histories are optional; the same coverage limits as manual recaps apply.'), form, element('p', 'muted', 'Runs while GPT Manager is open, including minimized. If you open it after the chosen time, it runs then for yesterday; older missed days are not backfilled. Failed or canceled runs are not retried automatically that day. Turn this off to cancel a daily run.'), status);
}
function paintRecapSchedule(value) {
  const status = $('#recap-schedule-status'); if (!status) return;
  status.textContent = `${value.enabled ? 'On · ' + value.time + ' local time · ' + providers[value.provider] : 'Off'}${value.lastAttempt ? ' · Last attempt: ' + value.lastAttempt : ''}${value.message ? ' · ' + value.message : ''}`;
  if (value.summaryId && value.summaryId !== recapScheduleSummary) {
    recapScheduleSummary = value.summaryId;
    run(async () => { catchupHistory = await api.catchupHistory(); renderCatchupHistory(); });
  }
}
setInterval(() => { if (state.view === 'catchup') loadRecapSchedule(); }, 3000);


let usageLoading = false, usageState = null, usageLastRefresh = 0;
async function loadUsage(refresh = false) {
  if (usageLoading) return;
  usageLoading = true;
  try {
    if (refresh && Date.now() - usageLastRefresh > 30000) { usageLastRefresh = Date.now(); usageState = await api.usageRefresh(); }
    else usageState = await api.usageStatus();
    renderUsage();
  } catch (error) { toast(error.message, true); } finally { usageLoading = false; }
}
function resetText(seconds) {
  if (!seconds) return 'Reset time not reported';
  const left = Math.ceil(seconds - Date.now() / 1000), when = new Date(seconds * 1000).toLocaleString();
  if (left <= 0) return `Reported reset: ${when} · awaiting a fresh report`;
  const totalMinutes = Math.ceil(left / 60), hours = Math.floor(totalMinutes / 60), minutes = totalMinutes % 60;
  return `Resets naturally in ${hours ? hours + 'h ' : ''}${minutes}m · ${when}`;
}
function renderUsage() {
  const page = $('#usage-view');
  if (!page.childElementCount) {
    page.append(element('p', 'transfer-note', 'These are provider-reported account limits, not context-window space or estimated token counts. Only local CLI accounts are shown. Shared account activity elsewhere may consume the same allowance. A reset passing does not prove the balance has refilled; wait for a fresh report.'));
    page.append(button('Refresh usage', 'button', () => loadUsage(true)), button('AI setup', 'button', () => { state.view = 'setup'; render(); }));
    const label = element('label', 'usage-meter-toggle'), meter = element('input'); meter.type = 'checkbox'; meter.checked = usageState.claudeMeter; meter.onchange = () => run(async () => { try { usageState = await api.usageConfigure({ claudeMeter: meter.checked }); renderUsage(); } catch (e) { meter.checked = !meter.checked; throw e; } });
    label.append(meter, document.createTextNode('Also capture Claude usage from new local manager terminals (optional fallback)')); page.append(label, element('p', 'muted', 'Refresh usage reads your local Claude subscription login automatically. This optional fallback supplies a quota status line for Claude sessions launched here, replacing any custom status line for those sessions only. Your Claude settings file stays unchanged. Restart the terminal after changing this option. Requires a recent Claude CLI and an account that reports limits.'));
    const cards = element('div', 'usage-grid'); cards.id = 'usage-cards'; page.append(cards);
  }
  const cards = $('#usage-cards'); cards.replaceChildren();
  for (const provider of usageState.providers) {
    const card = element('article', 'usage-card'); card.append(element('h2', '', providers[provider.id]));
    if (provider.updated) card.append(element('small', 'muted', 'Last report: ' + new Date(provider.updated * 1000).toLocaleString()));
    for (const window of provider.windows) {
      const expired = window.resetsAt && window.resetsAt * 1000 <= Date.now();
      const row = element('div', 'usage-window'); row.append(element('strong', '', window.label), element('span', '', expired ? 'Awaiting updated balance' : `${Number(window.remaining.toFixed(1))}% remaining`));
      if (!expired) { const bar = element('progress'); bar.max = 100; bar.value = window.remaining; bar.setAttribute('aria-label', window.label + ' allowance remaining'); row.append(bar); }
      row.append(element('small', 'muted', resetText(window.resetsAt))); card.append(row);
    }
    if (!provider.windows.length) card.append(element('p', 'muted', provider.checking ? 'Reading account limits…' : 'Remaining allowance not available yet.'));
    card.append(element('p', 'muted', provider.message));
    if (provider.source) card.append(element('small', 'muted', 'Last reporting store: ' + provider.source));
    cards.append(card);
  }
}
setInterval(() => { if (state.view === 'usage') loadUsage(Date.now() - usageLastRefresh >= 60000); }, 3000);
