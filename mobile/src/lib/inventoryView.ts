/**
 * The Inventory tab's view of every Spoolman spool: where each one is, what
 * the find bar can narrow to, how the list sorts and groups, and the summary
 * at the top. Spoolman's records and the bridge's assignments are read as
 * they stand; nothing here is kept.
 */

import type { InventorySpool, InventoryStatus } from "../api/inventory";
import { LOW_PERCENT, storedPercent } from "./shelf";
import { slotTitle, type SlotView } from "./slotInventory";

export type SortKey = "material" | "colour" | "left" | "recent";
export type Where = "all" | "printer" | "shelf";

export interface InventoryFilter {
  query: string;
  /** Upper-case material, or null for every material. */
  material: string | null;
  vendor: string | null;
  where: Where;
}

export const NO_FILTER: InventoryFilter = { query: "", material: null, vendor: null, where: "all" };

export interface ChipOption {
  id: string;
  label: string;
  count: number;
}

const UNGROUPED = "Other";

export function materialOf(spool: InventorySpool): string {
  return (spool.filament.material ?? "").trim().toUpperCase() || UNGROUPED;
}

export function vendorOf(spool: InventorySpool): string {
  return (spool.filament.vendor?.name ?? "").trim() || "No vendor";
}

/**
 * Where the spool sits: "Slot 2", "External spool", or null when it is off
 * the printer. Assignments on another printer than the one shown read as
 * "On <printer>", so a spool is never listed as stored while it is in use.
 */
export function placeOf(spool: InventorySpool, status: InventoryStatus | null, views: SlotView[], printerId: string | null): string | null {
  const view = views.find((v) => v.assignment?.spool_id === spool.id);
  if (view) return slotTitle(view.source);
  const elsewhere = (status?.assignments ?? []).find((a) => a.spool_id === spool.id && a.printer_id !== printerId);
  return elsewhere ? `On ${elsewhere.printer_id}` : null;
}

/** The chips for one facet, most spools first, then by name. */
function options(spools: InventorySpool[], key: (s: InventorySpool) => string): ChipOption[] {
  const counts = new Map<string, number>();
  for (const spool of spools) counts.set(key(spool), (counts.get(key(spool)) ?? 0) + 1);
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([id, count]) => ({ id, label: id, count }));
}

export const materialOptions = (spools: InventorySpool[]) => options(spools, materialOf);
export const vendorOptions = (spools: InventorySpool[]) => options(spools, vendorOf);

/** What the search box matches: number, vendor, name, material, colour hex, lot, SKU, location. */
function haystack(spool: InventorySpool): string {
  return [
    `#${spool.id}`,
    String(spool.id),
    spool.filament.vendor?.name,
    spool.filament.name,
    spool.filament.material,
    spool.filament.color_hex,
    spool.filament.article_number,
    spool.lot_nr,
    spool.location,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

/** The spools the find bar leaves. Every word of the query must match somewhere. */
export function filterSpools(
  spools: InventorySpool[],
  filter: InventoryFilter,
  place: (spool: InventorySpool) => string | null,
): InventorySpool[] {
  const words = filter.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return spools.filter((spool) => {
    if (filter.material && materialOf(spool) !== filter.material) return false;
    if (filter.vendor && vendorOf(spool) !== filter.vendor) return false;
    if (filter.where === "printer" && !place(spool)) return false;
    if (filter.where === "shelf" && place(spool)) return false;
    if (words.length === 0) return true;
    const text = haystack(spool);
    return words.every((w) => text.includes(w));
  });
}

/** Hue (reds first) for sorting by colour; greys sort by lightness after every hue, unknown last. */
export function colourKey(hex: string | null | undefined): number {
  const clean = (hex ?? "").replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}/.test(clean)) return 1000;
  const r = parseInt(clean.slice(0, 2), 16) / 255;
  const g = parseInt(clean.slice(2, 4), 16) / 255;
  const b = parseInt(clean.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  const lightness = (max + min) / 2;
  // Below this saturation the eye reads grey; order those dark to light after the colours.
  if (d < 0.08) return 360 + lightness * 100;
  let h: number;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  if (h < 0) h += 360;
  // Reds sit on both sides of 0°; turning the wheel 20° keeps them together at the start.
  return (h + 20) % 360;
}

const product = (s: InventorySpool) => `${s.filament.vendor?.name ?? ""} ${s.filament.name ?? ""}`.trim().toLowerCase();
const grams = (s: InventorySpool) => (s.remaining_weight == null ? Number.POSITIVE_INFINITY : s.remaining_weight);
const used = (s: InventorySpool) => (s.last_used ? Date.parse(s.last_used) : Number.NEGATIVE_INFINITY);

/**
 * The list order for a sort key. Material: by material, then product, then
 * fullest first (as the shelf does). Colour: around the hue circle, greys
 * last. Left: least filament first, unknown weight last. Recent: last used
 * newest first; spools never used, or from a bridge that doesn't say, keep
 * the bridge's own order (most recently assigned first) after those.
 */
export function sortSpools(spools: InventorySpool[], key: SortKey): InventorySpool[] {
  const indexed = spools.map((spool, index) => ({ spool, index }));
  const by = (cmp: (a: InventorySpool, b: InventorySpool) => number) =>
    indexed.sort((a, b) => cmp(a.spool, b.spool) || a.index - b.index).map((e) => e.spool);
  switch (key) {
    case "material":
      return by((a, b) => {
        const ma = materialOf(a), mb = materialOf(b);
        if (ma !== mb) return ma === UNGROUPED ? 1 : mb === UNGROUPED ? -1 : ma.localeCompare(mb);
        return product(a).localeCompare(product(b)) || grams(b) - grams(a) || a.id - b.id;
      });
    case "colour":
      return by((a, b) => colourKey(a.filament.color_hex) - colourKey(b.filament.color_hex) || grams(b) - grams(a));
    case "left":
      return by((a, b) => grams(a) - grams(b) || a.id - b.id);
    case "recent":
      return by((a, b) => used(b) - used(a));
  }
}

export interface Section {
  title: string | null;
  spools: InventorySpool[];
}

/** Material sort shows captions per material; the other sorts are one flat run. */
export function sectionsFor(sorted: InventorySpool[], key: SortKey): Section[] {
  if (key !== "material") return sorted.length ? [{ title: null, spools: sorted }] : [];
  const sections: Section[] = [];
  for (const spool of sorted) {
    const title = materialOf(spool);
    const last = sections[sections.length - 1];
    if (last && last.title === title) last.spools.push(spool);
    else sections.push({ title, spools: [spool] });
  }
  return sections;
}

export interface MaterialShare {
  material: string;
  grams: number;
  /** 0–1 of the total; the summary bar's segment width. */
  share: number;
}

export interface InventorySummary {
  count: number;
  /** Sum of remaining weight over spools that have one. */
  grams: number;
  /** Spools without a remaining weight; the total is marked "+ n unweighed" when > 0. */
  unweighed: number;
  byMaterial: MaterialShare[];
  /** Under LOW_PERCENT, least first. */
  low: InventorySpool[];
}

/** The card at the top: active (unarchived) spools only. */
export function inventorySummary(spools: InventorySpool[]): InventorySummary {
  const active = spools.filter((s) => !s.archived);
  const perMaterial = new Map<string, number>();
  let total = 0;
  let unweighed = 0;
  for (const spool of active) {
    if (spool.remaining_weight == null) { unweighed++; continue; }
    total += spool.remaining_weight;
    perMaterial.set(materialOf(spool), (perMaterial.get(materialOf(spool)) ?? 0) + spool.remaining_weight);
  }
  const byMaterial = [...perMaterial.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([material, g]) => ({ material, grams: g, share: total > 0 ? g / total : 0 }));
  const low = active
    .filter((s) => { const p = storedPercent(s); return p != null && p < LOW_PERCENT; })
    .sort((a, b) => grams(a) - grams(b));
  return { count: active.length, grams: total, unweighed, byMaterial, low };
}

/** "6.3 kg" past a kilogram, "845 g" under it. */
export function weightText(g: number): string {
  if (g >= 1000) {
    const kg = g / 1000;
    return `${kg >= 10 ? Math.round(kg) : Math.round(kg * 10) / 10} kg`;
  }
  return `${Math.round(g)} g`;
}

/** "154 g of 1,000 g", "154 g left" when the package weight is unknown, "weight unknown". */
export function fillText(spool: InventorySpool): string {
  if (spool.remaining_weight == null) return "weight unknown";
  const left = Math.round(spool.remaining_weight);
  const full = spool.initial_weight ?? spool.filament.weight;
  return full && full > 0 ? `${left.toLocaleString("en-US")} g of ${Math.round(full).toLocaleString("en-US")} g` : `${left.toLocaleString("en-US")} g left`;
}

/** "Oct 25" this year, "Oct 25, 2025" otherwise; null for nothing or junk. */
export function dateText(iso: string | null | undefined, now = new Date()): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const sameYear = at.getFullYear() === now.getFullYear();
  return at.toLocaleDateString("en-US", sameYear ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "numeric" });
}
