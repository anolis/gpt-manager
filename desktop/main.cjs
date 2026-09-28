const { app, BrowserWindow, ipcMain, dialog, shell, clipboard } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
if (process.platform === 'linux') app.setDesktopName('io.github.anolis.gpt-manager.desktop');

let win, worker, seq = 0, chosenImport = null;
const pending = new Map();
const entry = pathToFileURL(path.join(__dirname, '../ui/index.html')).href;
function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    worker.stdin.write(JSON.stringify({ id, method, params }) + '\n', error => {
      if (error) { pending.delete(id); reject(error); }
    });
  });
}
function register(name, fn) {
  ipcMain.handle(name, (event, ...args) => {
    if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame || event.senderFrame.url !== entry) {
      throw new Error('Untrusted request');
    }
    return fn(...args);
  });
}
async function selectDirectory(title) {
  const result = await dialog.showOpenDialog(win, { title, properties: ['openDirectory', 'createDirectory'] });
  return result.canceled ? null : result.filePaths[0];
}
app.whenReady().then(async () => {
  app.setAppUserModelId('io.github.anolis.gpt-manager');
  if (process.platform === 'darwin') app.dock.setIcon(path.join(__dirname, '../ui/assets/icon.png'));
  if (process.argv.includes('--platform-smoke-test')) setTimeout(() => { console.error('Platform smoke exceeded one minute'); app.exit(1); }, 60000).unref();
  if (process.argv.includes('--smoke-test')) {
    const fixture = process.env.GPT_MANAGER_HOME;
    if (!fixture || !process.env.GPT_MANAGER_DATA || !fs.existsSync(path.join(fixture, '.gpt-manager-smoke-fixture')) || process.env.PATH.split(path.delimiter)[0] !== path.join(fixture, 'bin')) {
      console.error('Smoke tests require tests/seed_demo.py fixtures, GPT_MANAGER_HOME, GPT_MANAGER_DATA, and the fixture bin directory first on PATH.');
      app.exit(1); return;
    }
    delete process.env.CODEX_HOME;
    delete process.env.CLAUDE_CONFIG_DIR;
    process.env.HOME = fixture;
  }
  const data = process.env.GPT_MANAGER_DATA || app.getPath('userData');
  const backend = require('./runtime.cjs').backendCommand('rpc', app);
  worker = spawn(backend.command, [...backend.args, '--data', data, ...(process.env.GPT_MANAGER_HOME ? ['--home', process.env.GPT_MANAGER_HOME] : [])], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  worker.on('error', error => {
    if (process.argv.includes('--platform-smoke-test')) { console.error(error); app.exit(1); return; }
    dialog.showErrorBox('Backend could not start', `Install Python 3.10 or newer, or set GPT_MANAGER_PYTHON.\n\n${error.message}`);
    app.quit();
  });
  worker.stderr.on('data', data => console.error(String(data)));
  worker.on('exit', () => {
    for (const task of pending.values()) task.reject(new Error('The context backend stopped. Restart the app.'));
    pending.clear();
  });
  readline.createInterface({ input: worker.stdout }).on('line', line => {
    try {
      const msg = JSON.parse(line), task = pending.get(msg.id);
      if (msg.event === 'handoffProgress' && win && !win.isDestroyed()) win.webContents.send('handoffProgress', msg.message);
      if (task) { pending.delete(msg.id); msg.error ? task.reject(new Error(msg.error)) : task.resolve(msg.result); }
    } catch (e) { console.error('Invalid backend response', e.message); }
  });
  win = new BrowserWindow({ width: 1460, height: 960, minWidth: 1000, minHeight: 680,
    title: 'GPT Manager', icon: path.join(__dirname, '../ui/assets/icon.png'), backgroundColor: '#101311', autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.webContents.on('console-message', (_event, level, message) => { if (level >= 2) console.error('RENDERER: ' + message); });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  for (const name of ['scan', 'library', 'detail', 'annotate', 'files']) register(name, params => rpc(name, params));
  for (const name of ['status', 'install', 'cancel', 'prefer']) register('setup' + name[0].toUpperCase() + name.slice(1), params => rpc('setup_' + name, params));
  for (const name of ['status', 'refresh', 'configure']) register('usage' + name[0].toUpperCase() + name.slice(1), params => rpc('usage_' + name, params));
  register('setupAuthRefresh', params => rpc('setup_auth_refresh', params));
  register('setupUninstall', async params => {
    if (terminalController.hasProviderRunning(params.provider)) throw new Error('Close this provider’s manager terminals before uninstalling its managed copy.');
    if ((await rpc('catchup_status')).job?.status === 'running' || (await rpc('catchup_schedule_status')).lastStatus === 'running' || (await rpc('usage_status')).running) throw new Error('Wait for the current recap or usage check before uninstalling.');
    return rpc('setup_uninstall', params);
  });
  register('catchupScheduleStatus', () => rpc('catchup_schedule_status'));
  register('catchupScheduleConfigure', params => rpc('catchup_schedule_configure', params));
  for (const name of ['status', 'prepare', 'generate', 'cancel', 'history', 'get', 'delete']) register('catchup' + name[0].toUpperCase() + name.slice(1), params => rpc('catchup_' + name, params));
  for (const [name, method] of Object.entries({ sshAdd: 'ssh_add', sshRemove: 'ssh_remove', sshRefresh: 'ssh_refresh', sshAliases: 'ssh_aliases', localNetworks: 'local_networks', scanNetwork: 'scan_network' })) register(name, params => rpc(method, params));
  register('handoffRelease', async id => {
    const answer = await dialog.showMessageBox(win, { type: 'warning', message: 'Allow this source context to resume again?', detail: 'This reopens the retained, older copy; it does not bring back newer messages. To return the latest history, cancel and choose Resume here on the destination machine’s SSH copy. Only release after stopping that conversation. No histories will be merged.', buttons: ['Keep handed off', 'Release handoff'], defaultId: 0, cancelId: 0 });
    return answer.response === 1 ? rpc('handoff_release', { id }) : null;
  });
  register('resumeHere', async id => {
    const context = (await rpc('library')).contexts.find(c => c.id === id);
    if (!context || context.origin !== 'remote') throw new Error('Select an SSH context.');
    if (terminalController.isRunning(id)) throw new Error('Stop this remote terminal before handing the context off.');
    const mode = await dialog.showMessageBox(win, { type: 'question', message: 'Where should this conversation work locally?', detail: `Source: ${context.machine}\nProject: ${context.project || 'Not recorded'}\n\nUse an existing local folder (source workspace changes will not be copied), or copy the remote work folder including .git, hidden files, dependencies and uncommitted changes. Place folder with files creates a new project folder inside your selected parent directory (for example ~/repos/project-name). Place files in folder copies the contents directly into an empty folder you select. Existing files are never replaced. Stop external provider sessions and edits on the source first. GPT Manager will reserve the source and retain its context files as a backup. If this is a return to a previously handed-off copy, the original handoff is verified and that older copy is backed up before restoring the latest history.`, buttons: ['Cancel', 'Use existing folder', 'Place folder with files', 'Place files in folder'], defaultId: 1, cancelId: 0 });
    if (!mode.response) return null;
    const folder = await selectDirectory(mode.response === 2 ? 'Choose the parent folder for the copied project' : mode.response === 3 ? 'Choose an empty folder for the project files' : 'Choose the existing local project folder');
    if (!folder) return null;
    if (mode.response === 1) {
      let git;
      try { git = await rpc('git_check', { folder }); }
      catch (error) {
        const answer = await dialog.showMessageBox(win, { type: 'warning', message: 'Could not check Git updates', detail: `${error.message}\n\nContinue with the local files as they are?`, buttons: ['Cancel', 'Use current files'], defaultId: 0, cancelId: 0 });
        if (answer.response === 0) return null;
        git = {};
      }
      if (git.behind > 0) {
        const canPull = !git.dirty && git.ahead === 0;
        const answer = await dialog.showMessageBox(win, { type: 'question', message: `${git.behind} upstream commit(s) available from ${git.upstream}`, detail: canPull ? 'Fetch completed. Apply a fast-forward update to the local checkout before resuming?' : 'The checkout has local changes or divergent commits. Update it manually, or resume with its current files. GPT Manager will not merge or discard your work.', buttons: canPull ? ['Cancel', 'Keep current files', 'Pull updates'] : ['Cancel', 'Keep current files'], defaultId: 0, cancelId: 0 });
        if (answer.response === 0) return null;
        if (answer.response === 2) await rpc('git_check', { folder, pull: true });
      }
    }
    const params = { id, folder, copy_workspace: mode.response >= 2, workspace_layout: mode.response === 3 ? 'contents' : 'folder' };
    const result = await rpc('teleport', params);
    if (result.conflict !== 'retained-changed') return result;
    const answer = await dialog.showMessageBox(win, { type: 'warning', message: 'The local conversation changed after the handoff', detail: `Use the conversation from ${result.sourceMachine} instead?\n\nThis replaces the local conversation, including any messages added here after the handoff. GPT Manager will save a recovery backup first. Your project files are not discarded. Stop any local provider session using this conversation before continuing.\n\nLocal conversation: ${result.localPath}`, buttons: ['Cancel', 'Trash local changes and continue'], defaultId: 0, cancelId: 0 });
    if (answer.response !== 1) return null;
    return rpc('teleport', { ...params, discard_local_changes: true, expected_local: result.expectedLocal });
  });
  register('addRoot', async provider => {
    const folder = await selectDirectory('Choose provider store: Codex home, Claude projects, Gemini tmp, or Antigravity data root');
    return folder ? rpc('add_root', { provider, path: folder }) : null;
  });
  register('exportBundle', async ids => {
    const result = await dialog.showSaveDialog(win, { title: 'Export portable context archive',
      defaultPath: `contexts-${new Date().toISOString().slice(0, 10)}.gptctx`,
      filters: [{ name: 'GPT Manager archive', extensions: ['gptctx'] }] });
    return result.canceled ? null : rpc('export', { ids, destination: result.filePath });
  });
  register('chooseImport', async () => {
    const result = await dialog.showOpenDialog(win, { title: 'Inspect context archive', properties: ['openFile'],
      filters: [{ name: 'GPT Manager archive', extensions: ['gptctx', 'zip'] }] });
    if (result.canceled) return null;
    chosenImport = null;
    const preview = await rpc('preview', { path: result.filePaths[0] });
    chosenImport = result.filePaths[0];
    return preview;
  });
  register('importBundle', async () => {
    if (!chosenImport) throw new Error('Choose an archive first');
    const file = chosenImport;
    chosenImport = null;
    return rpc('import_archive', { path: file });
  });
  register('restoreContext', async id => {
    const folder = await selectDirectory('Choose destination provider root (an empty staging folder is recommended)');
    if (!folder) return null;
    const result = await dialog.showMessageBox(win, { type: 'question', title: 'Restore original context files',
      message: 'Restore this context to the selected directory?',
      detail: `${folder}\n\nClose the provider before restoring into its store. Existing files are never replaced. Provider indexes, credentials, and project files are not included; native resume may require additional setup.`,
      buttons: ['Cancel', 'Restore files'], defaultId: 0, cancelId: 0 });
    return result.response === 1 ? rpc('restore', { id, destination: folder }) : null;
  });
  register('revealContext', async id => {
    const library = await rpc('library');
    const item = library.contexts.find(x => x.id === id);
    if (!item) throw new Error('Context unavailable');
    shell.showItemInFolder(item.path);
  });
  for (const [name, method] of Object.entries({ cloudStatus: 'status', cloudConnect: 'connect', cloudAnswer: 'answer', cloudCancel: 'cancel', cloudConfigure: 'configure', cloudSync: 'sync', cloudTick: 'tick' })) {
    register(name, params => rpc('cloud_' + method, params));
  }
  register('cloudFolder', async provider => {
    const folder = await selectDirectory('Choose a folder already synced by your cloud app or mount');
    return folder ? rpc('cloud_folder', { provider, path: folder }) : null;
  });
  register('cloudDisconnect', async id => {
    const answer = await dialog.showMessageBox(win, { type: 'question', message: 'Disconnect this cloud location?', detail: 'Sync will stop and local connection credentials will be removed. Existing cloud archives and imported contexts will remain.', buttons: ['Cancel', 'Disconnect'], defaultId: 0, cancelId: 0 });
    return answer.response === 1 ? rpc('cloud_disconnect', { id }) : null;
  });
  register('cloudGoogleConfig', async () => {
    const file = await dialog.showOpenDialog(win, { title: 'Maintainer setup: choose Google OAuth Desktop client JSON', properties: ['openFile'], filters: [{ name: 'OAuth client JSON', extensions: ['json'] }] });
    if (file.canceled) return null;
    const stat = fs.statSync(file.filePaths[0]);
    if (stat.size > 1024 * 1024) throw new Error('OAuth client file is too large');
    return rpc('cloud_configure_google', { config: JSON.parse(fs.readFileSync(file.filePaths[0], 'utf8')) });
  });
  register('changeProject', async id => {
    if (terminalController.isRunning(id)) throw new Error('Stop this context’s embedded terminal before changing its project folder.');
    const folder = await selectDirectory('Choose the project folder to use when resuming this context');
    return folder ? rpc('project_folder', { id, destination: folder }) : null;
  });
  register('moveContextFiles', async id => {
    if (terminalController.isRunning(id)) throw new Error('Stop this context’s embedded terminal before moving its files.');
    const folder = await selectDirectory('Choose the new provider context-store root');
    if (!folder) return null;
    const plan = await rpc('plan_move', { id, destination: folder });
    const answer = await dialog.showMessageBox(win, { type: 'question', message: `Move ${plan.files.length} original context files?`,
      detail: `From: ${plan.sourceRoot}\nTo: ${plan.destinationRoot}\n\nClose this conversation in external provider apps first. A recovery archive will be kept in GPT Manager. Existing destination files are never overwritten. Project source files are not moved. Provider authentication and shared indexes remain in their original store.`, buttons: ['Cancel', 'Move files'], defaultId: 0, cancelId: 0 });
    return answer.response === 1 ? rpc('move_files', { id, destination: folder }) : null;
  });
  register('copyText', text => {
    if (typeof text !== 'string' || text.length > 200000) throw new Error('Invalid clipboard text');
    clipboard.writeText(text);
  });
  const terminalController = require('./terminals.cjs').installTerminals({ app, win, register, rpc, dialog, selectDirectory });
  await win.loadFile(path.join(__dirname, '../ui/index.html'));
  if (process.argv.includes('--platform-smoke-test')) {
    try {
      if (!process.env.GPT_MANAGER_HOME || !process.env.GPT_MANAGER_DATA) throw new Error('Platform smoke requires isolated fixture paths.');
      const library = await rpc('scan');
      const setup = await rpc('setup_status');
      if (!Array.isArray(library.contexts) || setup.providers.length !== 4) throw new Error('Backend smoke failed');
      await new Promise((resolve, reject) => {
        const pty = require('node-pty').spawn('powershell.exe', ['-NoProfile', '-Command', "Write-Output 'PTY_READY'; $value = [Console]::ReadLine(); Write-Output ('REPLY:' + $value)"], { cols: 100, rows: 24, cwd: process.env.GPT_MANAGER_HOME, env: process.env, useConpty: true });
        let output = '', sent = false;
        const timer = setTimeout(() => { pty.kill(); reject(new Error('ConPTY timed out: ' + output)); }, 20000);
        pty.onData(data => { output += data; if (output.includes('PTY_READY') && !sent) { sent = true; pty.resize(120, 30); pty.write('hello-context\r'); } });
        pty.onExit(({ exitCode }) => { clearTimeout(timer); exitCode === 0 && output.includes('REPLY:hello-context') ? resolve() : reject(new Error('ConPTY failed: ' + output)); });
      });
      await win.webContents.executeJavaScript("state.view = 'setup'; render();");
      fs.writeFileSync(path.join(data, 'platform-smoke.json'), JSON.stringify({ backend: true, conpty: true, setup: true }));
      app.exit(0);
    } catch (error) { console.error(error); app.exit(1); }
    return;
  }
  if (process.argv.includes('--smoke-test')) {
    try {
      for (let i = 0; i < 120; i++) {
        if (await win.webContents.executeJavaScript('Boolean(window.__ready)')) break;
        await new Promise(r => setTimeout(r, 250));
      }
      const result = await win.webContents.executeJavaScript(`(async () => {
        if (!window.__ready) throw new Error('Library did not become ready');
        const row = document.querySelector('.context-row');
        if (row) { row.click(); await new Promise(r => setTimeout(r, 700)); }
        return { rows: document.querySelectorAll('.context-row').length, title: document.title,
          transcript: document.querySelectorAll('.message').length, error: document.querySelector('#toast.error')?.textContent || '' };
      })()`);
      const interaction = await win.webContents.executeJavaScript(`(async () => {
        const delay = ms => new Promise(r => setTimeout(r, ms));
        document.querySelector('#search').value = 'telescope';
        document.querySelector('#search').dispatchEvent(new Event('input'));
        if (document.querySelectorAll('.context-row').length !== 1) throw new Error('Search filtering failed');
        document.querySelector('#search').value = '';
        document.querySelector('#search').dispatchEvent(new Event('input'));
        document.querySelector('[data-view="locations"]').click();
        if (!document.querySelector('#locations-view').textContent.includes('SSH endpoints') || !document.querySelector('#locations-view').textContent.includes('Scan local network')) throw new Error('SSH location controls missing');
        await addSshDialog();
        if (!document.querySelector('#ssh-host')) throw new Error('SSH endpoint form missing');
        document.querySelector('#ssh-host').closest('dialog').close();
        document.querySelector('[data-view="setup"]').click();
        for (let i = 0; i < 40 && !setupState; i++) await delay(50);
        if (document.querySelectorAll('#setup-view .transfer-card').length !== 4) throw new Error('Provider setup cards missing');
        for (let i = 0; i < 100 && setupState.providers[0].auth?.state !== 'signed_in'; i++) { await delay(100); await loadSetup(); }
        if (setupState.providers[0].auth?.state !== 'signed_in') throw new Error('Provider sign-in status missing');
        const originalSetup = setupState;
        setupState = { ...setupState, providers: [{ ...setupState.providers[0], managedInstalled: true, managedAvailable: true, managedVersion: 'fixture' }] }; renderSetup();
        if (!document.querySelector('#setup-view').textContent.includes('Uninstall managed copy') || !document.querySelector('#setup-view').textContent.includes('Update managed copy')) throw new Error('Managed installation actions missing');
        setupState = originalSetup; renderSetup();
        document.querySelector('[data-view="usage"]').click();
        for (let i = 0; i < 100 && (!usageState || usageState.running); i++) { await delay(100); await loadUsage(); }
        if (document.querySelectorAll('#usage-view progress').length !== 2) throw new Error('Usage quota bars missing');
        document.querySelector('[data-view="catchup"]').click();
        for (let i = 0; i < 40 && !catchupLoaded; i++) await delay(50);
        for (let i = 0; i < 40 && !recapScheduleLoaded; i++) await delay(50);
        if (!document.querySelector('#recap-schedule-status') || !document.querySelector('#catchup-view').textContent.includes('One recap, all your services.')) throw new Error('Daily recap controls or aside missing');
        document.querySelector('#catchup-period').value = 'seven';
        document.querySelector('#catchup-review').click();
        for (let i = 0; i < 120 && (!document.querySelector('#catchup-generate') || document.querySelector('#catchup-generate').disabled); i++) await delay(100);
        if (!document.querySelector('#catchup-generate') || catchupState.preview.sources.length !== 4) throw new Error('Catch-up activity preview failed');
        document.querySelector('#catchup-generate').click();
        for (let i = 0; i < 120 && !catchupRecord; i++) await delay(100);
        if (!document.querySelector('.catchup-project') || !catchupRecord) throw new Error('Catch-up recap generation failed: ' + JSON.stringify(catchupState.job));
        document.querySelector('[data-view="cloud"]').click();
        if (document.querySelectorAll('.cloud-card').length !== 3) throw new Error('Cloud provider cards missing');
        document.querySelector('.maintainer-setup summary').click();
        await delay(2200);
        if (!document.querySelector('.maintainer-setup').open) throw new Error('Cloud polling closed maintainer setup');
        document.querySelector('.maintainer-setup summary').click();
        await delay(1200);
        if (document.querySelector('.maintainer-setup').open) throw new Error('Cloud polling reopened maintainer setup');
        cloudSignIn('icloud');
        if (!document.querySelector('#apple-email') || !document.querySelector('#apple-password')) throw new Error('Apple sign-in form missing');
        document.querySelector('#cloud-dialog').close();
        cloudProgress('Downloading cloud connector…', { received: 1048576, total: 2097152 });
        if (document.querySelector('.setup-progress').value !== 50 || !document.querySelector('.setup-download').textContent.includes('50%')) throw new Error('Download progress missing');
        const cancelButton = document.querySelector('#cloud-dialog-body button');
        cloudProgress('Verifying cloud connector download…');
        if (document.querySelector('.setup-progress').hasAttribute('value') || document.querySelector('#cloud-dialog-body button') !== cancelButton) throw new Error('Setup stage transition failed');
        document.querySelector('[data-view="all"]').click();
        const codex = state.library.contexts.find(c => c.provider === 'codex');
        await selectContext(codex.id);
        const inspector = document.querySelector('#inspector');
        let messagesPane = inspector.querySelector('.inspect-body');
        const actionsTop = inspector.querySelector('.inspect-actions').getBoundingClientRect().top;
        if (!inspector.classList.contains('compact-header') || messagesPane.clientHeight < 200) throw new Error('Latest conversation did not open with a compact header');
        const compactHeight = messagesPane.clientHeight;
        messagesPane.scrollTop -= 70;
        await delay(100);
        if (inspector.classList.contains('compact-header') || messagesPane.clientHeight >= compactHeight) throw new Error('Scrolling up did not expand conversation details');
        messagesPane.scrollTop += 90;
        await delay(100);
        if (!inspector.classList.contains('compact-header')) throw new Error('Scrolling down did not collapse conversation details');
        const stablePosition = messagesPane.scrollTop;
        await delay(100);
        if (Math.abs(messagesPane.scrollTop - stablePosition) > 1) throw new Error('Header resize caused a scroll feedback loop');
        inspector.querySelector('.inspector-toggle').click();
        if (inspector.classList.contains('compact-header')) throw new Error('Show details button did not expand');
        inspector.querySelector('.inspector-toggle').click();
        messagesPane.setScrollPosition(messagesPane.scrollHeight);
        if (!state.messages.at(-1).content.includes('History message 449')) throw new Error('Conversation did not load latest history');
        if (Math.abs(messagesPane.scrollHeight - messagesPane.clientHeight - messagesPane.scrollTop) > 2) throw new Error('Conversation did not open at bottom');
        const oldestVisible = inspector.querySelector('.message');
        messagesPane.setScrollPosition(0);
        if (inspector.scrollTop !== 0 || Math.abs(inspector.querySelector('.inspect-actions').getBoundingClientRect().top - actionsTop) > 1) throw new Error('Conversation actions scrolled with messages');
        const anchorText = oldestVisible.querySelector('pre').textContent;
        const anchorTop = oldestVisible.getBoundingClientRect().top;
        inspector.querySelector('.pagination button').click();
        for (let attempt = 0; attempt < 100 && state.messages.length === 200; attempt++) await delay(50);
        if (state.messages.length !== 400) throw new Error('Older page did not prepend');
        const anchor = [...inspector.querySelectorAll('.message')].find(item => item.querySelector('pre').textContent === anchorText);
        if (!anchor || Math.abs(anchor.getBoundingClientRect().top - anchorTop) > 2) throw new Error('Loading older history moved the reading position');
        if (!state.messages.at(-1).content.includes('History message 449')) throw new Error('Older page replaced latest history');
        messagesPane = inspector.querySelector('.inspect-body');
        messagesPane.setScrollPosition(messagesPane.scrollHeight);
        if (Math.abs(inspector.querySelector('.inspect-actions').getBoundingClientRect().top - actionsTop) > 1) throw new Error('Actions moved after older history loaded');
        inspector.querySelectorAll('.tab')[1].click();
        inspector.querySelectorAll('.tab')[0].click();
        messagesPane = inspector.querySelector('.inspect-body');
        if (Math.abs(messagesPane.scrollHeight - messagesPane.clientHeight - messagesPane.scrollTop) > 2) throw new Error('Returning to conversation did not scroll to latest');
        await delay(100);
        if (!inspector.classList.contains('compact-header')) throw new Error('An old scroll container expanded the current header');
        await openTerminal(codex.id);
        const session = terminalSessions.get(codex.id);
        if (!session) throw new Error('Terminal failed to start');
        await delay(600);
        await window.manager.terminalInput({ token: session.token, data: 'terminal-smoke-test\\n' });
        await delay(600);
        let text = '';
        for (let i = 0; i < session.term.buffer.active.length; i++) text += session.term.buffer.active.getLine(i).translateToString();
        if (!text.includes('ECHO:terminal-smoke-test')) throw new Error('PTY input did not reach renderer: ' + text);
        await window.manager.terminalInput({ token: session.token, data: 'exit\\n' });
        await delay(400);
        if (!session.exited) throw new Error('PTY exit was not observed');
        document.querySelector('#terminal-dock').classList.add('hidden');
        if (!inspector.classList.contains('compact-header')) throw new Error('Opening the terminal expanded the conversation header');
        inspector.scrollIntoView({ block: 'end' });
        return { search: true, cloudUI: true, pty: true, terminalExit: true };
      })()`);
      if (process.env.GPT_MANAGER_SCREENSHOT_VIEW === 'usage') await win.webContents.executeJavaScript("state.view = 'usage'; render();");
      if (process.env.GPT_MANAGER_SCREENSHOT_VIEW === 'schedule') await win.webContents.executeJavaScript("state.view = 'catchup'; render(); document.querySelector('#recap-schedule').open = true; window.scrollTo(0, 0);");
      if (process.env.GPT_MANAGER_SCREENSHOT_VIEW === 'setup') await win.webContents.executeJavaScript("state.view = 'setup'; render();");
      if (process.env.GPT_MANAGER_SCREENSHOT_VIEW === 'cloud') await win.webContents.executeJavaScript("state.view = 'cloud'; render();");
      if (process.env.GPT_MANAGER_SCREENSHOT_VIEW === 'locations') await win.webContents.executeJavaScript("state.view = 'locations'; render();");
      if (process.env.GPT_MANAGER_SCREENSHOT_VIEW === 'catchup') await win.webContents.executeJavaScript("state.view = 'catchup'; render(); document.querySelector('#catchup-result').scrollIntoView();");
      await new Promise(r => setTimeout(r, 300));
      console.log('INTERACTION_RESULT ' + JSON.stringify(interaction));
      const output = process.env.GPT_MANAGER_SCREENSHOT;
      if (output) fs.writeFileSync(output, (await win.webContents.capturePage()).toPNG());
      console.log('SMOKE_RESULT ' + JSON.stringify(result));
      app.exit(result.error || !result.rows || !result.transcript ? 1 : 0);
    } catch (e) { console.error(e); app.exit(1); }
  }
});
app.on('window-all-closed', () => app.quit());
app.on('quit', () => {
  if (process.platform === 'win32' && worker?.pid && worker.exitCode === null) spawnSync('taskkill', ['/PID', String(worker.pid), '/T', '/F'], { windowsHide: true, timeout: 5000, stdio: 'ignore' });
  else worker?.kill();
});
