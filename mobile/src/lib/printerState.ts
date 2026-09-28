import type { PrinterSnapshot } from "../api/types";
import type { LiveStatus } from "../ws/live";

export interface PrinterStateLabels {
  power: "On" | "Unknown";
  connection: string;
  activity: string;
  issues: string[];
  live: boolean;
  printerReady: boolean;
}

const FRESH_MS = 15_000;

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
  const stamp = session.last_telemetry_at ? Date.parse(session.last_telemetry_at) : NaN;
  if (!Number.isFinite(stamp) || now - stamp > FRESH_MS || stamp > now) {
    return { power: "Unknown", connection: "Status stale", activity: "Unknown",
      issues: [], live: false, printerReady: false };
  }

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
  return { power: "On", connection: "Connected", activity, issues, live: true,
    printerReady: state === "IDLE" || state === "FINISH" || state === "FAILED" };
}
