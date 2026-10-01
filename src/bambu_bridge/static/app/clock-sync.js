// clock-sync.js — offset between this browser's clock and the bridge's, by the
// NTP on-wire protocol (RFC 5905 section 8) and NTP's clock filter. Mirrors
// mobile/src/lib/clockSync.ts.
//
// ws.js sends {type:'clock', t0}; the bridge replies with t1 (receive) and t2
// (send); t3 is our receive time. offset = ((t1 - t0) + (t2 - t3)) / 2 is
// bridge minus browser; delay = (t3 - t0) - (t2 - t1); the offset error is at
// most delay / 2. The lowest-delay of the last 8 samples is used.

const MAX_SAMPLES = 8;
const MAX_RTT_MS = 5000;
const samples = new Map();

export function recordClockSample(key, t0, t1, t3, t2 = t1) {
  const rtt = (t3 - t0) - (t2 - t1);
  if ([t0, t1, t2, t3].every(Number.isFinite) && t2 >= t1 && rtt >= 0 && rtt <= MAX_RTT_MS) {
    const list = samples.get(key) || [];
    list.push({ offset: ((t1 - t0) + (t2 - t3)) / 2, rtt });
    samples.set(key, list.slice(-MAX_SAMPLES));
  }
  return clockFor(key);
}

export function clockFor(key) {
  const list = samples.get(key);
  if (!list || !list.length) return null;
  const best = list.reduce((a, b) => (b.rtt < a.rtt ? b : a));
  return { offsetMs: best.offset, rttMs: best.rtt, samples: list.length };
}

export function resetClockSync() { samples.clear(); }
