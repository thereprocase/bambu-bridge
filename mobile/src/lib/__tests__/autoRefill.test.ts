import { autoRefillSource, autoRefillNotices, autoRefillLabel, autoRefillSummary } from "../autoRefill";
const success = { hex: "0700_2000_0003_0002", severity: "info" };
function snapshot(slot: unknown = 2, hms: unknown[] = [success], run = 1): Record<string, unknown> {
  return { phase: "printing", job: { run_id: run, subtask_name: "part" }, ams: { engaged_slot: slot }, hms };
}
test("only completed auto-refill codes qualify, never a real runout or feed fault", () => {
  expect(autoRefillSource(success.hex)).toBe(1);
  expect(autoRefillSource("0700_2300_0003_0002")).toBe(4);
  for (const code of [undefined, "0700_2000_0002_0001", "0700_2000_0002_0002", "0700_2000_0002_0011", "0700_2000_0003_0001", "random"])
    expect(autoRefillSource(code)).toBeNull();
});
test("completed handoff renders 1 → 2, and does not follow a later colour change", () => {
  const initial = snapshot(); const notices = autoRefillNotices(initial);
  expect(notices).toEqual([{ hex: success.hex, from: 1, to: 2 }]);
  expect(autoRefillLabel(notices[0])).toBe("1 → 2 · Auto-refilled");
  expect(autoRefillNotices(snapshot(3), notices, initial)[0].to).toBe(2);
});
test.each([null, 1, 254, "2", 0, 5])("missing/ambiguous destination %s is not fabricated", (slot) => {
  expect(autoRefillNotices(snapshot(slot))[0].to).toBeNull();
});
test("a notice left over across a new run cannot claim its current tray is the destination", () => {
  const prior = snapshot(); const notices = autoRefillNotices(prior);
  expect(autoRefillNotices(snapshot(3, [success], 2), notices, prior)[0].to).toBeNull();
});
test("stale or paused success remains a historical notice with no inferred destination", () => {
  expect(autoRefillNotices(snapshot(2, [{ ...success, stale: true }]))[0].to).toBeNull();
  expect(autoRefillNotices({ ...snapshot(), phase: "paused" })[0].to).toBeNull();
});
test("clear, malformed and unrelated HMS do not leave a refill notice latched", () => {
  expect(autoRefillNotices(snapshot(2, []))).toEqual([]);
  expect(autoRefillNotices(snapshot(2, [null, {}, { hex: "0700_2000_0002_0001" }]))).toEqual([]);
  expect(autoRefillNotices(null)).toEqual([]);
});

test("bridge history survives clearing HMS and formats successive handoffs as a chain", () => {
  const snap = { ...snapshot(3, []), ams: { engaged_slot: 3, refill_history: [
    { from_slot: 1, to_slot: 2, status: "completed", observed_at: "one" },
    { from_slot: 2, to_slot: 3, status: "completed", observed_at: "two" },
  ] } };
  expect(autoRefillSummary(autoRefillNotices(snap))).toBe("1 → 2 → 3 · Auto-refilled");
});
test("bridge distinguishes switching from completed and does not invent a destination", () => {
  const snap = { ...snapshot(), ams: { refill_history: [
    { from_slot: 1, to_slot: 2, status: "completed" },
    { from_slot: 2, to_slot: null, status: "switching" },
    null, { from_slot: 9, status: "completed" },
  ] } };
  expect(autoRefillSummary(autoRefillNotices(snap))).toBe("1 → 2 · Auto-refilled · 2 → … · Switching filament");
});
