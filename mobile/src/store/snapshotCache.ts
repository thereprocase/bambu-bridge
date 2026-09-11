import { kv } from "../lib/kv";

const WRITE_DELAY_MS = 5_000;

export interface SnapshotCache {
  read(printerId: string): Record<string, unknown> | null;
  schedule(printerId: string, snapshot: Record<string, unknown>, immediate?: boolean): void;
  flush(printerId?: string): void;
  cancel(): void;
}

const keyFor = (id: string) => `live.${id}.snapshot`;

/** Best-effort persistence for live snapshots. MMKV failures must never affect telemetry. */
export function createSnapshotCache(
  store: Pick<typeof kv, "getString" | "set"> = kv,
): SnapshotCache {
  const pending = new Map<string, Record<string, unknown>>();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();

  const write = (printerId: string) => {
    const snapshot = pending.get(printerId);
    if (!snapshot) return;
    pending.delete(printerId);
    const timer = timers.get(printerId);
    if (timer) clearTimeout(timer);
    timers.delete(printerId);
    try { store.set(keyFor(printerId), JSON.stringify(snapshot)); }
    catch { /* persistent cache is optional */ }
  };

  return {
    read(printerId) {
      try {
        const raw = store.getString(keyFor(printerId));
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
      } catch { return null; }
    },
    schedule(printerId, snapshot, immediate = false) {
      pending.set(printerId, snapshot);
      if (immediate) { write(printerId); return; }
      if (!timers.has(printerId)) {
        timers.set(printerId, setTimeout(() => write(printerId), WRITE_DELAY_MS));
      }
    },
    flush(printerId) {
      if (printerId) write(printerId);
      else Array.from(pending.keys()).forEach(write);
    },
    cancel() {
      timers.forEach((timer) => clearTimeout(timer));
      timers.clear();
      pending.clear();
    },
  };
}
