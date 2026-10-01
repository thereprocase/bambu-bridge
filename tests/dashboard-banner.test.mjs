// The protocol-mismatch banner must survive the dashboard's re-renders.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { find, tick } from './helpers/fakedom.mjs';

globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}), headers: { get() { return null; } } });
const appDir = new URL('../src/bambu_bridge/static/app/', import.meta.url);
const store = await import(new URL('store.js', appDir));
const dashboard = await import(new URL('dashboard.js', appDir));

test('protocol-mismatch banner persists across store updates (render clears the banner host)', async () => {
  const pid = 'P1';
  let handlers;
  store.applySnapshot(pid, {
    printer_id: pid, phase: 'printing', headline: { title: 'Printing', indicator: 'progress' },
    session: { connected: true, last_telemetry_at: new Date().toISOString() },
    _raw: { gcode_state: 'RUNNING', layer_num: 5 }, temps: {}, cooling: {},
    ams: { present: false, slots: [] }, hms: [],
  });
  const app = {
    store, currentPrinterId: () => pid, navigate() {}, requireKeyScreen() {}, getKey: () => null,
    ws: { connectStatus: (_id, h) => { handlers = h; return { close() {} }; } },
    api: { api: async () => ({ ok: false }), getJson: async () => ({ ok: false }), postJson: async () => ({ ok: false }) },
  };
  const root = document.createElement('div');
  document.body.appendChild(root);
  const unmount = dashboard.mount(root, app);
  const shown = () => find(root, (n) => n._text.includes('needs an update')).length > 0;
  handlers.onProtocolMismatch();
  assert.ok(shown());
  store.applyDelta(pid, { _raw: { layer_num: 6 } });
  store.applyDelta(pid, { _raw: { layer_num: 7 } });
  assert.ok(shown(), 'still shown after deltas');
  unmount();
  await tick();
});
