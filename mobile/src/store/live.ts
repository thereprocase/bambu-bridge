/**
 * Live state per printer — the WS layer writes here; screens read.
 *
 * One store, keyed by printer_id, holds:
 *   - snapshot:   merged state dict from snapshot+delta
 *   - status:     WS connection status
 *   - lastEvent:  most recent named event (drives toasts)
 *   - eventLog:   bounded ring of recent named events (drives activity tab)
 *
 * Snapshots are also persisted to MMKV under `live.<printer_id>.snapshot`
 * so the dashboard renders the last-known state immediately on cold app
 * launch (rather than the blank "connecting…" flicker every time).
 */

import { create } from "zustand";

import { kv } from "../lib/kv";
import { qaLog } from "../lib/qalog";
import { applyDelta, LiveConnection, LiveMessage, LiveStatus } from "../ws/live";

const KV_SNAP = (id: string) => `live.${id}.snapshot`;
const RING_SIZE = 100;

// QA throttle: track last emission timestamp per printer so status.snap fires
// at most once every 10 seconds regardless of snapshot/delta cadence.
const _snapLogAt: Record<string, number> = {};

export interface LiveEvent {
  name: string;
  data: Record<string, unknown>;
  at: number;          // epoch ms (local)
}

interface PrinterLive {
  snapshot: Record<string, unknown> | null;
  status: LiveStatus;
  statusDetail?: string;
  lastEvent: LiveEvent | null;
  eventLog: LiveEvent[];
}

interface LiveStore {
  printers: Record<string, PrinterLive>;
  conns: Record<string, LiveConnection>;
  ensureConnection(printerId: string): void;
  closeConnection(printerId: string): void;
  reset(): void;
}

const empty = (): PrinterLive => ({
  snapshot: null,
  status: "closed",
  lastEvent: null,
  eventLog: [],
});

function loadCached(printerId: string): Record<string, unknown> | null {
  const raw = kv.getString(KV_SNAP(printerId));
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function persist(printerId: string, snapshot: Record<string, unknown>) {
  kv.set(KV_SNAP(printerId), JSON.stringify(snapshot));
}

export const useLiveStore = create<LiveStore>((set, get) => ({
  printers: {},
  conns: {},

  ensureConnection: (printerId: string) => {
    const { conns } = get();
    if (conns[printerId]) return;

    // Seed with cached snapshot so UI doesn't flash blank.
    const cached = loadCached(printerId);
    set((s) => ({
      printers: {
        ...s.printers,
        [printerId]: {
          ...empty(),
          snapshot: cached,
          status: "connecting",
        },
      },
    }));

    const conn = new LiveConnection(printerId, {
      onMessage(msg: LiveMessage) {
        // Pure liveness frames — skip the store-update path entirely.
        if (msg.type === "hello" || msg.type === "ping") return;

        // Track whether this message updated the snapshot so we can fire the
        // QA log after the store update (outside the pure set updater).
        let updatedSnapshot: Record<string, unknown> | null = null;

        set((s) => {
          const prev = s.printers[printerId] ?? empty();
          let snapshot = prev.snapshot;
          let lastEvent = prev.lastEvent;
          let eventLog = prev.eventLog;

          if (msg.type === "snapshot") {
            // Bridge sends the §6 translated snapshot at the ROOT:
            // `{printer_id, serial, friendly_name, model, session, phase,
            // headline, job, temps, cooling, lights, ams, …, _raw}`.
            // Store verbatim — it is already the shape `viewOf` reads.
            snapshot = msg.data;
            persist(printerId, snapshot);
          } else if (msg.type === "delta") {
            // Bridge deltas (contract §5.3) are root-level translated leaves,
            // deep-nested, e.g. `{"temps":{"nozzle":{"current_c":254.5}}}`.
            // Deep-merge them into the cached snapshot at the ROOT — there is
            // no `.state` wrapper post-PR-B.
            if (snapshot) {
              snapshot = applyDelta(snapshot, msg.data);
            } else {
              // No prior snapshot yet — the bridge always sends a snapshot
              // before deltas, but if one is lost on reconnect, seed from the
              // delta directly so screens have something coherent to read.
              snapshot = { ...msg.data };
            }
            persist(printerId, snapshot);
          } else if (msg.type === "event") {
            const ev: LiveEvent = {
              name: msg.event,
              data: msg.data,
              at: Date.now(),
            };
            lastEvent = ev;
            eventLog = [ev, ...eventLog].slice(0, RING_SIZE);
          }

          // Capture the updated snapshot reference so we can qaLog below.
          if (msg.type === "snapshot" || msg.type === "delta") {
            updatedSnapshot = snapshot;
          }

          return {
            printers: {
              ...s.printers,
              [printerId]: { ...prev, snapshot, lastEvent, eventLog },
            },
          };
        });

        // QA status snapshot — throttled to at most once per 10 s per printer.
        // Emitted outside the set() updater to keep the updater side-effect free.
        // SECURITY: only extracts translated view-model fields; no tokens/URLs.
        if (updatedSnapshot !== null) {
          const now = Date.now();
          const lastAt = _snapLogAt[printerId] ?? 0;
          if (now - lastAt >= 10_000) {
            _snapLogAt[printerId] = now;
            const snap = updatedSnapshot as Record<string, unknown>;
            const job = (snap.job && typeof snap.job === "object" && !Array.isArray(snap.job))
              ? snap.job as Record<string, unknown>
              : {};
            // stage: emit the text if present, else the id — never a URL.
            const stageBlock = (snap.stage && typeof snap.stage === "object" && !Array.isArray(snap.stage))
              ? snap.stage as Record<string, unknown>
              : null;
            const stageQa = (typeof stageBlock?.text === "string" && stageBlock.text)
              ? stageBlock.text
              : (typeof stageBlock?.id === "number" ? stageBlock.id : null);
            // err: first hms hex, else print_error hex — hex codes are greppable, no message text.
            const hmsArr = Array.isArray(snap.hms) ? snap.hms as Record<string, unknown>[] : [];
            const firstHmsHex = (hmsArr.length > 0 && typeof hmsArr[0]?.hex === "string")
              ? hmsArr[0].hex : null;
            const printErrBlock = (snap.print_error && typeof snap.print_error === "object" && !Array.isArray(snap.print_error))
              ? snap.print_error as Record<string, unknown>
              : null;
            const printErrHex = typeof printErrBlock?.hex === "string" ? printErrBlock.hex : null;
            const errQa = firstHmsHex ?? printErrHex ?? undefined;
            // ctx: job_context code ("no_job"|"printing"|"finishing"|"done").
            // Absent on older servers — only include when it is a valid string
            // to keep the log entry compact (undefined fields are omitted by
            // JSON.stringify, but explicit null would appear). Codes only; no
            // text — the log contract is machine-readable, not human-readable.
            const ctxRaw = snap.job_context;
            const ctx =
              ctxRaw === "no_job" || ctxRaw === "printing" ||
              ctxRaw === "finishing" || ctxRaw === "done"
                ? ctxRaw
                : undefined;
            qaLog("status.snap", {
              state: snap.phase,
              layer: job.layer_num ?? null,
              total_layers: job.total_layer_num ?? null,
              progress: job.percent ?? null,
              remaining_min: job.remaining_min ?? null,
              stage: stageQa ?? undefined,
              ...(errQa !== undefined ? { err: errQa } : {}),
              ...(ctx !== undefined ? { ctx } : {}),
            });
          }
        }
      },
      onStatus(status: LiveStatus, detail?: string) {
        set((s) => {
          const prev = s.printers[printerId] ?? empty();
          return {
            printers: {
              ...s.printers,
              [printerId]: { ...prev, status, statusDetail: detail },
            },
          };
        });
      },
    });
    conn.start();
    set((s) => ({ conns: { ...s.conns, [printerId]: conn } }));
  },

  closeConnection: (printerId: string) => {
    const { conns } = get();
    conns[printerId]?.stop();
    set((s) => {
      const nextConns = { ...s.conns };
      delete nextConns[printerId];
      return { conns: nextConns };
    });
  },

  reset: () => {
    const { conns } = get();
    Object.values(conns).forEach((c) => c.stop());
    set({ conns: {}, printers: {} });
  },
}));
