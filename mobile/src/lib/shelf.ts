/**
 * Spools off the AMS, and the question a slot asks when one leaves it.
 *
 * Spoolman holds every spool record and the bridge every assignment; both are
 * read as they stand. Two things are kept on this device only: the spool each
 * slot was last seen holding, so a swap is noticed even when it happened while
 * the app was closed, and the owner's "Back on the shelf" answers. The bridge's
 * own history (last_spool_id) can't serve alone: assigning a spool of the same
 * product to the slot overwrites the record of the one before.
 */

import type { InventorySpool, InventoryStatus } from "../api/inventory";
import { kv } from "./kv";
import type { SlotView } from "./slotInventory";

const KV_SHELVED = "filament.shelved";
const KV_SEEN = "filament.seen";

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

/** Per printer and source: the spool last seen assigned there, and the one that left it unanswered. */
export type SlotMemory = Record<string, { seen: number | null; out: number | null }>;

const slotKey = (printerId: string, source: string) => `${printerId}|${source}`;

/**
 * Compares what each of this printer's slots was last seen holding with its
 * assignment now, then remembers the current one. A spool seen there before
 * and gone now becomes the slot's question; the question is dropped once the
 * spool sits in any slot or is no longer an unarchived spool in Spoolman.
 * `spools` must be the whole unarchived list (everySpool).
 */
export function remember(
  memory: SlotMemory,
  printerId: string,
  status: InventoryStatus,
  spools: InventorySpool[],
): SlotMemory {
  // An older bridge sends no assignments; that must not read as every slot emptied.
  if (!Array.isArray(status.assignments)) return memory;
  const seated = seatedIds(status);
  const shelf = new Set(spools.filter((s) => !s.archived).map((s) => s.id));
  const here = new Map(
    (status.assignments ?? [])
      .filter((a) => a.printer_id === printerId)
      .map((a) => [slotKey(printerId, a.source), a.spool_id] as const),
  );
  const next: SlotMemory = {};
  for (const key of new Set([...Object.keys(memory), ...here.keys()])) {
    const before = memory[key] ?? { seen: null, out: null };
    const mine = key.startsWith(`${printerId}|`);
    // Another printer's slots keep what they saw until that printer is shown.
    const seen = mine ? (here.get(key) ?? null) : before.seen;
    let out = mine && before.seen != null && before.seen !== seen ? before.seen : before.out;
    if (out != null && (seated.has(out) || !shelf.has(out))) out = null;
    if (seen != null || out != null) next[key] = { seen, out };
  }
  return next;
}

/** The slot's question is answered: it stops asking about the spool that left. */
export function answered(memory: SlotMemory, printerId: string, source: string): SlotMemory {
  const key = slotKey(printerId, source);
  const entry = memory[key];
  if (!entry?.out) return memory;
  const next = { ...memory };
  if (entry.seen == null) delete next[key];
  else next[key] = { ...entry, out: null };
  return next;
}

export function readMemory(): SlotMemory {
  try {
    const parsed: unknown = JSON.parse(kv.getString(KV_SEEN) ?? "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const id = (v: unknown) => (typeof v === "number" && Number.isInteger(v) ? v : null);
    const memory: SlotMemory = {};
    for (const [key, value] of Object.entries(parsed as Record<string, { seen?: unknown; out?: unknown }>)) {
      if (value && typeof value === "object") memory[key] = { seen: id(value.seen), out: id(value.out) };
    }
    return memory;
  } catch {
    return {};
  }
}

/** False when it couldn't be saved; the app then compares against the last saved memory next time. */
export function writeMemory(memory: SlotMemory): boolean {
  try {
    kv.set(KV_SEEN, JSON.stringify(memory));
    return true;
  } catch {
    return false;
  }
}

/**
 * The spool that last sat in this slot when it has gone and nobody has said
 * where: not in this slot, not in any other, not archived, and not answered
 * "Back on the shelf" here. Null otherwise. The slot memory names it first;
 * the bridge's last_spool_id stands in for a slot this device never saw. A
 * spool missing from the list (archived, or gone from Spoolman) is never asked about.
 */
export function cameOutOf(
  view: SlotView,
  spools: InventorySpool[],
  status: InventoryStatus | null,
  shelved: ReadonlySet<string>,
  memory: SlotMemory = {},
): InventorySpool | null {
  const id = memory[slotKey(view.source.printer_id, view.source.source)]?.out ?? view.source.last_spool_id;
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
