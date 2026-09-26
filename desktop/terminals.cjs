const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { randomUUID } = require('node:crypto');

function executable(name, env = process.env) {
  for (const folder of (env.PATH || '').split(path.delimiter)) {
    if (!folder || !path.isAbsolute(folder)) continue;
    const file = path.join(folder, name);
    try { fs.accessSync(file, fs.constants.X_OK); if (fs.statSync(file).isFile()) return file; } catch {}
  }
  throw new Error(`${name} is not installed or is not on PATH. Install the provider CLI and reopen GPT Manager from your shell.`);
}
function resumeCommand(context) {
  if (context.origin !== 'local') throw new Error('Restore imported context files into a provider store, then refresh and resume the local copy.');
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}$/.test(context.sessionId)) throw new Error('This context has no valid resume ID.');
  const commands = { codex: ['codex', 'resume'], claude: ['claude', '--resume'], gemini: ['gemini', '--resume'], antigravity: ['agy', '--conversation'] };
  const command = commands[context.provider];
  if (!command) throw new Error('Unsupported provider');
  if (context.provider === 'antigravity' && !context.root.endsWith('antigravity-cli')) throw new Error('This Antigravity store is not an agy CLI store. IDE-only contexts cannot be resumed through agy.');
  const env = { ...process.env };
  if (context.provider === 'codex') env.CODEX_HOME = context.root;
  if (context.provider === 'claude') env.CLAUDE_CONFIG_DIR = path.dirname(context.root);
  // Provider CLIs must keep their own interactive approval behavior.
  return { command: executable(command[0], env), args: [...command.slice(1), context.sessionId], env };
}
function installTerminals({ app, win, register, rpc, dialog, selectDirectory }) {
  const sessions = new Map(), folders = new Map();
  async function config(id) {
    const context = (await rpc('library')).contexts.find(c => c.id === id);
    if (!context) throw new Error('Context not available');
    const command = resumeCommand(context);
    let cwd = context.project || folders.get(id);
    if (!cwd || !path.isAbsolute(cwd) || !fs.existsSync(cwd) || !fs.statSync(cwd).isDirectory()) {
      cwd = await selectDirectory('Choose the project folder for this conversation');
      if (!cwd) return null;
      folders.set(id, cwd);
    }
    if (context.provider === 'codex') command.args.push('--cd', cwd);
    return { ...command, cwd };
  }
  function emit(session, channel, value) {
    if (!session.attached) { session.buffer.push([channel, value]); if (session.buffer.length > 256) session.buffer.shift(); }
    else if (!win.isDestroyed()) win.webContents.send(channel, { token: session.token, ...value });
  }
  register('terminalStart', async id => {
    if (process.platform === 'win32') throw new Error('The embedded PTY currently supports Linux and macOS. Windows ConPTY support is not implemented yet.');
    for (const s of sessions.values()) if (s.id === id && !s.exited) throw new Error('This conversation is already open in a terminal.');
    if (sessions.size >= 8) throw new Error('Close a terminal tab before opening another (limit: 8).');
    const cfg = await config(id); if (!cfg) return null;
    const token = randomUUID(), child = spawn(process.env.GPT_MANAGER_PYTHON || 'python3',
      [path.join(__dirname, '../backend/terminal.py'), cfg.cwd, cfg.command, ...cfg.args],
      { env: cfg.env, stdio: ['pipe', 'pipe', 'pipe'] });
    const session = { id, token, child, buffer: [], attached: false, exited: false }; sessions.set(token, session);
    child.stdin.on('error', () => {});
    child.on('error', e => { emit(session, 'terminalData', { data: Buffer.from(e.message).toString('base64') }); });
    child.stderr.on('data', chunk => emit(session, 'terminalData', { data: chunk.toString('base64') }));
    readline.createInterface({ input: child.stdout }).on('line', line => {
      try { const value = JSON.parse(line); if (value.data) emit(session, 'terminalData', { data: value.data }); if (value.exit !== undefined) session.exitCode = value.exit; } catch {}
    });
    child.on('exit', code => { session.exited = true; emit(session, 'terminalExit', { code: session.exitCode ?? code }); });
    return { token, cwd: cfg.cwd };
  });
  register('terminalAttach', token => { const s = sessions.get(token); if (!s) return; s.attached = true; for (const [channel, value] of s.buffer) emit(s, channel, value); s.buffer = []; });
  register('terminalInput', ({ token, data }) => { const s = sessions.get(token); if (!s || s.exited || typeof data !== 'string' || data.length > 100000) return; s.child.stdin.write(JSON.stringify({ type: 'input', data }) + '\n'); });
  register('terminalResize', ({ token, cols, rows }) => { const s = sessions.get(token); if (!s || s.exited || !Number.isInteger(cols) || !Number.isInteger(rows)) return; s.child.stdin.write(JSON.stringify({ type: 'resize', cols, rows }) + '\n'); });
  register('terminalClose', async token => {
    const s = sessions.get(token); if (!s) return true;
    if (!s.exited) { const answer = await dialog.showMessageBox(win, { type: 'question', message: 'Stop this running terminal session?', detail: 'The provider process will receive a hangup signal. Saved conversation history remains in its provider store.', buttons: ['Keep open', 'Stop session'], defaultId: 0, cancelId: 0 }); if (answer.response !== 1) return false; }
    s.child.kill(); sessions.delete(token); return true;
  });
  register('resumeExternal', async id => {
    const cfg = await config(id); if (!cfg) return null;
    if (process.platform !== 'linux') throw new Error('External terminal launch currently supports Linux. Use Chat here on macOS.');
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
  app.on('before-quit', () => { if (quitting || ![...sessions.values()].some(s => !s.exited)) for (const s of sessions.values()) s.child.kill(); });
  app.on('quit', () => { for (const s of sessions.values()) s.child.kill(); });
  return { isRunning: id => [...sessions.values()].some(s => s.id === id && !s.exited) };
}
module.exports = { installTerminals, resumeCommand };
