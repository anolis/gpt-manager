const { app, BrowserWindow, ipcMain, dialog, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');

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
  if (process.argv.includes('--smoke-test')) {
    const fixture = process.env.GPT_MANAGER_HOME;
    if (!fixture || !process.env.GPT_MANAGER_DATA || !fs.existsSync(path.join(fixture, '.gpt-manager-smoke-fixture')) || process.env.PATH.split(path.delimiter)[0] !== path.join(fixture, 'bin')) {
      console.error('Smoke tests require tests/seed_demo.py fixtures, GPT_MANAGER_HOME, GPT_MANAGER_DATA, and the fixture bin directory first on PATH.');
      app.exit(1); return;
    }
    delete process.env.CODEX_HOME;
    delete process.env.CLAUDE_CONFIG_DIR;
  }
  const data = process.env.GPT_MANAGER_DATA || app.getPath('userData');
  worker = spawn(process.env.GPT_MANAGER_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
    [path.join(__dirname, '../backend/rpc.py'), '--data', data,
      ...(process.env.GPT_MANAGER_HOME ? ['--home', process.env.GPT_MANAGER_HOME] : [])],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  worker.on('error', error => {
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
      if (task) { pending.delete(msg.id); msg.error ? task.reject(new Error(msg.error)) : task.resolve(msg.result); }
    } catch (e) { console.error('Invalid backend response', e.message); }
  });
  win = new BrowserWindow({ width: 1460, height: 960, minWidth: 1000, minHeight: 680,
    title: 'GPT Manager', backgroundColor: '#101311', autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.webContents.on('console-message', (_event, level, message) => { if (level >= 2) console.error('RENDERER: ' + message); });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  for (const name of ['scan', 'library', 'detail', 'annotate', 'files']) register(name, params => rpc(name, params));
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
  register('copyText', text => {
    if (typeof text !== 'string' || text.length > 200000) throw new Error('Invalid clipboard text');
    clipboard.writeText(text);
  });
  require('./terminals.cjs').installTerminals({ app, win, register, rpc, dialog, selectDirectory });
  await win.loadFile(path.join(__dirname, '../ui/index.html'));
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
        const codex = state.library.contexts.find(c => c.provider === 'codex');
        await selectContext(codex.id);
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
        return { search: true, pty: true, terminalExit: true };
      })()`);
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
app.on('quit', () => worker?.kill());
