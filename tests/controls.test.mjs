// Controls sheet: built once, patched in place (OrcaSlicer device-panel rules).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { find, buttons, resetBody, tick } from './helpers/fakedom.mjs';

const appDir = new URL('../src/bambu_bridge/static/app/', import.meta.url);
const store = await import(new URL('store.js', appDir));
const controls = await import(new URL('controls.js', appDir));

const PID = 'P1';
let posts;
let pending;

function snapshot(raw = {}, extra = {}) {
  return {
    printer_id: PID,
    phase: 'printing',
    session: { connected: true, last_telemetry_at: new Date().toISOString() },
    _raw: { gcode_state: 'RUNNING', layer_num: 5, spd_lvl: 2, ...raw },
    temps: { nozzle: { current_c: 219, target_c: 220 }, bed: { current_c: 60, target_c: 60 } },
    cooling: { part_fan: { percent: 50 }, aux_fan: { percent: 0 }, chamber_fan: { percent: 0 } },
    ams: {
      present: true, engaged_slot: 1,
      slots: [
        { physical_slot: 1, type: 'PLA', color: 'FFFFFFFF', state: 'loaded', remaining_pct: 80 },
        { physical_slot: 2, type: 'PETG', color: '000000FF', state: 'loaded', remaining_pct: 50 },
        { physical_slot: 3, type: null, color: null, state: 'empty', remaining_pct: null },
      ],
    },
    lights: { chamber_on: true },
    hms: [],
    ...extra,
  };
}

/** A delta that only refreshes telemetry (what arrives every 1-4 s mid-print). */
function telemetry(patch = {}) {
  store.applyDelta(PID, {
    session: { last_telemetry_at: new Date().toISOString() },
    _raw: { layer_num: Math.floor(Math.random() * 1000) },
    ...patch,
  });
}

function openSheet({ hold = false } = {}) {
  posts = [];
  pending = [];
  const app = {
    store, currentPrinterId: () => PID, navigate() {}, requireKeyScreen() {},
    api: {
      postJson: (path, body) => {
        posts.push({ path, body });
        if (!hold) return Promise.resolve({ ok: true, status: 200, data: {} });
        return new Promise((resolve) => pending.push(resolve));
      },
    },
  };
  return controls.open(app);
}

const body = () => document.body;
const input = (label) => find(body(), (n) => n.tagName === 'INPUT' && n.attrs['aria-label'] === label)[0];
const text = () => body().textContent;

beforeEach(() => {
  resetBody();
  store.applySnapshot(PID, snapshot());
});

test('a refused command stays visible across telemetry deltas', async () => {
  openSheet({ hold: true });
  buttons(body(), 'Pause')[0].fire('click');
  telemetry();                                    // delta lands mid-request
  pending[0]({ ok: false, status: 409, error: 'printer_offline', message: 'printer is not connected' });
  await tick();
  telemetry();
  telemetry();
  const card = find(body(), (n) => n.attrs.role === 'alert')[0];
  assert.ok(card && card.isConnected, 'error card attached');
  assert.match(card.textContent, /printer is not connected/);
});

test('typed G-code, its focus and the input node survive deltas', () => {
  openSheet();
  const before = input('Raw G-code line');
  before.value = 'M400';
  before.focus();
  telemetry();
  telemetry();
  const after = input('Raw G-code line');
  assert.equal(after, before);
  assert.equal(after.value, 'M400');
  assert.ok(document.activeElement.isConnected);
});

test('heater target follows telemetry unless focused or just set', async () => {
  openSheet();
  const field = input('Nozzle target');
  assert.equal(field.value, '220');
  telemetry({ temps: { nozzle: { target_c: 230 } } });
  assert.equal(field.value, '230', 'unfocused field tracks the target');
  assert.match(text(), /→ 230 °C/);

  field.focus();
  field.value = '245';
  telemetry({ temps: { nozzle: { target_c: 231 } } });
  assert.equal(field.value, '245', 'focused field is left alone');
  field.blur();

  field.value = '999';
  await buttons(body(), 'Set')[0].fire('click');
  assert.deepEqual(posts.at(-1), { path: '/printers/P1/temperature', body: { nozzle: 300 } },
    'out-of-range clamps to the 300 °C ceiling');
  for (let i = 0; i < 5; i++) telemetry({ temps: { nozzle: { target_c: 231 } } });
  assert.equal(field.value, '300', 'held for five updates after a Set');
  telemetry({ temps: { nozzle: { target_c: 231 } } });
  assert.equal(field.value, '231');
});

test('reported nozzle_temp_range replaces the 300 °C ceiling', async () => {
  store.applySnapshot(PID, snapshot({ nozzle_temp_range: [20, 250] }));
  openSheet();
  const field = input('Nozzle target');
  field.value = '280';
  await buttons(body(), 'Set')[0].fire('click');
  assert.deepEqual(posts.at(-1).body, { nozzle: 250 });
});

test('a focused fan slider is not snapped back by telemetry', () => {
  openSheet();
  const slider = input('Part fan');
  assert.equal(slider.value, '50');
  slider.focus();
  slider.value = '80';
  telemetry({ cooling: { part_fan: { percent: 50 } } });
  assert.equal(slider.value, '80');
  slider.blur();
  telemetry({ cooling: { part_fan: { percent: 40 } } });
  assert.equal(slider.value, '40');
});

test('speed selection is the reported spd_lvl', () => {
  openSheet();
  assert.match(buttons(body(), 'Standard')[0].className, /btn--primary/);
  telemetry({ _raw: { spd_lvl: 3 } });
  assert.match(buttons(body(), 'Sport')[0].className, /btn--primary/);
  assert.doesNotMatch(buttons(body(), 'Standard')[0].className, /btn--primary/);
});

test('printing: motion and AMS loads are refused, as in OrcaSlicer', () => {
  openSheet();
  assert.equal(buttons(body(), 'X+').length, 0, 'no jog pad while printing');
  assert.match(text(), /The printer is busy with another print job\./);
  assert.ok(buttons(body(), 'Change to other tray')[0].disabled);
  assert.ok(buttons(body(), 'Unload filament')[0].disabled);
  assert.equal(buttons(body(), 'Pause AMS').length, 0);
  assert.equal(buttons(body(), 'Reset AMS').length, 0);
  assert.equal(buttons(body(), 'Resume AMS').length, 1);
});

test('paused: jog pad returns with 1/10 mm steps and Orca feed rates', async () => {
  openSheet();
  telemetry({ phase: 'paused', _raw: { gcode_state: 'PAUSE' } });
  assert.deepEqual(controls.JOG_STEPS, [1, 10]);
  assert.ok(buttons(body(), '1 mm')[0] && buttons(body(), '10 mm')[0]);
  assert.equal(buttons(body(), '50 mm').length, 0);
  assert.equal(buttons(body(), '100 mm').length, 0);
  await buttons(body(), 'X+')[0].fire('click');
  assert.deepEqual(posts.at(-1), { path: '/printers/P1/move', body: { axis: 'X', distance_mm: 1, feed_mm_min: 3000 } });
  await buttons(body(), '10 mm')[0].fire('click');
  await buttons(body(), 'Z−')[0].fire('click');
  assert.deepEqual(posts.at(-1).body, { axis: 'Z', distance_mm: -10, feed_mm_min: 900 });
  // Paused: AMS slots stay locked; unload only for the external spool.
  assert.ok(buttons(body(), 'Change to other tray')[0].disabled);
  assert.ok(buttons(body(), 'Unload filament')[0].disabled);
  telemetry({ ams: { engaged_slot: 'external' } });
  assert.equal(buttons(body(), 'Unload filament')[0].disabled, false);
});

test('idle: AMS change is offered; empty and loaded slots are not', () => {
  store.applySnapshot(PID, snapshot({ gcode_state: 'IDLE' }, { phase: 'idle' }));
  openSheet();
  const change = buttons(body(), 'Change to other tray')[0];
  assert.equal(change.disabled, false);
  change.fire('click');
  const pick = (re) => find(body(), (n) => n.tagName === 'BUTTON' && re.test(n.textContent))[0];
  assert.ok(pick(/^Slot 1 .*loaded/).disabled);
  assert.ok(pick(/^Slot 3 .*empty/).disabled);
  assert.equal(pick(/^Slot 2 /).disabled, false);
});

test('disconnected: the whole sheet is disabled', () => {
  openSheet();
  store.setConnected(PID, false);
  assert.match(text(), /Disconnected — controls disabled/);
});
