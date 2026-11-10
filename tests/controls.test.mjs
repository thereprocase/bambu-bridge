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

test('Skip objects follows Orca: shown with fun bit 49, usable while RUNNING/PAUSE', () => {
  openSheet();
  const skipBtn = () => find(body(), (n) => n.tagName === 'BUTTON' && /^Skip objects/.test(n.textContent))[0];
  assert.equal(skipBtn().hidden, true, 'hidden until the printer reports support');
  telemetry({ job: { part_skip_supported: true, skipped_objects: [] } });
  assert.equal(skipBtn().hidden, false);
  assert.equal(skipBtn().disabled, false);
  telemetry({ job: { skipped_objects: [63, 74] } });
  assert.equal(skipBtn().textContent, 'Skip objects (2)');
  telemetry({ _raw: { gcode_state: 'PREPARE' } });
  assert.equal(skipBtn().disabled, true);
  telemetry({ _raw: { gcode_state: 'PAUSE' }, phase: 'paused' });
  assert.equal(skipBtn().disabled, false);
});

test('maintenance Z presets stay left of step controls and send bounded absolute targets', async () => {
  store.applySnapshot(PID, snapshot({ gcode_state: 'FINISH', home_flag: 7 }, { phase: 'completed' }));
  openSheet({ hold: true });
  const targets = [['Top', 0], ['Upper', 83], ['Lower', 167], ['Bottom', 250]];
  const row = buttons(body(), 'Top')[0].parentNode;
  assert.equal(row.style.display, 'grid');
  assert.equal(row.style.gridTemplateColumns, 'repeat(2, minmax(0, 1fr))');
  assert.equal(row.parentNode.style.gridTemplateColumns, 'minmax(0, 1fr) 96px');
  for (const [label, target] of targets) {
    const button = buttons(body(), label)[0];
    assert.equal(button.parentNode, row);
    assert.equal(button.disabled, false);
    const request = button.fire("click");
    await tick();
    assert.deepEqual(posts.at(-1), {
      path: `/printers/${PID}/move/absolute-z`, body: { position_mm: target, feed_mm_min: 600 },
    });
    assert.ok(targets.every(([name]) => buttons(body(), name)[0].disabled));
    pending.shift()({ ok: true, status: 200, data: { confirmation: 'pending' } });
    await request;
    await tick();
  }
});

test('maintenance positions stay disabled until Z is homed and when telemetry is stale', () => {
  store.applySnapshot(PID, snapshot({ gcode_state: 'FINISH', home_flag: 3 }, { phase: 'completed' }));
  openSheet();
  assert.ok(buttons(body(), 'Bottom')[0].disabled);
  telemetry({ _raw: { home_flag: 7 } });
  assert.equal(buttons(body(), 'Bottom')[0].disabled, false);
  store.applyDelta(PID, { session: { last_telemetry_at: new Date(Date.now() - 60000).toISOString() } });
  assert.ok(buttons(body(), 'Bottom')[0].disabled);
  assert.equal(posts.length, 0);
});


const ariaButton = (label) => find(body(), (n) => n.tagName === 'BUTTON' && n.attrs['aria-label'] === label)[0];

test('maintenance step references come from server response, with bounded Z direction and dropdown', async () => {
  store.applySnapshot(PID, snapshot({ gcode_state: 'FINISH', home_flag: 7 }, { phase: 'completed' }));
  openSheet({ hold: true });
  assert.ok(buttons(body(), '↑ Raise')[0].disabled);
  assert.match(text(), /Select an absolute Z preset/);
  const request = buttons(body(), 'Bottom')[0].fire('click');
  await tick();
  pending.shift()({ ok: true, status: 200, data: { maintenance: { last_requested: { X: null, Y: null, Z: 250 }, position_verified: false } } });
  await request;
  assert.equal(buttons(body(), '↑ Raise')[0].disabled, false);
  assert.ok(buttons(body(), '↓ Lower')[0].disabled, 'no requested movement past bottom');
  const select = find(body(), (n) => n.tagName === 'SELECT' && n.attrs['aria-label'] === 'Maintenance Z step')[0];
  assert.deepEqual(select.children.map(n => n.attrs.value), ['1', '10', '50']);
  select.value = '50';
  await select.fire('change');
  const step = buttons(body(), '↑ Raise')[0].fire('click');
  await tick();
  assert.deepEqual(posts.at(-1), { path: `/printers/${PID}/move/maintenance-step`, body: { axis: 'Z', direction: -1, step_mm: 50 } });
  pending.shift()({ ok: true, status: 200, data: { maintenance: { last_requested: { X: null, Y: null, Z: 200 }, position_verified: false } } });
  await step;
  assert.match(text(), /Last Z command: 200 mm. Physical position is unverified/);
});

test('nine XY presets are nested; eight finite direction buttons use requested reference', async () => {
  store.applySnapshot(PID, snapshot({ gcode_state: 'FINISH', home_flag: 7 }, { phase: 'completed' }));
  openSheet({ hold: true });
  const points = [['Back left',0,256],['Back right',256,256],['Halfway back left',64,192],['Halfway back right',192,192],['Center',128,128],['Halfway front left',64,64],['Halfway front right',192,64],['Front left',0,0],['Front right',256,0]];
  const pointButtons = points.map(([name,x,y])=>ariaButton(`${name}: X${x} Y${y} mm`));
  assert.equal(pointButtons[0].parentNode.style.gridTemplateColumns, 'repeat(5, minmax(0, 1fr))');
  assert.deepEqual(pointButtons.map(n=>[n.style.gridRow,n.style.gridColumn]), [['1','1'],['1','5'],['2','2'],['2','4'],['3','3'],['4','2'],['4','4'],['5','1'],['5','5']]);
  assert.ok(ariaButton('Step back by selected XY step').disabled);
  const center = pointButtons[4].fire('click');
  await tick();
  assert.deepEqual(posts.at(-1), { path: `/printers/${PID}/move/absolute-xy`, body: { x_mm:128, y_mm:128, feed_mm_min:3000 } });
  pending.shift()({ok:true,status:200,data:{maintenance:{last_requested:{X:128,Y:128,Z:null},position_verified:false}}});
  await center;
  const directions = [['back left',-1,1],['back',0,1],['back right',1,1],['left',-1,0],['right',1,0],['front left',-1,-1],['front',0,-1],['front right',1,-1]];
  for (const [name,dx,dy] of directions) {
    const btn=ariaButton(`Step ${name} by selected XY step`);
    assert.equal(btn.disabled,false);
    const click=btn.fire('click'); await tick();
    assert.deepEqual(posts.at(-1),{path:`/printers/${PID}/move/maintenance-xy-step`,body:{direction_x:dx,direction_y:dy,step_mm:10}});
    assert.equal((btn.listeners.pointerdown || []).length, 0, 'no unbounded hold-repeat');
    pending.shift()({ok:true,status:200,data:{maintenance:{last_requested:{X:128+dx*10,Y:128+dy*10,Z:null},position_verified:false}}});
    await click;
  }
  telemetry({maintenance:{last_requested:{X:null,Y:null,Z:null},reason:'connection_lost'}});
  assert.ok(ariaButton('Step back by selected XY step').disabled);
  assert.match(text(), /Select an absolute XY target/);
});
