/**
 * Filament-memory endpoints — the operator-set human label remembered for an
 * AMS slot, so a tagless spool (no RFID) reads as "Polymaker PolyLite ASA"
 * instead of an anonymous filament-type code.
 *
 * Bridge contract (live):
 *   GET    /printers/{id}/filament-memory
 *            → { slots: { "1": {make,model,profile,tray_type_seen,updated_at}, … } }
 *   PUT    /printers/{id}/filament-memory/{slot}   body {make?,model?,profile?}
 *            → 200 with the stored entry. Each field ≤120 chars; at least one
 *              must be non-empty (the server rejects an all-empty body).
 *   DELETE /printers/{id}/filament-memory/{slot}   → 204
 *
 * The slot's `memory` block in the live snapshot updates immediately after a
 * successful PUT, so the screen does not need to re-fetch to reflect the edit.
 */

import { request } from "./client";

/** Max length per field — mirrors the bridge's ≤120-char validation so the
 * editor can guard before the round-trip rather than surfacing a 422. */
export const FILAMENT_MEMORY_MAX = 120;

/** The label a slot remembers. Any field may be empty, but a stored entry
 * always has at least one non-empty field (the server enforces it on PUT). */
export interface FilamentMemory {
  make: string;
  model: string;
  profile: string;
}

/** One stored entry as the bridge returns it (GET/PUT). The editor only reads
 * make/model/profile; tray_type_seen/updated_at are informational. */
export interface FilamentMemoryEntry extends FilamentMemory {
  tray_type_seen?: string | null;
  updated_at?: string | null;
}

/** GET the full per-slot map. Keys are 1-based physical-slot strings. Not used
 * by the slot editor (it reads `memory` off the live snapshot), but kept for
 * completeness / a future "review all labels" view. */
export function listFilamentMemory(printerId: string) {
  return request<{ slots: Record<string, FilamentMemoryEntry> }>(
    `/printers/${printerId}/filament-memory`,
  );
}

/** PUT a slot's label. `body` may omit fields; send only what the operator
 * filled in. The caller must ensure at least one field is non-empty — the
 * bridge 422s an all-empty body and the editor disables Save for that case. */
export function setFilamentMemory(
  printerId: string,
  slot: number,
  body: Partial<FilamentMemory>,
) {
  return request<FilamentMemoryEntry>(
    `/printers/${printerId}/filament-memory/${slot}`,
    { method: "PUT", body },
  );
}

/** DELETE (forget) a slot's label → 204. The bridge falls the slot back to its
 * live filament-type display. */
export function clearFilamentMemory(printerId: string, slot: number) {
  return request<void>(
    `/printers/${printerId}/filament-memory/${slot}`,
    { method: "DELETE" },
  );
}
