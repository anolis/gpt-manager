const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { backendCommand } = require('./runtime.cjs');
const { randomUUID } = require('node:crypto');

function executable(name, env = process.env) {
  if (path.isAbsolute(name)) { fs.accessSync(name, fs.constants.X_OK); return name; }
  for (const folder of (env.PATH || '').split(path.delimiter)) {
    if (!folder || !path.isAbsolute(folder)) continue;
    const file = path.join(folder, process.platform === 'win32' && !path.extname(name) ? name + '.exe' : name);
    try { fs.accessSync(file, fs.constants.X_OK); if (fs.statSync(file).isFile()) return file; } catch {}
  }
  throw new Error(`${name} is not installed or is not on PATH. Install the provider CLI and reopen GPT Manager from your shell.`);
}
function resumeCommand(context, resolved) {
  if (context.origin !== 'local') throw new Error('Restore imported context files into a provider store, then refresh and resume the local copy.');
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}$/.test(context.sessionId)) throw new Error('This context has no valid resume ID.');
  const commands = { codex: ['codex', 'resume'], claude: ['claude', '--resume'], gemini: ['gemini', '--resume'], antigravity: ['agy', '--conversation'] };
  const command = commands[context.provider];
  if (!command) throw new Error('Unsupported provider');
  if (context.provider === 'antigravity' && !context.root.endsWith('antigravity-cli')) throw new Error('This Antigravity store is not an agy CLI store. IDE-only contexts cannot be resumed through agy.');
  const env = { ...process.env, ...(resolved?.env || {}) };
  if (context.provider === 'codex') env.CODEX_HOME = context.root;
  if (context.provider === 'claude') env.CLAUDE_CONFIG_DIR = path.dirname(context.root);
  // Provider CLIs must keep their own interactive approval behavior.
  return { command: resolved?.argv[0] || executable(command[0], env), args: [...(resolved?.argv.slice(1) || []), ...command.slice(1), context.sessionId], env };
}
function installTerminals({ app, win, register, rpc, dialog, selectDirectory }) {
  const sessions = new Map(), folders = new Map();
  async function config(id) {
    const context = (await rpc('library')).contexts.find(c => c.id === id);
    if (!context) throw new Error('Context not available');
    if (context.copies?.length) {
      const answer = await dialog.showMessageBox(win, { type: 'warning', message: `Resume on ${context.machine}?`, detail: `Other copies of this session were found on: ${context.copies.join(', ')}. Stop any running copies before continuing. GPT Manager cannot lock independent copies on other machines.`, buttons: ['Cancel', 'Resume this copy'], defaultId: 0, cancelId: 0 });
      if (answer.response !== 1) return null;
    }
    if (context.origin === 'remote') {
      const remote = await rpc('ssh_resume', { id });
      return { command: executable('ssh'), args: remote.args, env: { ...process.env }, cwd: app.getPath('home'), machine: remote.machine };
    }
    const command = resumeCommand(context, await rpc('provider_command', { provider: context.provider }));
    let cwd = context.project || folders.get(id);
    if (!cwd || !path.isAbsolute(cwd) || !fs.existsSync(cwd) || !fs.statSync(cwd).isDirectory()) {
      cwd = await selectDirectory('Choose the project folder for this conversation');
      if (!cwd) return null;
      folders.set(id, cwd);
    }
    if (context.provider === 'codex') command.args.push('--cd', cwd);
    if (context.provider === 'claude' && (await rpc('usage_status')).claudeMeter) {
      const capture = backendCommand('usage', app);
      const argv = [capture.command, ...capture.args, path.join(process.env.GPT_MANAGER_DATA || app.getPath('userData'), 'usage', 'claude.json'), context.root];
      const quote = text => "'" + text.replace(/'/g, "'\"'\"'") + "'";
      let hook = argv.map(quote).join(' ');
      if (process.platform === 'win32') {
        const script = '& ' + argv.map(text => "'" + text.replace(/'/g, "''") + "'").join(' ');
        hook = 'powershell.exe -NoProfile -EncodedCommand ' + Buffer.from(script, 'utf16le').toString('base64');
      }
      command.args.push('--settings', JSON.stringify({ statusLine: { type: 'command', command: hook } }));
    }

    const backend = backendCommand('lock', app);
    return { command: backend.command, args: [...backend.args, context.provider, context.sessionId, cwd, command.command, ...command.args], env: command.env, cwd, machine: context.machine };
  }
  function emit(session, channel, value) {
    if (!session.attached) { session.buffer.push([channel, value]); if (session.buffer.length > 256) session.buffer.shift(); }
    else if (!win.isDestroyed()) win.webContents.send(channel, { token: session.token, ...value });
  }
  async function start(id, provided) {
    for (const s of sessions.values()) if (s.id === id && !s.exited) throw new Error('This conversation is already open in a terminal.');
    if (sessions.size >= 8) throw new Error('Close a terminal tab before opening another (limit: 8).');
    const cfg = provided || await config(id); if (!cfg) return null;
    const token = randomUUID();
    const session = { id, token, buffer: [], attached: false, exited: false }; sessions.set(token, session);
    try {
      if (process.platform === 'win32') {
        const pty = require('node-pty').spawn(cfg.command, cfg.args, { name: 'xterm-256color', cols: 100, rows: 24, cwd: cfg.cwd, env: cfg.env, useConpty: true });
        session.input = data => pty.write(data);
        session.resize = (cols, rows) => pty.resize(Math.max(2, Math.min(500, cols)), Math.max(2, Math.min(300, rows)));
        session.kill = () => { if (!session.exited) pty.kill(); };
        pty.onData(data => emit(session, 'terminalData', { data: Buffer.from(data, 'utf8').toString('base64') }));
        pty.onExit(({ exitCode }) => { session.exited = true; emit(session, 'terminalExit', { code: exitCode }); });
      } else {
        const backend = backendCommand('terminal', app);
        const child = spawn(backend.command, [...backend.args, cfg.cwd, cfg.command, ...cfg.args], { env: cfg.env, stdio: ['pipe', 'pipe', 'pipe'] });
        session.input = data => child.stdin.write(JSON.stringify({ type: 'input', data }) + '\n');
        session.resize = (cols, rows) => child.stdin.write(JSON.stringify({ type: 'resize', cols, rows }) + '\n');
        session.kill = () => child.kill();
        child.stdin.on('error', () => {});
        child.on('error', e => { session.exited = true; emit(session, 'terminalData', { data: Buffer.from(e.message).toString('base64') }); emit(session, 'terminalExit', { code: 1 }); });
        child.stderr.on('data', chunk => emit(session, 'terminalData', { data: chunk.toString('base64') }));
        readline.createInterface({ input: child.stdout }).on('line', line => {
          try { const value = JSON.parse(line); if (value.data) emit(session, 'terminalData', { data: value.data }); if (value.exit !== undefined) session.exitCode = value.exit; } catch {}
        });
        child.on('exit', code => { session.exited = true; emit(session, 'terminalExit', { code: session.exitCode ?? code }); });
      }
    } catch (error) { sessions.delete(token); throw error; }
    return { token, cwd: cfg.cwd, machine: cfg.machine };
  }
  register('terminalStart', id => start(id));
  register('providerSignIn', async provider => {
    if (!['codex', 'claude', 'gemini'].includes(provider)) throw new Error('Unknown provider');
    const resolved = await rpc('provider_command', { provider });
    const args = provider === 'codex' ? ['login'] : provider === 'claude' ? ['auth', 'login'] : [];
    const cwd = path.join(app.getPath('userData'), 'provider-sign-in'); fs.mkdirSync(cwd, { recursive: true });
    const env = { ...resolved.env }; delete env.CLAUDECODE;
    return start('setup:' + provider, { command: resolved.argv[0], args: [...resolved.argv.slice(1), ...args], env, cwd, machine: 'Account setup' });
  });
  register('terminalAttach', token => { const s = sessions.get(token); if (!s) return; s.attached = true; for (const [channel, value] of s.buffer) emit(s, channel, value); s.buffer = []; });
  register('terminalInput', ({ token, data }) => { const s = sessions.get(token); if (!s || s.exited || typeof data !== 'string' || data.length > 100000) return; s.input(data); });
  register('terminalResize', ({ token, cols, rows }) => { const s = sessions.get(token); if (!s || s.exited || !Number.isInteger(cols) || !Number.isInteger(rows)) return; s.resize(cols, rows); });
  register('terminalClose', async token => {
    const s = sessions.get(token); if (!s) return true;
    if (!s.exited) { const answer = await dialog.showMessageBox(win, { type: 'question', message: 'Stop this running terminal session?', detail: 'The provider process will be stopped. Saved conversation history remains in its provider store.', buttons: ['Keep open', 'Stop session'], defaultId: 0, cancelId: 0 }); if (answer.response !== 1) return false; }
    s.kill(); sessions.delete(token); return true;
  });
  register('resumeExternal', async id => {
    const cfg = await config(id); if (!cfg) return null;
    if (process.platform === 'win32') {
      // PowerShell encoded scripts preserve literal argv without cmd.exe expansion.
      const quote = value => "'" + value.replace(/'/g, "''") + "'";
      const script = '& ' + [cfg.command, ...cfg.args].map(quote).join(' ');
      const child = spawn('powershell.exe', ['-NoProfile', '-NoExit', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], { cwd: cfg.cwd, env: cfg.env, detached: true, stdio: 'ignore', windowsHide: false });
      await new Promise((resolve, reject) => { child.once('error', reject); child.once('spawn', resolve); }); child.unref(); return true;
    }
    if (process.platform !== 'linux') throw new Error('Use Chat here for an embedded terminal on macOS.');
    let terminal; for (const name of ['x-terminal-emulator', 'gnome-terminal', 'konsole', 'xterm']) { try { terminal = executable(name); break; } catch {} }
    if (!terminal) throw new Error('No supported terminal found. Install x-terminal-emulator, GNOME Terminal, Konsole, or xterm.');
    const args = path.basename(terminal) === 'gnome-terminal' ? ['--', cfg.command, ...cfg.args] : ['-e', cfg.command, ...cfg.args];
    await new Promise((resolve, reject) => { const child = spawn(terminal, args, { cwd: cfg.cwd, env: cfg.env, detached: true, stdio: 'ignore' }); child.once('error', reject); child.once('spawn', () => { child.unref(); resolve(); }); });
    return true;
  });
  let quitting = false;
  win.on('close', event => {
    if (quitting || ![...sessions.values()].some(s => !s.exited)) return;
    event.preventDefault();
    dialog.showMessageBox(win, { type: 'question', message: 'Close GPT Manager and stop its running terminals?', buttons: ['Keep open', 'Stop and quit'], defaultId: 0, cancelId: 0 }).then(answer => { if (answer.response === 1) { quitting = true; app.quit(); } });
  });
  app.on('before-quit', () => { if (quitting || ![...sessions.values()].some(s => !s.exited)) for (const s of sessions.values()) s.kill(); });
  app.on('quit', () => { for (const s of sessions.values()) s.kill(); });
  return { isRunning: id => [...sessions.values()].some(s => s.id === id && !s.exited) };
}
module.exports = { installTerminals, resumeCommand };
