/* Local-only renderer. Conversation text is always inserted as text, never HTML. */
const api = window.manager;
const $ = selector => document.querySelector(selector);
const providers = { codex: 'Codex', claude: 'Claude', gemini: 'Gemini CLI', antigravity: 'Antigravity' };
const state = { library: { contexts: [], locations: [] }, view: 'all', provider: 'all', query: '', selected: new Set(), active: null, tab: 'messages', messages: [], cursor: null, tools: false, generation: 0, busy: false };
const titles = { all: 'All contexts', starred: 'Starred', imported: 'Imported contexts', archived: 'Archived contexts', locations: 'Context locations', cloud: 'Cloud sync', transfer: 'Backup & teleport' };
const descriptions = { all: 'Pick up the thread. Every conversation, in one place.', starred: 'The conversations you want to keep close.', imported: 'Conversations brought over from another manager.', archived: 'Out of the way. Still here when you need them.', locations: 'Know where your context lives. Connect every local store.', cloud: 'Connect an account. Choose what travels with you.', transfer: 'Your work travels with you. Pack it up and pick it up anywhere.' };
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
function updateStatus() { $('#status').textContent = `${state.library.contexts.length} contexts · ${state.library.locations.filter(x => x.exists).length} connected stores${state.library.warnings?.length ? ` · ${state.library.warnings.length} scan warnings` : ''}`; $('#status').title = (state.library.warnings || []).join('\n'); }
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
    if (state.view === 'archived' ? !c.archived : c.archived) return false;
    if (state.view === 'starred' && !c.starred) return false;
    if (state.view === 'imported' && c.origin !== 'imported') return false;
    return [c.title, c.project, c.sessionId, c.provider, ...(c.tags || [])].join(' ').toLowerCase().includes(query);
  }).sort((a, b) => ({ recent: () => b.updated.localeCompare(a.updated), oldest: () => a.updated.localeCompare(b.updated), size: () => b.size - a.size, title: () => a.title.localeCompare(b.title) })[$('#sort').value]());
}
function providerLabel(provider) { const span = element('span', 'provider-label'); span.append(element('i', `provider-dot ${provider}`), document.createTextNode(providers[provider])); return span; }
function render() {
  $('#crumb').textContent = titles[state.view]; $('#page-title').textContent = titles[state.view]; $('#page-description').textContent = descriptions[state.view];
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b.dataset.view === state.view));
  const library = !['locations', 'transfer', 'cloud'].includes(state.view);
  $('#library-view').classList.toggle('hidden', !library); $('#stats').classList.toggle('hidden', !library);
  $('#cloud-view').classList.toggle('hidden', state.view !== 'cloud');
  $('#locations-view').classList.toggle('hidden', state.view !== 'locations'); $('#transfer-view').classList.toggle('hidden', state.view !== 'transfer');
  $('#nav-total').textContent = state.library.contexts.filter(c => !c.archived).length;
  const stats = $('#stats'); stats.replaceChildren();
  for (const [id, name] of Object.entries(providers)) {
    const items = state.library.contexts.filter(c => c.provider === id), card = element('div', 'stat'), top = element('div', 'stat-top', name);
    top.append(element('i', `provider-dot ${id}`)); const count = element('div', 'stat-value', String(items.length)); count.append(element('small', '', bytes(items.reduce((n, c) => n + c.size, 0)))); card.append(top, count); stats.append(card);
  }
  const pills = $('#provider-filters'); pills.replaceChildren();
  for (const [id, name] of [['all', 'All providers'], ...Object.entries(providers)]) pills.append(button(name, `pill ${state.provider === id ? 'active' : ''}`, () => { state.provider = id; render(); }));
  renderRows(); renderLocations(); renderTransfer(); renderCloud(); updateSelection();
}
function updateSelection() {
  $('#selected-count').textContent = state.selected.size; $('#export-button').disabled = state.selected.size === 0;
  const list = filtered(), chosen = list.filter(c => state.selected.has(c.id)).length;
  $('#select-all').checked = list.length > 0 && chosen === list.length; $('#select-all').indeterminate = chosen > 0 && chosen < list.length;
}
function renderRows() {
  const rows = $('#context-rows'); rows.replaceChildren(); const list = filtered(); $('#result-count').textContent = `${list.length} context${list.length === 1 ? '' : 's'}`;
  if (!list.length) { const empty = element('div', 'empty'); empty.append(element('h2', '', 'No contexts here yet.'), element('p', '', state.query ? 'Try another search or provider filter.' : 'Connect a context location or import a portable archive.')); rows.append(empty); return; }
  for (const c of list) {
    const row = element('div', `context-row ${state.active === c.id ? 'selected' : ''}`); row.tabIndex = 0; row.setAttribute('role', 'button'); row.setAttribute('aria-label', `Inspect ${c.title}`);
    row.onclick = () => run(() => selectContext(c.id)); row.onkeydown = e => { if (e.target === row && ['Enter', ' '].includes(e.key)) { e.preventDefault(); run(() => selectContext(c.id)); } };
    const check = element('input'); check.type = 'checkbox'; check.checked = state.selected.has(c.id); check.setAttribute('aria-label', `Select ${c.title}`); check.onclick = e => e.stopPropagation(); check.onchange = () => { check.checked ? state.selected.add(c.id) : state.selected.delete(c.id); updateSelection(); };
    const content = element('div', 'row-content'), title = element('div', 'row-title'); title.append(element('h3', '', `${c.starred ? '★ ' : ''}${c.title}`), element('time', '', date(c.updated)));
    const meta = element('div', 'row-meta'); meta.append(providerLabel(c.provider), document.createTextNode(' / '), element('span', 'project', c.project ? c.project.split(/[\\/]/).filter(Boolean).pop() : 'Project not recorded')); meta.title = c.project;
    const bottom = element('div', 'row-bottom'); bottom.append(element('span', '', bytes(c.size))); if (c.origin === 'imported') bottom.append(element('span', 'tag', 'imported')); for (const tag of (c.tags || []).slice(0, 3)) bottom.append(element('span', 'tag', tag));
    content.append(title, meta, bottom); row.append(check, content); rows.append(row);
  }
}
async function selectContext(id) { state.active = id; state.tab = 'messages'; state.messages = []; state.cursor = null; const generation = ++state.generation; renderRows(); renderInspector(true); const detail = await api.detail({ id }); if (state.generation !== generation) return; state.messages = detail.messages; state.cursor = detail.next; state.notice = detail.notice || (detail.skipped ? `${detail.skipped} malformed or oversized records skipped; originals remain intact.` : ''); renderInspector(); }
function activeContext() { return state.library.contexts.find(c => c.id === state.active); }
function renderInspector(loading = false) {
  const c = activeContext(); if (!c) return;
  const pane = $('#inspector'); pane.replaceChildren();
  const head = element('div', 'inspect-head'), top = element('div', 'inspect-top'); top.append(providerLabel(c.provider), button(c.starred ? '★ Starred' : '☆ Star', 'quiet', async () => { updateLibrary(await api.annotate({ id: c.id, starred: !c.starred })); renderInspector(); }));
  head.append(top, element('h2', '', c.title), element('div', 'project-path', c.project || 'Working directory not recorded'));
  const actions = element('div', 'inspect-actions');
  if (c.origin === 'local') {
    actions.append(button('▶ Chat here', 'button primary', () => openTerminal(c.id)), button('↗ Terminal', 'button', async () => { await api.resumeExternal(c.id); toast('Conversation opened in your terminal.'); }));
  } else actions.append(button('Restore files', 'button', () => operation('Restoring context files…', async () => { const result = await api.restoreContext(c.id); if (result) toast(`${result.count} files restored to ${result.path}`); })));
  actions.append(button('Show file', 'button', () => api.revealContext(c.id)), button('Export', 'button', () => exportContexts([c.id])));
  head.append(actions); pane.append(head);
  const tabs = element('div', 'tabs'); for (const [id, title] of [['messages', 'Conversation'], ['files', 'Original files'], ['metadata', 'Details & notes']]) tabs.append(button(title, `tab ${state.tab === id ? 'active' : ''}`, () => { state.tab = id; renderInspector(); })); pane.append(tabs);
  const body = element('div', 'inspect-body'); pane.append(body);
  if (loading) { body.append(element('div', 'empty', 'Loading conversation…')); return; }
  if (state.tab === 'messages') renderMessages(body);
  if (state.tab === 'files') {
    body.append(element('p', '', 'Original files included when you export this context.'));
    api.files({ id: c.id }).then(files => { if (state.active !== c.id || state.tab !== 'files') return; for (const f of files) { const row = element('div', 'file'); row.append(element('code', '', f.relative), element('small', '', bytes(f.size))); body.append(row); } }).catch(e => toast(e.message, true));
  }
  if (state.tab === 'metadata') renderMetadata(body, c);
}
function renderMessages(body) {
  const controls = element('div', 'detail-controls'), label = element('label'), check = element('input'); check.type = 'checkbox'; check.checked = state.tools; check.onchange = () => { state.tools = check.checked; renderInspector(); }; label.append(check, document.createTextNode('Show tools & system messages')); controls.append(label, element('span', '', `${state.messages.length} loaded`)); body.append(controls);
  if (state.notice) body.append(element('div', 'notice', state.notice));
  const visible = state.messages.filter(m => state.tools || ['user', 'assistant'].includes(m.role));
  for (const msg of visible) {
    const item = element('article', `message ${msg.role}`), meta = element('div', 'message-meta'); meta.append(element('span', '', msg.role), element('time', '', date(msg.timestamp))); item.append(meta);
    const content = element('pre', '', msg.content + (msg.truncated ? '\n\n[Display truncated; full content is in the original file.]' : ''));
    if (!['user', 'assistant'].includes(msg.role)) { const details = element('details'); details.append(element('summary', '', msg.kind || 'Expand content'), content); item.append(details); } else item.append(content);
    body.append(item);
  }
  if (!visible.length) body.append(element('p', '', state.cursor !== null ? 'No visible messages on this page. Load more or show system and tool messages.' : 'No readable conversation messages found. You can inspect and export the original files.'));
  if (state.cursor !== null) { const next = element('div', 'pagination'); const b = button('Load more messages ↓', 'button', async () => {
    b.disabled = true; const generation = state.generation, c = activeContext();
    try { const detail = await api.detail({ id: c.id, cursor: state.cursor }); if (generation !== state.generation) return; state.messages.push(...detail.messages); state.cursor = detail.next; const scroll = $('#inspector').scrollTop; renderInspector(); $('#inspector').scrollTop = scroll; } finally { b.disabled = false; }
  }); next.append(b); body.append(next); }
}
function renderMetadata(body, c) {
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
  for (const loc of state.library.locations) { const card = element('div', 'location-card'), content = element('div'), h = element('h3'); h.append(providerLabel(loc.provider)); content.append(h, element('code', '', loc.path)); card.append(content, element('span', `location-status ${loc.exists ? '' : 'missing'}`, loc.exists ? `${loc.count} contexts · connected` : 'Not found')); page.append(card); }
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
async function exportContexts(ids) { return operation('Packing original files and computing checksums…', async () => { const result = await api.exportBundle(ids); if (result) toast(`Exported ${result.count} contexts · ${bytes(result.size)} · ${result.path}`); }); }
async function importPreview() { await operation('Validating archive and checksums…', async () => { const preview = await api.chooseImport(); if (!preview) return; const body = $('#import-preview'); body.replaceChildren(element('div', 'muted', `${preview.contexts.length} contexts · ${preview.files} files · ${bytes(preview.size)}`)); for (const c of preview.contexts) body.append(element('div', 'preview-row', `${providers[c.provider]} / ${c.title}`)); $('#import-dialog').showModal(); }); }
$('#navigation').addEventListener('click', e => { const button = e.target.closest('[data-view]'); if (button) { state.view = button.dataset.view; render(); } });
$('#search').oninput = e => { state.query = e.target.value; renderRows(); updateSelection(); };
$('#sort').onchange = () => renderRows();
$('#select-all').onchange = e => { for (const c of filtered()) e.target.checked ? state.selected.add(c.id) : state.selected.delete(c.id); renderRows(); updateSelection(); };
$('#refresh').onclick = () => run(() => operation('Refreshing local stores…', async () => updateLibrary(await api.scan())));
$('#export-button').onclick = () => run(() => exportContexts([...state.selected]));
$('#import-button').onclick = () => run(importPreview);
$('#confirm-import').onclick = () => run(() => operation('Importing contexts into your library…', async () => { $('#confirm-import').disabled = true; try { updateLibrary(await api.importBundle()); $('#import-dialog').close(); state.view = 'imported'; render(); toast('Contexts imported. Original files are available in the inspector.'); } finally { $('#confirm-import').disabled = false; } }));
document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); state.view = 'all'; render(); $('#search').focus(); } });

// Terminals remain mounted when navigating the library, preserving the live CLI screen.
const terminalSessions = new Map(); let activeTerminal = null;
async function openTerminal(id) {
  if (terminalSessions.has(id)) { showTerminal(id); return; }
  const c = state.library.contexts.find(c => c.id === id);
  const session = await api.terminalStart(id);
  if (!session) return;
  const term = new Terminal({ cursorBlink: true, fontFamily: 'monospace', fontSize: 12, scrollback: 5000, theme: { background: '#111610', foreground: '#d7e3cf', cursor: '#d3f49a' } });
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  const host = element('div', 'terminal-host'); $('#terminal-content').append(host); term.open(host);
  terminalSessions.set(id, { term, fit, host, token: session.token, title: c.title, exited: false });
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
api.onTerminalExit(({ token, code }) => { for (const t of terminalSessions.values()) if (t.token === token) { t.exited = true; t.term.write(`\r\n\x1b[90m[Process exited: ${code}. Close this tab to resume again.]\x1b[0m\r\n`); } if (activeTerminal) showTerminal(activeTerminal); });
$('#terminal-hide').onclick = () => $('#terminal-dock').classList.add('hidden');
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
  const page = $('#cloud-view'); page.replaceChildren();
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
  setup.append(button('Import Google OAuth app JSON', 'button', async () => { const result = await api.cloudGoogleConfig(); if (result) { acceptCloud(result); toast('Google app registration configured. Users can now connect their account.'); } })); page.append(setup);
}
function acceptCloud(value) {
  cloudState = value; renderCloud();
  const job = value.job;
  if (!job) return;
  if (job.status === 'running') {
    if ($('#cloud-dialog').open && !$('#cloud-dialog-body input')) cloudProgress(job.message);
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
function cloudProgress(message) {
  const body = $('#cloud-dialog-body'); body.replaceChildren(element('div', 'eyebrow', 'CONNECT YOUR CLOUD'), element('h2', '', 'A moment, please.'), element('p', '', message), button('Cancel sign-in', 'button', async () => { acceptCloud(await api.cloudCancel()); $('#cloud-dialog').close(); }));
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
