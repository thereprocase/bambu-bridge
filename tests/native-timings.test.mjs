import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../src/bambu_bridge/static/app/native-timings.js', import.meta.url), 'utf8');
const { timingSummary } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('shows delivery separately from printer acknowledgement', () => {
  const text = timingSummary({ delivery_started_at: 100, delivered_at: 134, dispatched_at: 135, acknowledged_at: 136.2 });
  assert.match(text, /Deliver to printer: 34.0s/);
  assert.match(text, /Printer acknowledgement: 1.2s/);
  assert.doesNotMatch(text, /Begin printing/);
});
test('old or incomplete receipts do not invent timings', () => {
  assert.match(timingSummary({ created: 100, received_at: null }), /not recorded/);
});
test('whole-second dispatch rounding never shows negative latency', () => {
  assert.match(timingSummary({ dispatched_at: 100, acknowledged_at: 99.9 }), /0.0s/);
});
