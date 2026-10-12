import { clockFor, recordClockSample, resetClockSync } from "../clockSync";

beforeEach(() => resetClockSync());

test("RFC 5905 offset and delay with bridge hold time removed", () => {
  // Bridge 2000 ms ahead; 40 ms out, 60 ms back; bridge holds the reply 25 ms.
  const t0 = 1_000_000, t1 = t0 + 40 + 2_000, t2 = t1 + 25, t3 = t2 - 2_000 + 60;
  const est = recordClockSample("p", t0, t1, t3, t2)!;
  expect(est.rttMs).toBe(100);
  expect(Math.abs(est.offsetMs - 2_000)).toBeLessThanOrEqual(est.rttMs / 2);
  expect(est.offsetMs).toBe(1_990);          // asymmetric path: error 10 ms <= delay/2
});

test("clock filter keeps the lowest-delay of the last 8 samples", () => {
  recordClockSample("p", 0, 500 + 400, 800);          // slow: delay 800
  recordClockSample("p", 1_000, 1_000 + 500 + 10, 1_020); // fast: delay 20, offset 500
  for (let i = 0; i < 5; i++) recordClockSample("p", 2_000 + i, 2_000 + i + 700, 2_000 + i + 300);
  expect(clockFor("p")).toMatchObject({ offsetMs: 500, rttMs: 20 });
  for (let i = 0; i < 8; i++) recordClockSample("p", 9_000 + i, 9_000 + i + 520, 9_000 + i + 80);
  expect(clockFor("p")!.rttMs).toBe(80);              // the fast sample aged out
});

test("rejects impossible samples and keeps printers separate", () => {
  expect(recordClockSample("p", 10, 5, 5)).toBeNull();          // reply before request
  expect(recordClockSample("p", 0, 10, 9_000)).toBeNull();      // delay over 5 s
  expect(recordClockSample("p", 0, 10, 20, 5)).toBeNull();      // t2 before t1
  recordClockSample("a", 0, 1_000, 10);
  expect(clockFor("b")).toBeNull();
});
