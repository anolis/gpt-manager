const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../ui/app.js'), 'utf8');
const rangeSource = source.slice(source.indexOf('function catchupRange('), source.indexOf('function renderCatchUp('));
const range = vm.runInNewContext(rangeSource + '\ncatchupRange', { Date, Intl });
test('catch-up uses calendar days through daylight-saving boundaries', () => {
  const previous = process.env.TZ;
  process.env.TZ = 'America/Chicago';
  try {
    const spring = range('yesterday', '', '', new Date('2026-03-09T12:00:00-05:00'));
    assert.equal(spring.start, '2026-03-08T06:00:00.000Z');
    assert.equal(spring.end, '2026-03-09T05:00:00.000Z');
    const fall = range('yesterday', '', '', new Date('2026-11-02T12:00:00-06:00'));
    assert.equal(fall.start, '2026-11-01T05:00:00.000Z');
    assert.equal(fall.end, '2026-11-02T06:00:00.000Z');
    const week = range('week', '', '', new Date('2026-09-26T12:00:00-05:00'));
    assert.equal(week.start, '2026-09-14T05:00:00.000Z');
    assert.equal(week.end, '2026-09-21T05:00:00.000Z');
    const custom = range('custom', '2026-09-20', '2026-09-22');
    assert.equal(custom.start, '2026-09-20T05:00:00.000Z');
    assert.equal(custom.end, '2026-09-23T05:00:00.000Z');
  } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
});
