/**
 * Offset between this device's clock and the bridge's, by the NTP on-wire
 * protocol (RFC 5905 section 8) and NTP's clock filter.
 *
 * The live socket sends `{type:"clock", t0}` (device send time); the bridge
 * replies with t1 (its receive time) and t2 (its send time); t3 is the device
 * receive time. offset = ((t1 - t0) + (t2 - t3)) / 2 is bridge minus device;
 * delay = (t3 - t0) - (t2 - t1) is the network round trip, and the offset error
 * is at most delay / 2. Of the last 8 samples the lowest-delay one is used, as
 * in NTP's clock filter. A reply without t2 (t2 = t1) is still valid.
 */

export interface ClockEstimate {
  /** Bridge clock minus device clock, ms. */
  offsetMs: number;
  /** Network round-trip delay of the sample the offset came from, ms. */
  rttMs: number;
  samples: number;
}

const MAX_SAMPLES = 8;
const MAX_RTT_MS = 5_000;
const samples = new Map<string, { offset: number; rtt: number }[]>();

export function recordClockSample(
  key: string, t0: number, t1: number, t3: number, t2: number = t1,
): ClockEstimate | null {
  const rtt = (t3 - t0) - (t2 - t1);
  if ([t0, t1, t2, t3].every(Number.isFinite) && t2 >= t1 && rtt >= 0 && rtt <= MAX_RTT_MS) {
    const list = samples.get(key) ?? [];
    list.push({ offset: ((t1 - t0) + (t2 - t3)) / 2, rtt });
    samples.set(key, list.slice(-MAX_SAMPLES));
  }
  return clockFor(key);
}

export function clockFor(key: string): ClockEstimate | null {
  const list = samples.get(key);
  if (!list?.length) return null;
  const best = list.reduce((a, b) => (b.rtt < a.rtt ? b : a));
  return { offsetMs: best.offset, rttMs: best.rtt, samples: list.length };
}

/** Test hook. */
export function resetClockSync(): void { samples.clear(); }
