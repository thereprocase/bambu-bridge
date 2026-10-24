/**
 * Words for the Status tab's filament line while a print runs.
 *
 * The bridge counts grams per slot from the G-code layer plan and sends
 * Spoolman the difference as the print goes. This turns that report into
 * one headline and, for a multi-spool print, one line per slot.
 */

import type { InventorySpool, LiveUsage } from "../api/inventory";
import { EXTERNAL_WIRE, spoolLabel } from "./slotInventory";

export interface UsageLines {
  headline: string;
  /** One per slot; empty for a single-slot print (the headline says it all). */
  slots: { text: string; unassigned: boolean }[];
  /** "Sent to Spoolman: 19.3 g", or null when nothing has been sent. */
  sent: string | null;
}

// Whole grams: the owner wants nothing finer.
const g = (value: number) => `${Math.round(value)} g`;

export function usageLines(usage: LiveUsage, spools: InventorySpool[]): UsageLines | null {
  if (!usage.printing) return null;
  const slots = usage.slots ?? [];
  const used = slots.reduce((sum, s) => sum + s.used_g, 0);
  const written = slots.reduce((sum, s) => sum + s.written_g, 0);
  const planned = usage.planned_grams;
  const headline =
    planned != null && planned > 0
      ? `Filament · ${g(used)} used · ${g(Math.max(0, planned - used))} to go`
      : `Filament · ${g(used)} used`;
  const lines =
    slots.length > 1 || slots.some((s) => s.spool_id == null)
      ? slots.map((s) => {
          const where = s.wire_slot === EXTERNAL_WIRE ? "External" : `Slot ${s.wire_slot + 1}`;
          const spool = s.spool_id == null ? null : spools.find((r) => r.id === s.spool_id);
          const who = s.spool_id == null ? "no spool assigned" : spool ? spoolLabel(spool) : `#${s.spool_id}`;
          return { text: `${where} · ${who} · ${g(s.used_g)}`, unassigned: s.spool_id == null };
        })
      : [];
  return { headline, slots: lines, sent: written > 0 ? `Sent to Spoolman: ${g(written)}` : null };
}
