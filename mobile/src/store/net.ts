/**
 * Network reachability store — coarse signal for the UI.
 *
 * "reach" reflects the last known outcome of HTTP/WS traffic to the bridge:
 *   "unknown"  — app just launched, no requests completed yet
 *   "lan"      — last successful traffic was via the LAN path
 *   "remote"   — last successful traffic was via remote/Tailscale
 *   "down"     — both paths attempted and both failed; user needs to act
 *
 * The store only transitions on explicit `setReach()` calls from client.ts
 * and ws/live.ts — not from a timer or a passive poll — so it reflects real
 * request outcomes, not a stale probe. Any success (HTTP or WS) clears a
 * "down" state; "down" is only set when both fallback paths have been
 * exhausted in a single request cycle.
 *
 * The `since` field is the epoch ms of the last transition, so the UI can
 * decide whether a "down" badge is worth showing yet (e.g. ignore a sub-2s
 * blip on reconnect). Currently unused in the banner but available for
 * future polish.
 */

import { create } from "zustand";

import { qaLog } from "../lib/qalog";

export type Reach = "unknown" | "lan" | "remote" | "down";

interface NetStore {
  reach: Reach;
  since: number; // epoch ms of last transition

  setReach(r: Reach): void;
}

export const useNetStore = create<NetStore>((set, get) => ({
  reach: "unknown",
  since: 0,

  setReach: (r) => {
    if (get().reach === r) return; // no-op on same value
    qaLog("net.reach", { reach: r });
    set({ reach: r, since: Date.now() });
  },
}));
