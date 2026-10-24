/**
 * Spools off the AMS, and the question a slot asks when one leaves it.
 *
 * Spoolman holds every spool record and the bridge every assignment; both are
 * read as they stand. The one thing kept on this device is the owner's "Back
 * on the shelf" answers, so a slot stops asking about a spool it was told about.
 */

import type { InventorySpool, InventoryStatus } from "../api/inventory";
import { kv } from "./kv";
import type { SlotView } from "./slotInventory";

const KV_SHELVED = "filament.shelved";

// The route's largest page, and the bridge's own cap of 1000 spools read from Spoolman.
const SPOOL_PAGE = 100;
const SPOOL_PAGES = 10;

/**
 * Every unarchived spool, page by page until a short page. Off the AMS and the
 * slot question each need the whole shelf; one page would leave spools out.
 */
export async function everySpool(page: (offset: number, limit: number) => Promise<InventorySpool[]>): Promise<InventorySpool[]> {
  const all: InventorySpool[] = [];
  for (let n = 0; n < SPOOL_PAGES; n++) {
    const next = await page(all.length, SPOOL_PAGE);
    all.push(...next);
    if (next.length < SPOOL_PAGE) break;
  }
  return all;
}

function seatedIds(status: InventoryStatus | null): Set<number> {
  return new Set((status?.assignments ?? []).map((a) => a.spool_id));
}

/**
 * Unarchived spools in no slot on any printer, in the bridge's order (most
 * recently assigned first, so the one just taken out leads).
 */
export function offAmsSpools(spools: InventorySpool[], status: InventoryStatus | null): InventorySpool[] {
  const seated = seatedIds(status);
  return spools.filter((s) => !s.archived && !seated.has(s.id));
}

export function shelfKey(printerId: string, source: string, spoolId: number): string {
  return `${printerId}|${source}|${spoolId}`;
}

/**
 * The spool that last sat in this slot when it has gone and nobody has said
 * where: not in this slot, not in any other, not archived, and not answered
 * "Back on the shelf" here. Null otherwise. A spool missing from the list
 * (archived, or gone from Spoolman) is never asked about.
 */
export function cameOutOf(
  view: SlotView,
  spools: InventorySpool[],
  status: InventoryStatus | null,
  shelved: ReadonlySet<string>,
): InventorySpool | null {
  const id = view.source.last_spool_id;
  if (id == null || view.assignment?.spool_id === id || seatedIds(status).has(id)) return null;
  const spool = spools.find((s) => s.id === id && !s.archived);
  if (!spool) return null;
  return shelved.has(shelfKey(view.source.printer_id, view.source.source, id)) ? null : spool;
}

export function readShelved(): Set<string> {
  try {
    const parsed: unknown = JSON.parse(kv.getString(KV_SHELVED) ?? "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === "string") : []);
  } catch {
    return new Set();
  }
}

/** False when the answer couldn't be saved; it then holds until the app restarts. */
export function writeShelved(keys: ReadonlySet<string>): boolean {
  try {
    kv.set(KV_SHELVED, JSON.stringify([...keys]));
    return true;
  } catch {
    return false;
  }
}

/**
 * Drops the answers for spools that are back in a slot, so when one comes out
 * again its slot asks again. This also keeps the stored list short.
 */
export function pruneShelved(shelved: ReadonlySet<string>, status: InventoryStatus | null): Set<string> {
  const seated = seatedIds(status);
  return new Set([...shelved].filter((key) => !seated.has(Number(key.slice(key.lastIndexOf("|") + 1)))));
}

/** The spool's Spoolman colour as #RRGGBB; null when it has none. */
export function spoolColor(spool: InventorySpool): string | null {
  const hex = (spool.filament.color_hex ?? "").replace(/^#/, "");
  return /^[0-9a-fA-F]{6}/.test(hex) ? `#${hex.slice(0, 6).toUpperCase()}` : null;
}
