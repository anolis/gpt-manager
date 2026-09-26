const path = require('node:path');
function backendCommand(mode, app) {
  if (app.isPackaged) return { command: path.join(process.resourcesPath, 'backend', process.platform === 'win32' ? 'gpt-manager-backend.exe' : 'gpt-manager-backend'), args: [mode] };
  const script = { rpc: 'rpc.py', lock: 'session_lock.py', terminal: 'terminal.py' }[mode];
  return { command: process.env.GPT_MANAGER_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), args: [path.join(__dirname, '../backend', script)] };
}
module.exports = { backendCommand };
