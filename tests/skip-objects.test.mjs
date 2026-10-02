// Skip Objects sheet: Orca's pick-map hit test and PartSkipDialog flow.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { find, buttons, resetBody, tick } from './helpers/fakedom.mjs';

const appDir = new URL('../src/bambu_bridge/static/app/', import.meta.url);
const store = await import(new URL('store.js', appDir));
const skip = await import(new URL('skip-objects.js', appDir));

// A 6x4 map: object 63 in columns 0-1, object 74 in columns 3-4 of rows 1-2,
// and a stray colour 99 that is not an object of the plate.
const MAP = {
  width: 6,
  height: 4,
  rows: [
    [0, 6],
    [63, 2, 0, 1, 74, 2, 99, 1],
    [63, 2, 0, 1, 74, 2, 0, 1],
    [0, 6],
  ],
};
const IDS = new Set([63, 74]);

test('idAt walks the run-length row', () => {
  assert.equal(skip.idAt(MAP, 0, 1), 63);
  assert.equal(skip.idAt(MAP, 1, 2), 63);
  assert.equal(skip.idAt(MAP, 2, 1), 0);
  assert.equal(skip.idAt(MAP, 3, 1), 74);
  assert.equal(skip.idAt(MAP, 4, 2), 74);
  assert.equal(skip.idAt(MAP, 5, 1), 99);
  assert.equal(skip.idAt(MAP, 0, 0), 0);
  assert.equal(skip.idAt(MAP, -1, 1), 0);
  assert.equal(skip.idAt(MAP, 6, 1), 0);
  assert.equal(skip.idAt(MAP, 0, 4), 0);
});

test('hitTest scales a tap on the shown map to image pixels', () => {
  // Shown at 300x200: each image pixel is 50x50 on screen.
  assert.equal(skip.hitTest(MAP, IDS, 10, 60, 300, 200), 63);
  assert.equal(skip.hitTest(MAP, IDS, 99.9, 149.9, 300, 200), 63);
  assert.equal(skip.hitTest(MAP, IDS, 100, 60, 300, 200), 0);     // gap column
  assert.equal(skip.hitTest(MAP, IDS, 160, 140, 300, 200), 74);
  assert.equal(skip.hitTest(MAP, IDS, 260, 60, 300, 200), 0);     // stray colour 99
  assert.equal(skip.hitTest(MAP, IDS, 10, 10, 300, 200), 0);      // bed
  assert.equal(skip.hitTest(MAP, IDS, 10, 60, 0, 0), 0);
  assert.equal(skip.hitTest(null, IDS, 10, 60, 300, 200), 0);
});

test('applyRefusal uses Orca tooltips', () => {
  const ok = { label_object_enabled: true, available: true };
  const sel = new Map([[63, 'checked'], [74, 'unchecked']]);
  assert.equal(skip.applyRefusal(ok, new Map([[63, 'unchecked']])), 'Nothing selected');
  assert.equal(skip.applyRefusal(ok, sel), '');
  assert.equal(skip.applyRefusal({ ...ok, label_object_enabled: false }, sel),
    'The current print job cannot be skipped');
  const many = new Map(Array.from({ length: 65 }, (_, i) => [i + 1, i ? 'unchecked' : 'checked']));
  assert.equal(skip.applyRefusal(ok, many), 'Over 64 objects in single plate');
  assert.equal(skip.applyRefusal({ ...ok, available: false, reason: 'Control support under review' }, sel),
    'Control support under review');
});

// ── the sheet ────────────────────────────────────────────────────────────────
const PID = 'P1';
let posts;
let gets;
let data;
let reply;
let sheet;

function openSheet() {
  posts = [];
  gets = 0;
  reply = async () => ({ ok: true, status: 200, data: { action: 'skip' } });
  const app = {
    store,
    api: {
      api: async () => { gets += 1; return { ok: true, status: 200, data }; },
      tokenUrl: (path, params) => `${path}?${new URLSearchParams(params)}`,
      postJson: async (path, body) => { posts.push({ path, body }); return reply(body); },
    },
  };
  sheet = skip.open(app, PID);
  return sheet;
}

const IDENTITY = { job: 'multi3', gcode_file: 'multi3.gcode.3mf', plate: 1, digest: 'd'.repeat(64) };

const img = () => find(document.body, (n) => n.tagName === 'IMG')[0];
const box = (label) => find(document.body, (n) => n.tagName === 'INPUT' && n.attrs['aria-label'] === label)[0];
const tap = async (x, y) => {
  const node = img();
  node.clientWidth = 300; node.clientHeight = 200;
  await node.fire('click', { offsetX: x, offsetY: y });
};

beforeEach(() => {
  if (sheet) sheet.close();   // drop the last sheet's store subscription
  sheet = null;
  resetBody();
  data = {
    ...IDENTITY, label_object_enabled: true, available: true, reason: null, map: MAP,
    objects: [
      { id: 63, name: 'cube.stl', skipped: false },
      { id: 74, name: 'bar.stl', skipped: false },
      { id: 85, name: 'frame.stl', skipped: true },
    ],
  };
  store.applySnapshot(PID, { printer_id: PID, job: { skipped_objects: [85] }, _raw: {} });
});

test('tapping the map toggles the object under the finger; skipped stay locked', async () => {
  openSheet();
  await tick();
  const skipBtn = buttons(document.body, 'Skip')[0];
  assert.equal(skipBtn.disabled, true);
  assert.equal(box('frame.stl').disabled, true);
  assert.equal(box('frame.stl').checked, true);
  await tap(10, 60);                     // cube
  assert.equal(box('cube.stl').checked, true);
  assert.match(img().src, /checked=63/);
  assert.match(img().src, /digest=d{64}/);
  assert.equal(skipBtn.disabled, false);
  await tap(260, 60);                    // stray colour: nothing
  await tap(10, 10);                     // bed: nothing
  assert.equal(box('bar.stl').checked, false);
  await tap(10, 60);                     // cube again: unchecked
  assert.equal(box('cube.stl').checked, false);
  assert.equal(skipBtn.disabled, true);
});

test('Skip confirms with Orca wording and posts the checked ids', async () => {
  openSheet();
  await tick();
  await tap(160, 140);                   // bar
  buttons(document.body, 'Skip')[0].fire('click');   // resolves after the confirm
  await tick();
  assert.ok(find(document.body, (n) => n.textContent === 'Skipping 1 objects.').length);
  assert.ok(find(document.body, (n) => n.textContent === 'This action cannot be undone. Continue?').length);
  await buttons(document.body, 'Continue')[0].fire('click');
  await tick();
  assert.deepEqual(posts, [{
    path: '/printers/P1/skip_objects',
    body: { obj_list: [74], action: 'skip', ...IDENTITY },
  }]);
});

test('Select All covers every remaining object and warns the print will stop', async () => {
  openSheet();
  await tick();
  const all = box('Select All');
  all.checked = true;
  await all.fire('change');
  assert.equal(box('cube.stl').checked, true);
  assert.equal(box('bar.stl').checked, true);
  buttons(document.body, 'Skip')[0].fire('click');   // resolves after the confirm
  await tick();
  assert.ok(find(document.body, (n) => n.textContent === 'Skipping all objects.').length);
  assert.ok(find(document.body, (n) => n.textContent === 'The printing job will be stopped. Continue?').length);
  await buttons(document.body, 'Cancel')[0].fire('click');
  await tick();
  assert.deepEqual(posts, []);
});

test('a withheld bridge shows its reason and never posts', async () => {
  data.available = false;
  data.reason = 'Control support under review';
  openSheet();
  await tick();
  await tap(10, 60);
  assert.equal(buttons(document.body, 'Skip')[0].disabled, true);
  assert.ok(find(document.body, (n) => n.textContent === 'Control support under review').length);
});

test('objects the printer skips while the sheet is open lock in place', async () => {
  openSheet();
  await tick();
  await tap(10, 60);                     // cube checked
  store.applyDelta(PID, { job: { skipped_objects: [85, 63] } });
  assert.equal(box('cube.stl').disabled, true);
  assert.equal(box('cube.stl').checked, true);
  assert.match(img().src, /v=63%2C85|v=85%2C63/);
  assert.equal(buttons(document.body, 'Skip')[0].disabled, true);   // nothing left checked
});


test('Skip is disabled while its confirm or request is open (no double send)', async () => {
  openSheet();
  await tick();
  await tap(10, 60);
  const skipBtn = buttons(document.body, 'Skip')[0];
  skipBtn.fire('click');
  await tick();
  assert.equal(skipBtn.disabled, true);
  skipBtn.fire('click');                 // ignored: one confirm only
  await tick();
  assert.equal(buttons(document.body, 'Continue').length, 1);
  let release;
  reply = () => new Promise((resolve) => { release = resolve; });
  buttons(document.body, 'Continue')[0].fire('click');
  await tick();
  assert.equal(skipBtn.disabled, true, 'still disabled while the POST runs');
  release({ ok: false, status: 409, error: 'capability_unavailable', message: 'The print changed; reopen Skip Objects' });
  await tick();
  assert.equal(posts.length, 1);
  assert.equal(skipBtn.disabled, false, 'usable again after a refusal');
  assert.ok(find(document.body, (n) => /The print changed/.test(n.textContent)).length);
});

test('the toast follows the action the bridge took', async () => {
  openSheet();
  await tick();
  await tap(10, 60);
  reply = async () => ({ ok: true, status: 200, data: { action: 'stop' } });
  buttons(document.body, 'Skip')[0].fire('click');
  await tick();
  buttons(document.body, 'Continue')[0].fire('click');
  await tick();
  assert.ok(find(document.body, (n) => n.textContent === 'Stopping…').length);
});

test('a new job on the printer reloads the sheet', async () => {
  store.applySnapshot(PID, { printer_id: PID, job: { skipped_objects: [85] }, _raw: { subtask_name: 'multi3', gcode_file: 'multi3.gcode.3mf' } });
  openSheet();
  await tick();
  assert.equal(gets, 1);
  store.applyDelta(PID, { _raw: { layer_num: 3 } });
  assert.equal(gets, 1, 'telemetry alone does not reload');
  store.applyDelta(PID, { _raw: { subtask_name: 'next', gcode_file: 'next.gcode.3mf' } });
  await tick();
  assert.equal(gets, 2);
  // The bridge still answers with the old job: no reload loop.
  store.applyDelta(PID, { _raw: { layer_num: 4 } });
  await tick();
  assert.equal(gets, 2);
});

test('jobChanged ignores a snapshot without job fields', () => {
  assert.equal(skip.jobChanged(IDENTITY, { _raw: {} }), false);
  assert.equal(skip.jobChanged(IDENTITY, { _raw: { subtask_name: 'multi3', gcode_file: 'multi3.gcode.3mf' } }), false);
  assert.equal(skip.jobChanged(IDENTITY, { _raw: { gcode_file: 'Metadata/plate_1.gcode' } }), true);
});
