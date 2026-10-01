// Keep power, transport, printer activity, and errors independent. A lost
// connection does not establish that the printer is powered off.

import { clockFor } from './clock-sync.js';

// Telemetry age is measured in the bridge's time base via the NTP offset from
// clock-sync.js; the old check (stamp later than this device's "now" = stale)
// hid live status whenever this device's clock ran slightly behind. Until the
// first clock sample, age is the time since this browser last saw the stamp
// change, and stamps more than SKEW_MS off are ignored. Mirrors mobile.
export const LIVE_MS = 5000;     // a P1S reports every 2-4 s while printing
export const FRESH_MS = 15000;   // the bridge's own gate for control commands
const SKEW_MS = 600000;
const seen = new Map();
export function resetTelemetryClock() { seen.clear(); }
function telemetryAge(key, stamp, now) {
  const prev = seen.get(key);
  if (!prev || prev.stamp !== stamp || now < prev.at) {
    seen.set(key, { stamp, at: now });
    return 0;
  }
  return now - prev.at;
}

/** Age of the latest report (see mobile printerState.telemetryAgeMs). */
export function telemetryAgeMs(key, stampText, stamp, now = Date.now()) {
  const receiptAge = telemetryAge(key, stampText, now);
  const clock = clockFor(key);
  if (clock) {
    const age = now + clock.offsetMs - stamp;
    if (age >= -(clock.rttMs / 2 + 250)) return Math.max(0, age);
  }
  if (Math.abs(now - stamp) > FRESH_MS + SKEW_MS) return null;
  return receiptAge;
}

export function printerStateLabels(snapshot, bridgeConnected, now = Date.now()) {
  const unavailable = (connection) => ({ power: 'Unknown', connection, activity: 'Unknown', issues: [], live: false });
  if (!bridgeConnected) return unavailable('Bridge unreachable');
  if (!snapshot) return unavailable('Printer status unavailable');
  if (snapshot.cert_status === 'changed') return unavailable('Printer identity changed');
  const session = snapshot.session || {};
  if (!session.connected) {
    const phase = session.last_failure_phase;
    return unavailable(phase === 'mqtt_connack' ? 'Printer login failed'
      : ['tls_handshake', 'mqtt_protocol_error'].includes(phase) ? 'Connection error' : 'Printer unreachable');
  }
  const stampText = session.last_telemetry_at || '';
  const stamp = Date.parse(stampText);
  const key = String(snapshot.printer_id ?? snapshot.serial ?? '');
  const ageMs = Number.isFinite(stamp) ? telemetryAgeMs(key, stampText, stamp, now) : null;
  if (ageMs === null || ageMs > FRESH_MS) {
    return { ...unavailable('Status stale'), freshness: 'stale', ageMs: ageMs ?? undefined };
  }
  const freshness = ageMs <= LIVE_MS ? 'live' : 'aging';

  const raw = snapshot._raw || {};
  const state = raw.gcode_state;
  const layer = snapshot.job?.layer_num ?? raw.layer_num;
  const activity = state === 'IDLE' ? 'Idle'
    : state === 'PREPARE' ? 'Preparing'
      : state === 'RUNNING' ? Number(layer) > 0 ? 'Printing' : 'Preparing'
        : state === 'PAUSE' ? 'Paused'
          : state === 'FINISH' ? 'Finished'
            : state === 'FAILED' ? 'Print failed' : 'Unknown';
  const issues = [];
  if (snapshot.print_error) issues.push('Print error');
  const hms = (snapshot.hms || []).filter((item) => !item.stale);
  if (hms.some((item) => item.severity === 'error')) issues.push('Printer error');
  if (hms.some((item) => item.severity === 'warn')) issues.push('Printer warning');
  if (snapshot.job_anomaly) issues.push('Job warning');
  return { power: 'On', connection: freshness === 'live' ? 'Connected' : `Updated ${Math.round(ageMs / 1000)} s ago`,
    activity, issues, live: true, freshness, ageMs };
}
