// Keep power, transport, printer activity, and errors independent. A lost
// connection does not establish that the printer is powered off.
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
  const stamp = Date.parse(session.last_telemetry_at || '');
  if (!Number.isFinite(stamp) || now - stamp > 15000 || stamp > now) return unavailable('Status stale');

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
