import { test } from 'node:test';
import assert from 'node:assert/strict';
import { jpegFrames } from '../src/bambu_bridge/static/app/camera-stream.js';

const jpeg = Uint8Array.from([255, 216, 0, 1, 2, 255, 217]);
function part(data = jpeg) {
  const header = new TextEncoder().encode(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${data.length}\r\n\r\n`);
  return Uint8Array.from([...header, ...data, 13, 10]);
}
function response(chunks) {
  return new Response(new ReadableStream({ start(c) { for (const chunk of chunks) c.enqueue(chunk); c.close(); } }), {
    headers: { 'Content-Type': 'multipart/x-mixed-replace; boundary=frame' },
  });
}
test('accepts byte-split headers and JPEGs without waiting for another frame', async () => {
  const bytes = part();
  const stream = jpegFrames(response(Array.from(bytes, byte => Uint8Array.of(byte))));
  assert.deepEqual((await stream.next()).value, jpeg);
  await stream.return();
});
test('delivers coalesced frames immediately and reports EOF', async () => {
  const stream = jpegFrames(response([Uint8Array.from([...part(), ...part(), ...part()])]));
  for (let i = 0; i < 3; i++) assert.deepEqual((await stream.next()).value, jpeg);
  await assert.rejects(stream.next(), /ended/);
});
test('rejects missing length, oversized headers, oversized frames, and corrupt JPEGs', async () => {
  const bad = [
    new TextEncoder().encode('--frame\r\nContent-Type: image/jpeg\r\n\r\n'),
    new Uint8Array(4096).fill(65),
    new TextEncoder().encode('--frame\r\nContent-Length: 99999999\r\n\r\n'),
    part(Uint8Array.of(0, 1, 2, 3)),
  ];
  for (const bytes of bad) await assert.rejects(jpegFrames(response([bytes])).next(), /Invalid/);
});
test('closing the viewer cancels its streaming body', async () => {
  let canceled = false;
  const body = new ReadableStream({ start(c) { c.enqueue(part()); }, cancel() { canceled = true; } });
  const stream = jpegFrames(new Response(body, { headers: { 'Content-Type': 'multipart/x-mixed-replace' } }));
  await stream.next(); await stream.return(); assert.equal(canceled, true);
});
