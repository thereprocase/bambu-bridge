import type { PrinterSnapshot } from "../../api/types";
import { ownerLabel, startLabel } from "../nativeState";
import { printerStateLabels } from "../printerState";

const NOW = Date.parse("2026-09-27T12:00:00Z");

function snapshot(state: string, layer = 0): PrinterSnapshot {
  return {
    session: { connected: true, last_telemetry_at: new Date(NOW).toISOString(),
      last_connect_attempt: null, last_failure_phase: null },
    cert_status: "trusted", _raw: { gcode_state: state, layer_num: layer },
    job: { layer_num: layer }, hms: [], print_error: null,
  } as unknown as PrinterSnapshot;
}

test.each([
  ["IDLE", 0, "Idle", true], ["PREPARE", 0, "Preparing", false],
  ["RUNNING", 0, "Preparing", false], ["RUNNING", 2, "Printing", false],
  ["PAUSE", 2, "Paused", false], ["FINISH", 2, "Finished", true],
  ["FAILED", 2, "Print failed", true], ["UNKNOWN", 0, "Unknown", false],
])("printer %s layer %i -> %s", (state, layer, activity, ready) => {
  const labels = printerStateLabels(snapshot(state, layer), "open", NOW);
  expect(labels).toMatchObject({ power: "On", connection: "Connected", activity,
    live: true, printerReady: ready });
});

test("a dropped link does not prove printer power is off or preserve live activity", () => {
  const last = snapshot("RUNNING", 5);
  expect(printerStateLabels(last, "closed", NOW)).toMatchObject({
    power: "Unknown", connection: "Status stream unavailable", activity: "Unknown", live: false,
  });
  last.session.connected = false;
  last.session.last_failure_phase = "mqtt_connack";
  expect(printerStateLabels(last, "open", NOW)).toMatchObject({
    power: "Unknown", connection: "Printer login failed", activity: "Unknown", live: false,
  });
});

test("stale telemetry and changed certificate cannot imply readiness", () => {
  const last = snapshot("IDLE");
  expect(printerStateLabels(last, "open", NOW + 16_000)).toMatchObject({
    power: "Unknown", connection: "Status stale", activity: "Unknown", printerReady: false,
  });
  last.cert_status = "changed";
  expect(printerStateLabels(last, "open", NOW)).toMatchObject({
    connection: "Printer identity changed", printerReady: false,
  });
});

test("issues remain separate from printer activity and ignore stale HMS", () => {
  const live = snapshot("RUNNING", 4);
  live.print_error = { text: "Error" } as PrinterSnapshot["print_error"];
  live.hms = [
    { severity: "warn", stale: false }, { severity: "error", stale: true },
  ] as PrinterSnapshot["hms"];
  expect(printerStateLabels(live, "open", NOW)).toMatchObject({
    activity: "Printing", issues: ["Print error", "Printer warning"],
  });
});

test("start labels do not call a pending start a waiting-list item", () => {
  expect(startLabel("queued")).toBe("Start pending");
  expect(startLabel("unknown")).toBe("Start unconfirmed");
  expect(ownerLabel({ start_owner: null } as never)).toBe("No bridge start in progress");
  expect(ownerLabel({ start_owner: "a", start_owner_state: "running" } as never))
    .toBe("Bridge print active");
});
