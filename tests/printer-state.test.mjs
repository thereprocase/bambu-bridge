import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

async function moduleFrom(name) {
  const source = await readFile(new URL(`../src/bambu_bridge/static/app/${name}`, import.meta.url), 'utf8');
  return import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
}

// printer-state.js imports clock-sync.js, so load it by file URL (a data: URL
// cannot resolve relative imports). Node detects the ES module syntax.
const appDir = new URL('../src/bambu_bridge/static/app/', import.meta.url);
const { printerStateLabels, resetTelemetryClock } = await import(new URL('printer-state.js', appDir));
const { recordClockSample, resetClockSync } = await import(new URL('clock-sync.js', appDir));
const { ownerLabel, startLabel, fileLabel } = await moduleFrom('native-state.js');
const now = Date.parse('2026-09-27T12:00:00Z');
function snapshot(state, layer = 0) {
  return { session: { connected: true, last_telemetry_at: new Date(now).toISOString() },
    cert_status: 'trusted', _raw: { gcode_state: state }, job: { layer_num: layer },
    print_error: null, hms: [] };
}

for (const [state, layer, activity] of [
  ['IDLE', 0, 'Idle'], ['PREPARE', 0, 'Preparing'], ['RUNNING', 0, 'Preparing'],
  ['RUNNING', 2, 'Printing'], ['PAUSE', 2, 'Paused'], ['FINISH', 2, 'Finished'],
  ['FAILED', 2, 'Print failed'], ['UNKNOWN', 0, 'Unknown'],
]) test(`${state} layer ${layer} -> ${activity}`, () => {
  assert.equal(printerStateLabels(snapshot(state, layer), true, now).activity, activity);
});

test('offline and stale do not assert power off or current activity', () => {
  const last = snapshot('RUNNING', 3);
  assert.deepEqual(printerStateLabels(last, false, now),
    { power: 'Unknown', connection: 'Bridge unreachable', activity: 'Unknown', issues: [], live: false });
  resetTelemetryClock();
  assert.equal(printerStateLabels(last, true, now).live, true);
  assert.equal(printerStateLabels(last, true, now + 16000).connection, 'Status stale');
  last.session.connected = false;
  last.session.last_failure_phase = 'mqtt_connack';
  assert.equal(printerStateLabels(last, true, now).connection, 'Printer login failed');
});

test('errors and start ownership have separate labels', () => {
  const live = snapshot('RUNNING', 3);
  live.print_error = { text: 'Nozzle error' };
  live.hms = [{ severity: 'warn' }, { severity: 'error', stale: true }];
  assert.deepEqual(printerStateLabels(live, true, now).issues, ['Print error', 'Printer warning']);
  assert.equal(startLabel('queued'), 'Start pending');
  assert.equal(fileLabel('stored'), 'Cached on bridge');
  assert.equal(ownerLabel({ start_owner: null }), 'No bridge start in progress');
  assert.equal(ownerLabel({ start_owner: 'id', start_owner_state: 'unknown' }), 'Start unconfirmed');
});

test('a device clock behind or ahead of the bridge still shows fresh telemetry as live', () => {
  for (const offset of [-3000, -300, 4000, 60000]) {
    resetTelemetryClock();
    const live = snapshot('RUNNING', 2);
    for (let t = 0; t <= 30000; t += 2000) {
      live.session.last_telemetry_at = new Date(now + t).toISOString();
      assert.equal(printerStateLabels(live, true, now + t + offset + 50).live, true, `offset ${offset} t ${t}`);
    }
  }
});

test('clock offset gives exact age for a device clock far off the bridge', () => {
  for (const off of [-4000, -300, 2500, 90000]) {
    resetTelemetryClock(); resetClockSync();
    const bridgeNow = now + 60000;
    const t0 = bridgeNow + off, t1 = bridgeNow + 30, t2 = t1 + 5, t3 = t2 + 30 + off;
    recordClockSample('', t0, t1, t3, t2);
    const s = snapshot('RUNNING', 2);
    for (const [age, freshness, live] of [[1000, 'live', true], [9000, 'aging', true], [16000, 'stale', false]]) {
      s.session.last_telemetry_at = new Date(bridgeNow + 1000 - age).toISOString();
      const out = printerStateLabels(s, true, bridgeNow + 1000 + off);
      assert.equal(out.freshness, freshness, `off ${off} age ${age}`);
      assert.equal(out.live, live);
      assert.ok(Math.abs(out.ageMs - age) <= 35);
    }
  }
});

test('RFC 5905 offset removes bridge hold time', () => {
  resetClockSync();
  const t0 = 1e6, t1 = t0 + 2040, t2 = t1 + 25, t3 = t2 - 2000 + 60;
  assert.deepEqual(recordClockSample('p', t0, t1, t3, t2), { offsetMs: 1990, rttMs: 100, samples: 1 });
});
