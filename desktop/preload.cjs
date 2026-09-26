const { contextBridge, ipcRenderer } = require('electron');
const call = name => (...args) => ipcRenderer.invoke(name, ...args);
contextBridge.exposeInMainWorld('manager', {
  terminalStart: call('terminalStart'), terminalAttach: call('terminalAttach'), terminalInput: call('terminalInput'), terminalResize: call('terminalResize'), terminalClose: call('terminalClose'), resumeExternal: call('resumeExternal'),
  onTerminalData: listener => { const handler = (_event, data) => listener(data); ipcRenderer.on('terminalData', handler); return () => ipcRenderer.removeListener('terminalData', handler); },
  onTerminalExit: listener => { const handler = (_event, data) => listener(data); ipcRenderer.on('terminalExit', handler); return () => ipcRenderer.removeListener('terminalExit', handler); },
  scan: call('scan'), library: call('library'), detail: call('detail'), annotate: call('annotate'), files: call('files'),
  addRoot: call('addRoot'), exportBundle: call('exportBundle'), chooseImport: call('chooseImport'),
  importBundle: call('importBundle'), restoreContext: call('restoreContext'), revealContext: call('revealContext'), copyText: call('copyText')
});
