const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync('src/bambu_bridge/static/viewer.html', 'utf8');
function declaration(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
function fixture() {
  const frames = new Map(), timers = new Map(); let serial = 0;
  const geometry = { fixture: 'retained model' };
  const context = {
    STATE: { mesh: geometry, pollTimer: null, pollController: null, needsDraw: false },
    document: { hidden: false }, performance: { now: () => 1000 },
    requestAnimationFrame: fn => { frames.set(++serial, fn); return serial; },
    cancelAnimationFrame: id => frames.delete(id),
    setInterval: fn => { timers.set(++serial, fn); return serial; },
    clearInterval: id => timers.delete(id),
    POLL_MS: 3000, polls: 0,
    pollState: () => { context.polls++; },
    loseTracking: () => { context.STATE.freshUntil = 0; },
    render: () => {},
  };
  vm.createContext(context);
  const start = source.indexOf('let hostActive=true');
  vm.runInContext(source.slice(start, source.indexOf('/* ── 6. Networking', start)) + '\n' +
    declaration('startPolling') + '\n' + declaration('stopPolling') +
    '\nthis.active=viewerActive; this.activate=setViewerActive;', context);
  return { context, frames, timers, geometry };
}
test('hide aborts telemetry and rendering; repeated resume creates only one of each', () => {
  const { context: c, frames, timers, geometry } = fixture();
  c.activate(true);
  assert.equal(frames.size, 1); assert.equal(timers.size, 1); assert.equal(c.polls, 1);
  let aborted = 0;
  c.STATE.pollController = { abort: () => aborted++ };
  c.activate(false);
  assert.equal(c.active(), false); assert.equal(aborted, 1);
  assert.equal(frames.size, 0); assert.equal(timers.size, 0);
  c.activate(true); c.activate(true);
  assert.equal(frames.size, 1); assert.equal(timers.size, 1); assert.equal(c.polls, 2);
  assert.equal(c.STATE.mesh, geometry);
  assert.equal(c.STATE.freshUntil, 0); // fresh telemetry required before live motion
});
test('document visibility cannot override a hidden native host', () => {
  const { context: c, frames, timers } = fixture();
  c.activate(false); c.document.hidden = false;
  vm.runInContext('setViewerActive(hostActive)', c);
  assert.equal(frames.size, 0); assert.equal(timers.size, 0);
  c.document.hidden = true; c.activate(true);
  assert.equal(frames.size, 0); assert.equal(timers.size, 0);
  c.document.hidden = false; vm.runInContext('setViewerActive(hostActive)', c);
  assert.equal(frames.size, 1); assert.equal(timers.size, 1);
});
