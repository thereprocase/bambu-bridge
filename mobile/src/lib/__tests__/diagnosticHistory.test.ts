import { faultDetails, historyTime, refillDetails } from "../diagnosticHistory";
import type { FaultHistoryRow } from "../../api/types";
const row: FaultHistoryRow = { id: "hms:code", code: "0300_0100_0001_0001", text: "Heater fault",
  severity: "error", channel: "hms", first_seen: "2026-10-12T10:00:00Z", last_seen: "2026-10-12T10:01:00Z",
  count: 2, active: true };
test("copied diagnostic includes code, explanation, both times and recurrence count", () => {
  const text = faultDetails(row);
  for (const part of [row.code, row.text, "First seen:", "Last seen:", "Occurrences: 2", "Status: Active"])
    expect(text).toContain(part);
  expect(faultDetails({ ...row, active: false })).toContain("Status: Cleared");
  expect(faultDetails({ ...row, stale: true })).toContain("Status: Latched");
});
test("missing or invalid observations aren't represented as real timestamps", () => {
  expect(historyTime(undefined)).toBe("Not observed");
  expect(historyTime("garbage")).toBe("Not observed");
  expect(refillDetails({ from_slot: 2, to_slot: null, status: "switching", observed_at: "2026-10-12T10:00:00Z" }))
    .toContain("Completion observed: Not observed");
});
test("old completed rows preserve their recorded observation without inventing a start", () => {
  const result = refillDetails({ from_slot: 2, to_slot: 3, status: "completed", observed_at: "2026-10-12T10:00:00Z" });
  expect(result).toContain("2 → 3 · completed");
  expect(result).toContain("Switch first observed: Not observed");
});
