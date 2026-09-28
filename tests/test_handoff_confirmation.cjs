const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../desktop/main.cjs'), 'utf8');
const handlerSource = source.slice(source.indexOf("  register('resumeHere'"), source.indexOf("  register('addRoot'"));

async function resume(choice, conflict = true, mode = 1) {
  const calls = [], dialogs = [];
  const selections = [];
  let handler;
  const snapshot = 'a'.repeat(64);
  vm.runInNewContext(handlerSource, {
    register: (_name, fn) => { handler = fn; }, win: {},
    terminalController: { isRunning: () => false },
    selectDirectory: async title => { selections.push(title); return '/project'; },
    dialog: { showMessageBox: async (_win, options) => {
      dialogs.push(options);
      return { response: dialogs.length === 1 ? mode : choice };
    } },
    rpc: async (name, params) => {
      calls.push({ name, params });
      if (name === 'library') return { contexts: [{ id: 'remote', origin: 'remote', machine: 'thinkpad' }] };
      if (name === 'git_check') return {};
      if (conflict && !params.discard_local_changes) return { conflict: 'retained-changed', sourceMachine: 'thinkpad', localPath: '/session.jsonl', expectedLocal: snapshot };
      return { id: 'restored' };
    },
  });
  return { result: await handler('remote'), calls: calls.filter(c => c.name === 'teleport'), dialogs, snapshot, selections };
}

test('canceling a changed local conversation never submits discard permission', async () => {
  const { result, calls, dialogs } = await resume(0);
  assert.equal(result, null);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].params.discard_local_changes, undefined);
  assert.equal(dialogs[1].defaultId, 0);
  assert.equal(dialogs[1].buttons[1], 'Trash local changes and continue');
});

test('explicit confirmation submits the opaque fingerprint with discard permission', async () => {
  const { result, calls, snapshot } = await resume(1);
  assert.equal(result.id, 'restored');
  assert.equal(calls.length, 2);
  assert.equal(calls[1].params.discard_local_changes, true);
  assert.equal(calls[1].params.expected_local, snapshot);
});

test('an unchanged return needs no discard confirmation', async () => {
  const { result, calls, dialogs } = await resume(0, false);
  assert.equal(result.id, 'restored');
  assert.equal(calls.length, 1);
  assert.equal(dialogs.length, 1);
});

test('workspace copy asks for a parent and passes it to the backend', async () => {
  const { result, calls, selections } = await resume(0, false, 2);
  assert.equal(result.id, 'restored');
  assert.match(selections[0], /parent folder/);
  assert.equal(calls[0].params.folder, '/project');
  assert.equal(calls[0].params.copy_workspace, true);
  assert.equal(calls[0].params.workspace_layout, 'folder');
});

test('copy contents asks for an empty folder and requests contents layout', async () => {
  const { result, calls, selections } = await resume(0, false, 3);
  assert.equal(result.id, 'restored');
  assert.match(selections[0], /empty folder/);
  assert.equal(calls[0].params.folder, '/project');
  assert.equal(calls[0].params.copy_workspace, true);
  assert.equal(calls[0].params.workspace_layout, 'contents');
});
