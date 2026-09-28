import type { PrinterSnapshot } from "../../api/types";
import { connectionStripLabels } from "../connectionStrip";

const snapshot = (state: string, connected = true) => ({
  session: { connected, last_telemetry_at: new Date().toISOString() },
  _raw: { gcode_state: state, layer_num: 3 },
}) as unknown as PrinterSnapshot;

test.each([
  ["IDLE", "Idle", "■"], ["PREPARE", "Preparing", "◷"],
  ["RUNNING", "Printing", "▶"], ["PAUSE", "Paused", "Ⅱ"],
  ["FINISH", "Finished", "■"], ["FAILED", "Print failed", "!"],
  ["UNRECOGNIZED", "Unknown", "?"],
])("%s has a text label alongside its symbol", (state, activity, symbol) => {
  expect(connectionStripLabels(snapshot(state), "open", true)).toMatchObject({
    bridge: "Connected", activity, symbol,
  });
});
test("printer unreachable does not imply bridge unreachable", () => {
  expect(connectionStripLabels(snapshot("RUNNING", false), "open", true)).toMatchObject({
    bridge: "Connected", activity: "Unknown", symbol: "?", connection: "Printer unreachable",
  });
});
test("lost bridge does not retain a live printing symbol", () => {
  expect(connectionStripLabels(snapshot("RUNNING"), "closed", true, "down")).toMatchObject({
    bridge: "Unreachable", activity: "Unknown", symbol: "?",
  });
});
test("working remote requests establish bridge access while the status stream reconnects", () => {
  expect(connectionStripLabels(snapshot("RUNNING"), "closed", true, "remote")).toMatchObject({
    bridge: "Connected", activity: "Unknown", connection: "Status stream unavailable",
  });
});
test("fresh reports use render time, not an earlier timer tick", () => {
  jest.useFakeTimers();
  try {
    jest.setSystemTime(new Date("2026-09-27T18:00:00Z"));
    const report = snapshot("RUNNING");
    jest.setSystemTime(new Date("2026-09-27T18:00:01Z"));
    report.session!.last_telemetry_at = new Date().toISOString();
    expect(connectionStripLabels(report, "open", true).activity).toBe("Printing");
    jest.advanceTimersByTime(16000);
    expect(connectionStripLabels(report, "open", true).connection).toBe("Status stale");
  } finally { jest.useRealTimers(); }
});
