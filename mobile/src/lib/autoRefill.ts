/** Completed auto-refill notices, verified against BambuStudio hms_en_094.json.
 * 0003/0002 means a completed switch. Never match ordinary runout/feed faults.
 */
export interface AutoRefillNotice { hex: string; from: number; to: number | null; status?: "completed" | "switching" | "interrupted" }

export function autoRefillSource(hex: unknown): number | null {
  if (typeof hex !== "string") return null;
  const match = /^0700_2([0-3])00_0003_0002$/i.exec(hex);
  return match ? Number(match[1]) + 1 : null;
}

/** Capture the destination once while the success notice first arrives.
 * Don't relabel an old handoff when a later colour change engages another tray.
 * An unknown destination stays unknown; the firmware still confirms success.
 */
export function autoRefillNotices(
  snapshot: Record<string, unknown> | null,
  previous: AutoRefillNotice[] = [],
  previousSnapshot: Record<string, unknown> | null = null,
): AutoRefillNotice[] {
  if (!snapshot) return [];
  const job = snapshot.job as Record<string, unknown> | undefined;
  const oldJob = previousSnapshot?.job as Record<string, unknown> | undefined;
  const sameRun = job?.run_id === oldJob?.run_id && job?.subtask_name === oldJob?.subtask_name;
  const oldEntries = sameRun ? previous : [];
  const ams = snapshot.ams as Record<string, unknown> | undefined;
  if (Array.isArray(ams?.refill_history)) {
    return ams.refill_history.flatMap((value, i) => {
      if (!value || typeof value !== "object") return [];
      const row = value as Record<string, unknown>;
      const from = row.from_slot, to = row.to_slot;
      if (!Number.isInteger(from) || Number(from) < 1 || Number(from) > 4 ||
          !["completed", "switching", "interrupted"].includes(String(row.status))) return [];
      return [{ hex: `refill-${String(row.observed_at ?? i)}`, from: Number(from),
        to: Number.isInteger(to) && Number(to) >= 1 && Number(to) <= 4 && to !== from ? Number(to) : null,
        status: row.status as "completed" | "switching" | "interrupted" }];
    });
  }
  const engaged = ams?.engaged_slot;
  const entries = Array.isArray(snapshot.hms) ? snapshot.hms : [];
  const result: AutoRefillNotice[] = [];
  for (const item of entries) {
    if (!item || typeof item !== "object") continue;
    const entry = item as Record<string, unknown>;
    const from = autoRefillSource(entry.hex);
    if (from == null || result.some((notice) => notice.hex === entry.hex)) continue;
    const old = oldEntries.find((notice) => notice.hex === entry.hex);
    // A latched code already present before this run cannot identify its destination.
    const existed = !sameRun && Array.isArray(previousSnapshot?.hms) &&
      previousSnapshot.hms.some((e: any) => e?.hex === entry.hex);
    const to = old ? old.to : !existed && !entry.stale && snapshot.phase === "printing" &&
      Number.isInteger(engaged) && Number(engaged) >= 1 && Number(engaged) <= 4 && engaged !== from
      ? Number(engaged) : null;
    result.push({ hex: String(entry.hex), from, to });
  }
  return result;
}

export function autoRefillLabel(notice: AutoRefillNotice): string {
  if (notice.status === "interrupted") return `Slot ${notice.from} · Refill interrupted`;
  if (notice.status === "switching") return `${notice.from} → … · Switching filament`;
  return notice.to == null ? `Slot ${notice.from} · Auto-refilled` : `${notice.from} → ${notice.to} · Auto-refilled`;
}

/** Compact chronological chain without inventing unknown links. */
export function autoRefillSummary(notices: AutoRefillNotice[]): string {
  const groups: string[] = [];
  let chain: number[] = [];
  const flush = () => { if (chain.length) { groups.push(`${chain.join(" → ")} · Auto-refilled`); chain = []; } };
  for (const notice of notices) {
    if (notice.status === "switching" || notice.status === "interrupted" || notice.to == null) { flush(); groups.push(autoRefillLabel(notice)); continue; }
    if (chain.length && chain[chain.length - 1] !== notice.from) flush();
    if (!chain.length) chain.push(notice.from);
    chain.push(notice.to);
  }
  flush();
  return groups.join(" · ");
}
