const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resumeCommand } = require('../desktop/terminals.cjs');

test('resume adapters keep IDs as literal argv and preserve provider stores', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpt-manager-cli-'));
  const previous = process.env.PATH;
  try {
    for (const name of ['codex', 'claude', 'gemini', 'agy']) fs.writeFileSync(path.join(dir, name), '#!/bin/sh\nexit 0\n', { mode: 0o700 });
    process.env.PATH = dir;
    for (const [provider, expected] of Object.entries({ codex: ['resume', 'session-123'], claude: ['--resume', 'session-123'], gemini: ['--resume', 'session-123'], antigravity: ['--conversation', 'session-123'] })) {
      const root = provider === 'antigravity' ? '/home/test/.gemini/antigravity-cli' : '/home/test/store/projects';
      const result = resumeCommand({ origin: 'local', provider, sessionId: 'session-123', root });
      assert.deepEqual(result.args, expected);
      assert.ok(path.isAbsolute(result.command));
      if (provider === 'codex') assert.equal(result.env.CODEX_HOME, root);
      if (provider === 'claude') assert.equal(result.env.CLAUDE_CONFIG_DIR, path.dirname(root));
      assert.ok(!result.args.some(x => x.includes('skip') || x.includes('yolo')));
    }
    assert.throws(() => resumeCommand({ origin: 'imported', sessionId: 'session-123' }), /Restore/);
    for (const id of ['--help', 'id; touch /tmp/x', '$(whoami)', 'id\ncommand', '', '../session']) assert.throws(() => resumeCommand({ origin: 'local', provider: 'codex', sessionId: id }), /valid resume ID/);
  } finally {
    process.env.PATH = previous;
    fs.rmSync(dir, { recursive: true });
  }
});
