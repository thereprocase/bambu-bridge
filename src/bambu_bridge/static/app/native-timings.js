export function timingSummary(receipt) {
  const stages = [
    ['Receive', 'created', 'received_at'],
    ['Wait for delivery', 'received_at', 'delivery_started_at'],
    ['Deliver to printer', 'delivery_started_at', 'delivered_at'],
    ['Check readiness', 'readiness_started_at', 'ready_at'],
    ['Printer acknowledgement', 'dispatched_at', 'acknowledged_at'],
    ['Begin printing', 'acknowledged_at', 'running_at'],
  ];
  const measured = stages.flatMap(([label, from, to]) => {
    const a = receipt[from], b = receipt[to];
    if (typeof a !== 'number' || typeof b !== 'number' || !Number.isFinite(a) || !Number.isFinite(b)) return [];
    // Dispatch/creation use the existing whole-second clock; subsecond rounding
    // must not turn a valid fast acknowledgement into a negative duration.
    return [`${label}: ${Math.max(0, b - a).toFixed(1)}s`];
  });
  return measured.length ? measured.join(' · ') : 'Stage timings were not recorded for this receipt.';
}
