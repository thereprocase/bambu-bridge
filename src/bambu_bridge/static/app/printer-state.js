// Keep power, transport, printer activity, and errors independent. A lost
// connection does not establish that the printer is powered off.

// Freshness is judged by when this browser last saw the telemetry stamp
// change, not by comparing the bridge's clock with this device's: a device
// clock slightly behind the bridge made every fresh stamp look stale.
// Offsets up to SKEW_MS either way are tolerated (mirrors mobile printerState).
const FRESH_MS = 15000;
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
  if (!Number.isFinite(stamp) || Math.abs(now - stamp) > FRESH_MS + SKEW_MS
      || telemetryAge(key, stampText, now) > FRESH_MS) return unavailable('Status stale');

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
  return { power: 'On', connection: 'Connected', activity, issues, live: true };
}
