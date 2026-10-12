import type { PrinterSnapshot } from "../../api/types";
import { ownerLabel, startLabel } from "../nativeState";
import { recordClockSample, resetClockSync } from "../clockSync";
import { printerStateLabels, resetTelemetryClock } from "../printerState";

beforeEach(() => { resetTelemetryClock(); resetClockSync(); });

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
  expect(printerStateLabels(last, "open", NOW).live).toBe(true);
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

test.each([
  ["behind", -3_000], ["slightly behind", -300], ["ahead", 4_000], ["a minute ahead", 60_000],
])("a phone clock %s the bridge still shows fresh telemetry as live", (_, offset) => {
  // `now` is the phone's clock; stamps are the bridge's. Advance both.
  const live = snapshot("RUNNING", 2);
  for (let t = 0; t <= 30_000; t += 2_000) {
    live.session.last_telemetry_at = new Date(NOW + t).toISOString();
    expect(printerStateLabels(live, "open", NOW + t + offset + 50)).toMatchObject({
      live: true, activity: "Printing",
    });
  }
});

test("telemetry that stops changing goes stale after 15 s of this device's time", () => {
  const quiet = snapshot("RUNNING", 2);
  const phone = NOW - 5_000;          // phone 5 s behind the bridge
  expect(printerStateLabels(quiet, "open", phone).live).toBe(true);
  expect(printerStateLabels(quiet, "open", phone + 15_000).live).toBe(true);
  expect(printerStateLabels(quiet, "open", phone + 15_001)).toMatchObject({ live: false, connection: "Status stale" });
});

test("an implausibly old or future stamp is never live", () => {
  const old = snapshot("RUNNING", 2);
  expect(printerStateLabels(old, "open", NOW + 16 * 60_000).live).toBe(false);
  expect(printerStateLabels(old, "open", NOW - 16 * 60_000).live).toBe(false);
});

test.each([-4_000, -300, 0, 2_500, 90_000])(
  "with a clock estimate, age is exact for a phone %i ms off the bridge", (phoneOffset) => {
  // phone = bridge + phoneOffset. Probe: 30 ms each way, bridge holds 5 ms.
  const bridgeNow = NOW + 60_000;
  const t0 = bridgeNow + phoneOffset, t1 = bridgeNow + 30, t2 = t1 + 5, t3 = t2 + 30 + phoneOffset;
  recordClockSample("", t0, t1, t3, t2);
  const s = snapshot("RUNNING", 2);
  const phoneNow = bridgeNow + 1_000 + phoneOffset;
  for (const [age, freshness, live] of [[1_000, "live", true], [9_000, "aging", true], [16_000, "stale", false]] as const) {
    s.session.last_telemetry_at = new Date(bridgeNow + 1_000 - age).toISOString();
    const out = printerStateLabels(s, "open", phoneNow);
    expect(out).toMatchObject({ freshness, live });
    expect(Math.abs(out.ageMs! - age)).toBeLessThanOrEqual(35);   // within half the delay
  }
});

test("aging status is labelled with its age and still allows controls", () => {
  recordClockSample("", NOW, NOW, NOW, NOW);
  const s = snapshot("IDLE");
  expect(printerStateLabels(s, "open", NOW + 9_000)).toMatchObject({
    live: true, printerReady: true, freshness: "aging", connection: "Updated 9 s ago" });
});
