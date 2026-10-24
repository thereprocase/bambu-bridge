/**
 * Slot ↔ spool view model for the Filament tab and the assign sheet.
 *
 * The bridge reports physical sources (AMS trays and the external holder),
 * Spoolman holds the spool records, and the bridge's assignment table joins
 * them. This module merges the three into one row per source and names the
 * words the screens show, so the tab and the sheet agree on every label.
 */

import { BridgeError, BridgeNetworkError } from "../api/errors";
import type {
  InventoryAssignment,
  InventorySource,
  InventorySpool,
  InventoryStatus,
} from "../api/inventory";

export const EXTERNAL_WIRE = 254;

export interface SlotView {
  source: InventorySource;
  assignment: InventoryAssignment | null;
  /** The assigned spool's record; null when assigned but the record is not in the list. */
  spool: InventorySpool | null;
}

/** One row per physical source, in wire order with the external holder last. */
export function slotViews(
  printerId: string,
  sources: InventorySource[],
  status: InventoryStatus | null,
  spools: InventorySpool[],
): SlotView[] {
  const assignments = status?.assignments ?? [];
  return [...sources]
    .sort((a, b) => a.wire_slot - b.wire_slot)
    .map((source) => {
      const assignment =
        assignments.find((a) => a.printer_id === printerId && a.source === source.source) ??
        null;
      const spool = assignment ? (spools.find((s) => s.id === assignment.spool_id) ?? null) : null;
      return { source, assignment, spool };
    });
}

/** "#12 Bambu PETG Translucent Brown" — the vendor and product after the number. */
export function spoolLabel(spool: InventorySpool): string {
  const name = spool.filament.name ?? spool.filament.material ?? "Filament";
  return `#${spool.id} · ${[spool.filament.vendor?.name ?? "", name].join(" ").trim()}`;
}

/** "640 g left" or "weight unknown". */
export function gramsLeft(spool: InventorySpool | null): string {
  if (!spool || spool.remaining_weight == null) return "weight unknown";
  return `${Math.round(spool.remaining_weight)} g left`;
}

/** The slot's material line: "PETG from the tag", "PETG", or "Empty". */
export function materialLine(source: InventorySource): string {
  if (source.issues.includes("source_empty")) return "Empty";
  const material = source.material.trim() || "Filament";
  return source.tagged ? `${material} from the tag` : material;
}

/** "Slot 3" for a single AMS, "AMS B slot 3" past the first unit, "External spool" otherwise. */
export function slotTitle(source: InventorySource): string {
  if (source.wire_slot === EXTERNAL_WIRE) return "External spool";
  const unit = Math.floor(source.wire_slot / 4);
  const tray = (source.wire_slot % 4) + 1;
  return unit === 0 ? `Slot ${tray}` : `AMS ${String.fromCharCode(65 + unit)} slot ${tray}`;
}

/** The printer reports RRGGBBAA; views want #RRGGBB. Empty when unknown. */
export function sourceColor(source: InventorySource): string | null {
  const hex = source.color.replace(/^#/, "");
  return /^[0-9a-fA-F]{6}/.test(hex) ? `#${hex.slice(0, 6)}` : null;
}

export function sameMaterial(spool: InventorySpool, source: InventorySource): boolean {
  const mine = (spool.filament.material ?? "").trim().toUpperCase();
  return mine !== "" && mine === source.material.trim().toUpperCase();
}

/**
 * Spools the sheet offers for a source: the same material first, then (for an
 * untagged slot only) other materials the bridge can set the slot to. Archived
 * spools and the one already in this slot are left out.
 */
export function candidateSpools(
  source: InventorySource,
  spools: InventorySpool[],
  current: InventoryAssignment | null,
  query = "",
): { matching: InventorySpool[]; others: InventorySpool[] } {
  const term = query.trim().toLowerCase();
  const visible = spools.filter(
    (s) =>
      !s.archived &&
      s.id !== current?.spool_id &&
      (!term || spoolLabel(s).toLowerCase().includes(term)),
  );
  const matching = visible.filter((s) => sameMaterial(s, source));
  const others = source.tagged
    ? []
    : visible.filter((s) => !sameMaterial(s, source) && !!s.filament.material);
  return { matching, others };
}

/**
 * What the owner reads when a call fails. The bridge's own words stand; the
 * one rewrite is an older bridge's bare "Spoolman: <kind>" on a 503, which is
 * Spoolman out of reach.
 */
export function inventoryErrorText(e: unknown): string {
  if (e instanceof BridgeNetworkError) return "Couldn't reach the bridge. Try again.";
  if (e instanceof BridgeError) {
    if (e.status === 503 && /^Spoolman:\s*\S+$/.test(e.envelope.message.trim()))
      return "Spoolman didn't answer. Try again.";
    return e.envelope.message;
  }
  return e instanceof Error ? e.message : String(e);
}

/**
 * The Print tab's warning for the slot about to be used: null when it has a
 * spool (or inventory is off), else the words and the source to assign.
 */
export function unassignedPick(
  pickedWire: number,
  views: SlotView[],
): { text: string; source: InventorySource } | null {
  const view = views.find((v) => v.source.wire_slot === pickedWire);
  if (!view || view.assignment || view.source.issues.includes("source_empty")) return null;
  const where = pickedWire === EXTERNAL_WIRE ? "the external spool" : `slot ${pickedWire + 1}`;
  return { text: `No spool in ${where} · this print would go uncharged`, source: view.source };
}

/** How long a slot row keeps saying the bridge put the spool there from its tag. */
export const TAG_NOTE_MS = 60 * 60 * 1000;

/**
 * "added from its tag" when the bridge created the spool from the RFID tag,
 * "matched by its tag" when it recognised one Spoolman already had; only
 * for the hour after it happened, then the row reads like any other.
 */
export function tagOriginNote(assignment: InventoryAssignment | null, now = Date.now()): string | null {
  if (!assignment?.origin || !assignment.origin.startsWith("rfid")) return null;
  if (now - assignment.assigned_at * 1000 > TAG_NOTE_MS) return null;
  return assignment.origin === "rfid_new" ? "added from its tag" : "matched by its tag";
}

/** "last seen 45 s ago" for a slot the printer hasn't reported lately; null when fresh. */
export function staleLine(source: InventorySource): string | null {
  if (!source.stale && !source.issues.includes("ams_stale")) return null;
  const age = source.age_seconds;
  if (age == null) return "last report is old";
  return age < 90 ? `last seen ${Math.round(age)} s ago` : `last seen ${Math.round(age / 60)} min ago`;
}

const hex6 = (value: string | null | undefined): string | null => {
  const hex = (value ?? "").replace(/^#/, "");
  return /^[0-9a-fA-F]{6}/.test(hex) ? `#${hex.slice(0, 6).toUpperCase()}` : null;
};

/**
 * The swatch for a slot row. Spoolman is the source of truth:
 * an assigned spool's colour fills the swatch; the printer's tray colour,
 * when it differs, shows only as a thin ring (it is often left over from an
 * earlier filament). An unassigned slot keeps the tray colour.
 */
export function swatchFor(slot: SlotView | null, trayColor: string | null | undefined): { fill: string | null; ring: string | null } {
  const tray = hex6(trayColor);
  const spool = slot?.assignment ? hex6(slot.spool?.filament.color_hex) : null;
  if (!spool) return { fill: tray, ring: null };
  return { fill: spool, ring: tray && tray !== spool ? tray : null };
}

/**
 * Filament left as a percent, for the row's bar. An assigned spool reads
 * Spoolman's figure (or remaining over initial or package weight); an
 * unassigned slot keeps the printer's own estimate, which third-party
 * spools don't report.
 */
export function percentFor(slot: SlotView | null, trayPercent: number | null | undefined): number | null {
  const spool = slot?.assignment ? slot.spool : null;
  if (spool) {
    if (spool.remaining_percent != null) return Math.max(0, Math.min(100, spool.remaining_percent));
    const full = spool.initial_weight ?? spool.filament.weight;
    if (spool.remaining_weight != null && full && full > 0) return Math.max(0, Math.min(100, Math.round((spool.remaining_weight / full) * 100)));
    return null;
  }
  return trayPercent ?? null;
}
