const { contextBridge, ipcRenderer } = require('electron');
const call = name => (...args) => ipcRenderer.invoke(name, ...args);
contextBridge.exposeInMainWorld('manager', {
  usageStatus: call('usageStatus'), usageRefresh: call('usageRefresh'), usageConfigure: call('usageConfigure'),
  setupUninstall: call('setupUninstall'), setupAuthRefresh: call('setupAuthRefresh'),
  setupPrefer: call('setupPrefer'),
  setupStatus: call('setupStatus'), setupInstall: call('setupInstall'), setupCancel: call('setupCancel'), providerSignIn: call('providerSignIn'),
  catchupScheduleStatus: call('catchupScheduleStatus'), catchupScheduleConfigure: call('catchupScheduleConfigure'),
  catchupStatus: call('catchupStatus'), catchupPrepare: call('catchupPrepare'), catchupGenerate: call('catchupGenerate'), catchupCancel: call('catchupCancel'), catchupHistory: call('catchupHistory'), catchupGet: call('catchupGet'), catchupDelete: call('catchupDelete'),
  sshAdd: call('sshAdd'), sshRemove: call('sshRemove'), sshRefresh: call('sshRefresh'), resumeHere: call('resumeHere'),
  sshAliases: call('sshAliases'), localNetworks: call('localNetworks'), scanNetwork: call('scanNetwork'), handoffRelease: call('handoffRelease'),
  onHandoffProgress: listener => { const handler = (_event, message) => listener(message); ipcRenderer.on('handoffProgress', handler); return () => ipcRenderer.removeListener('handoffProgress', handler); },
  terminalStart: call('terminalStart'), terminalAttach: call('terminalAttach'), terminalInput: call('terminalInput'), terminalResize: call('terminalResize'), terminalClose: call('terminalClose'), resumeExternal: call('resumeExternal'),
  onTerminalData: listener => { const handler = (_event, data) => listener(data); ipcRenderer.on('terminalData', handler); return () => ipcRenderer.removeListener('terminalData', handler); },
  onTerminalExit: listener => { const handler = (_event, data) => listener(data); ipcRenderer.on('terminalExit', handler); return () => ipcRenderer.removeListener('terminalExit', handler); },
  cloudStatus: call('cloudStatus'), cloudConnect: call('cloudConnect'), cloudAnswer: call('cloudAnswer'), cloudCancel: call('cloudCancel'), cloudConfigure: call('cloudConfigure'), cloudSync: call('cloudSync'), cloudTick: call('cloudTick'), cloudFolder: call('cloudFolder'), cloudDisconnect: call('cloudDisconnect'), cloudGoogleConfig: call('cloudGoogleConfig'),
  changeProject: call('changeProject'), moveContextFiles: call('moveContextFiles'),
  scan: call('scan'), library: call('library'), detail: call('detail'), annotate: call('annotate'), files: call('files'),
  addRoot: call('addRoot'), exportBundle: call('exportBundle'), chooseImport: call('chooseImport'),
  importBundle: call('importBundle'), restoreContext: call('restoreContext'), revealContext: call('revealContext'), copyText: call('copyText')
});
