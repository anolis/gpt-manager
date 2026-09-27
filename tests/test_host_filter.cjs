const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../ui/app.js'), 'utf8');
const filterSource = source.slice(source.indexOf('function filtered('), source.indexOf('function providerLabel('));

function fixture() {
  const contexts = [
    { id: 'local', origin: 'local', provider: 'codex' },
    { id: 'imported', origin: 'imported', provider: 'claude', endpointId: 'host-a' },
    { id: 'remote-a', origin: 'remote', endpointId: 'host-a', provider: 'codex', starred: true },
    { id: 'remote-b', origin: 'remote', endpointId: 'host-b', provider: 'claude', offline: true },
    { id: 'archived', origin: 'remote', endpointId: 'host-a', provider: 'codex', archived: true },
  ].map(c => ({ title: 'Project recap', machine: 'same-machine-name', updated: '2026-09-27', ...c }));
  const state = { library: { contexts }, host: 'all', provider: 'all', view: 'all', query: '' };
  const filtered = vm.runInNewContext(filterSource + '\nfiltered', { state, $: () => ({ value: 'recent' }) });
  return { state, ids: () => Array.from(filtered(), c => c.id) };
}

test('host scope separates local/imported contexts and SSH endpoints, including offline snapshots', () => {
  const { state, ids } = fixture();
  assert.deepEqual(ids(), ['local', 'imported', 'remote-a', 'remote-b']);
  state.host = 'local'; assert.deepEqual(ids(), ['local', 'imported']);
  state.host = 'ssh'; assert.deepEqual(ids(), ['remote-a', 'remote-b']);
  state.host = 'host-a'; assert.deepEqual(ids(), ['remote-a']);
  state.host = 'host-b'; assert.deepEqual(ids(), ['remote-b']);
});

test('host filtering composes with provider, search, starred and archived views', () => {
  const { state, ids } = fixture();
  state.host = 'host-a'; state.provider = 'claude'; assert.deepEqual(ids(), []);
  state.provider = 'codex'; state.query = 'RECAP'; state.view = 'starred'; assert.deepEqual(ids(), ['remote-a']);
  state.query = 'no match'; assert.deepEqual(ids(), []);
  state.query = ''; state.view = 'archived'; assert.deepEqual(ids(), ['archived']);
  state.view = 'imported'; assert.deepEqual(ids(), []);
});
