import type { PrinterSnapshot } from "../api/types";
import type { LiveStatus } from "../ws/live";
import { clockFor } from "./clockSync";

export interface PrinterStateLabels {
  power: "On" | "Unknown";
  connection: string;
  activity: string;
  issues: string[];
  live: boolean;
  printerReady: boolean;
  /** live <= LIVE_MS old; aging up to FRESH_MS (still live, labelled); else stale. */
  freshness?: "live" | "aging" | "stale";
  /** Age of the last printer report, in the bridge's time base when known. */
  ageMs?: number;
}

/** A P1S reports every 2-4 s while printing; two missed reports is noticeable. */
export const LIVE_MS = 5_000;
/** Same 15 s the bridge requires before it accepts a control command. */
export const FRESH_MS = 15_000;
// Phone and bridge clocks differ, and the old check (stamp later than the
// phone's "now" = stale) hid live status whenever the phone ran even slightly
// behind. Age is now measured in the bridge's time base using the NTP-style
// offset from clockSync. Until the first clock sample arrives, age is the time
// since THIS device last saw the stamp change, and stamps more than SKEW_MS
// off are ignored as implausible, so a long-cached snapshot never looks live.
const SKEW_MS = 600_000;
const seen = new Map<string, { stamp: string; at: number }>();

/** Test hook: forget when stamps were first seen. */
export function resetTelemetryClock(): void { seen.clear(); }

/** Local ms since this printer's telemetry stamp last changed. */
function telemetryAge(key: string, stamp: string, now: number): number {
  const prev = seen.get(key);
  if (!prev || prev.stamp !== stamp || now < prev.at) {
    seen.set(key, { stamp, at: now });
    return 0;
  }
  return now - prev.at;
}

/**
 * Age of the latest report. With a clock estimate: bridge time now minus the
 * stamp, floored at zero. A stamp ahead of corrected bridge time by more than
 * the estimate's uncertainty means the estimate is off (e.g. a clock stepped),
 * so fall back to device receipt time. Null when the stamp is implausible.
 */
function telemetryAgeMs(key: string, stampText: string, stamp: number, now: number): number | null {
  const receiptAge = telemetryAge(key, stampText, now);
  const clock = clockFor(key);
  if (clock) {
    const age = now + clock.offsetMs - stamp;
    if (age >= -(clock.rttMs / 2 + 250)) return Math.max(0, age);
  }
  if (Math.abs(now - stamp) > FRESH_MS + SKEW_MS) return null;
  return receiptAge;
}

/** Independent facts: a lost connection never proves that power is off. */
export function printerStateLabels(
  snapshot: PrinterSnapshot | null,
  bridgeStatus: LiveStatus,
  now = Date.now(),
): PrinterStateLabels {
  if (bridgeStatus !== "open") {
    return { power: "Unknown", connection: bridgeStatus === "connecting" ? "Status stream connecting" : "Status stream unavailable",
      activity: "Unknown", issues: [], live: false, printerReady: false };
  }
  if (!snapshot) {
    return { power: "Unknown", connection: "Printer status unavailable", activity: "Unknown",
      issues: [], live: false, printerReady: false };
  }
  if (snapshot.cert_status === "changed") {
    return { power: "Unknown", connection: "Printer identity changed", activity: "Unknown",
      issues: [], live: false, printerReady: false };
  }
  const session = snapshot.session;
  if (!session?.connected) {
    const connection = session?.last_failure_phase === "mqtt_connack" ? "Printer login failed"
      : session?.last_failure_phase === "tls_handshake" || session?.last_failure_phase === "mqtt_protocol_error"
        ? "Connection error" : "Printer unreachable";
    return { power: "Unknown", connection, activity: "Unknown", issues: [], live: false, printerReady: false };
  }
  const stampText = session.last_telemetry_at ?? "";
  const stamp = stampText ? Date.parse(stampText) : NaN;
  const key = String((snapshot as { printer_id?: string; serial?: string }).printer_id
    ?? (snapshot as { serial?: string }).serial ?? "");
  const ageMs = Number.isFinite(stamp) ? telemetryAgeMs(key, stampText, stamp, now) : null;
  if (ageMs === null || ageMs > FRESH_MS) {
    return { power: "Unknown", connection: "Status stale", activity: "Unknown",
      issues: [], live: false, printerReady: false, freshness: "stale", ageMs: ageMs ?? undefined };
  }
  const freshness = ageMs <= LIVE_MS ? "live" : "aging";

  const raw = snapshot._raw as Record<string, unknown> | undefined;
  const state = raw?.gcode_state;
  const layer = snapshot.job?.layer_num ?? raw?.layer_num;
  const activity = state === "IDLE" ? "Idle"
    : state === "PREPARE" ? "Preparing"
      : state === "RUNNING" ? Number(layer) > 0 ? "Printing" : "Preparing"
        : state === "PAUSE" ? "Paused"
          : state === "FINISH" ? "Finished"
            : state === "FAILED" ? "Print failed"
              : "Unknown";
  const issues: string[] = [];
  if (snapshot.print_error) issues.push("Print error");
  const hms = (snapshot.hms ?? []).filter((item) => !item.stale);
  if (hms.some((item) => item.severity === "error")) issues.push("Printer error");
  if (hms.some((item) => item.severity === "warn")) issues.push("Printer warning");
  if (hms.some((item) => item.severity !== "error" && item.severity !== "warn" && item.severity !== "info")) issues.push("Printer issue");
  if (snapshot.job_anomaly) issues.push("Job warning");
  return { power: "On", connection: freshness === "live" ? "Connected" : `Updated ${Math.round(ageMs / 1000)} s ago`,
    activity, issues, live: true, freshness, ageMs,
    printerReady: state === "IDLE" || state === "FINISH" || state === "FAILED" };
}
