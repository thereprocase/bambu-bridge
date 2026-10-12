import test from 'node:test';
import assert from 'node:assert/strict';

const appDir = new URL('../src/bambu_bridge/static/app/', import.meta.url);
const { amsMapping, autoMatch, projectFilamentCount, colorDistance } =
  await import(new URL('ams-mapping.js', appDir));

const slots = [
  { physical_slot: 1, type: 'PLA', color: '#FFFFFF', state: 'loaded' },
  { physical_slot: 2, type: 'PETG', color: '#000000', state: 'loaded' },
  { physical_slot: 3, type: 'PLA', color: '#FF0000', state: 'loaded' },
  { physical_slot: 4, type: null, color: null, state: 'empty' },
];

test('ams_mapping has one entry per project filament, -1 where unused (Orca v0)', () => {
  // project of 4 presets, the plate uses filaments 2 and 4
  const filaments = [{ index: 2, tray: 1 }, { index: 4, tray: 0 }];
  assert.deepEqual(amsMapping(filaments, 4), [-1, 1, -1, 0]);
  // physical slot 2 alone in a one-filament project is "1", which is what the
  // operator's p1s-print helper already sends
  assert.deepEqual(amsMapping([{ index: 1, tray: 1 }], 1), [1]);
  // external spool is -1, so an all-external print is all -1 (use_ams false)
  assert.deepEqual(amsMapping([{ index: 1, tray: 'external' }], 2), [-1, -1]);
});

test('project filament count comes from project_settings, else the plate', () => {
  const plate = [{ index: 1 }, { index: 3 }];
  assert.equal(projectFilamentCount({ filament_settings_id: ['a', 'b', 'c', 'd'] }, plate), 4);
  assert.equal(projectFilamentCount({ filament_type: ['PLA', 'PLA', 'PETG'] }, plate), 3);
  assert.equal(projectFilamentCount(null, plate), 3);
  assert.equal(projectFilamentCount({ filament_type: ['PLA'] }, plate), 3);  // inconsistent file
});

test('auto-match picks same type, nearest colour, free trays first; no match stays unassigned', () => {
  const picks = autoMatch([
    { index: 1, type: 'PLA', color: '#F01010' },
    { index: 2, type: 'PETG', color: '#202020' },
    { index: 3, type: 'ABS', color: '#000000' },
  ], slots);
  assert.equal(picks.get(1), 2);      // red PLA -> slot 3 (tray 2), not white slot 1
  assert.equal(picks.get(2), 1);      // PETG -> slot 2 (tray 1)
  assert.equal(picks.get(3), null);   // no ABS loaded: the user must choose
});

test('two filaments of one material share a tray only when no free one matches', () => {
  const picks = autoMatch([
    { index: 1, type: 'PETG', color: '#000000' },
    { index: 2, type: 'PETG', color: '#111111' },
  ], slots);
  assert.deepEqual([picks.get(1), picks.get(2)], [1, 1]);
  const pla = autoMatch([
    { index: 1, type: 'PLA', color: '#FFFFFF' },
    { index: 2, type: 'PLA', color: '#FEFEFE' },
  ], slots);
  assert.deepEqual(new Set([pla.get(1), pla.get(2)]), new Set([0, 2]));
});

test('colour distance is CIE76 in Lab', () => {
  assert.equal(colorDistance('#123456', '#123456'), 0);
  assert.ok(Math.abs(colorDistance('#000000', '#FFFFFF') - 100) < 0.01);
});
