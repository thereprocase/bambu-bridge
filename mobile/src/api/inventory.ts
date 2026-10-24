import { request } from "./client";
import { BridgeError } from "./errors";

export interface InventoryVendor {
  id: number;
  name: string;
  empty_spool_weight: number | null;
}
export interface InventoryProduct {
  id: number;
  name: string | null;
  vendor: InventoryVendor | null;
  material: string | null;
  color_hex: string | null;
  weight: number | null;
  spool_weight: number | null;
  density: number | null;
  diameter: number | null;
}
export interface InventorySpool {
  id: number;
  filament: InventoryProduct;
  remaining_weight: number | null;
  initial_weight: number | null;
  spool_weight: number | null;
  archived: boolean;
  remaining_percent?: number | null;
  weight_basis?: string;
  weight_observed_at?: number | null;
  over_capacity?: boolean;
  orca_material?: OrcaMaterialSelection;
}
export interface InventoryAssignment {
  printer_id: string;
  source: string;
  spool_id: number;
  assigned_at: number;
  observed_identity: string | null;
  wire_slot: number | null;
  /** Who put the spool here: the owner, or the bridge from the slot's RFID tag. Absent on older bridges. */
  origin?: "owner" | "rfid_new" | "rfid_known" | string | null;
}
export interface InventoryStatus {
  configured: boolean;
  api_version: number;
  provider_id?: string;
  revision?: number;
  assignments?: InventoryAssignment[];
  accounting_mode: string;
  native_overlay_enabled: boolean;
  /** Prints waiting for the owner (delivery_state review). Absent on older bridges. */
  review_count?: number;
  /** Spoolman's web address for an "Open Spoolman" link; null when the operator set none. */
  spoolman_web_url?: string | null;
}
export interface InventorySource {
  printer_id: string;
  source: string;
  wire_slot: number;
  label: string;
  material: string;
  color: string;
  observed_identity: string;
  observed_at: number;
  assignable: boolean;
  issues: string[];
  /** An RFID spool: its tag sets the slot's material. Absent on older bridges. */
  tagged?: boolean;
  /** The printer hasn't sent a full AMS report lately; this is the last known slot. Absent on older bridges. */
  stale?: boolean;
  age_seconds?: number | null;
  /** The AMS is still reading this tray's RFID tag; the bridge seats the spool once it settles. Absent on older bridges. */
  tag_reading?: boolean;
  /** The AMS finished reading the tray's tag; null when the firmware doesn't say. The bridge's gate, shown nowhere. */
  tag_read_done?: boolean | null;
  /** What the tray's RFID tag says, for a tagged tray; null otherwise. Absent on older bridges. */
  tag?: { brand: string | null; material: string | null; color: string | null; weight_g: number | null; remain_percent: number | null } | null;
  /** The spool that last sat behind this slot's current identity, from the bridge's assignment history. Absent on older bridges. */
  last_spool_id?: number | null;
}
export const inventoryStatus = () => request<InventoryStatus>("/inventory");
export const inventorySpool = (id: number) =>
  request<InventorySpool>(`/inventory/spools/${id}`);
export const inventorySpools = (query = "", offset = 0, material?: string, limit = 50) =>
  request<InventorySpool[]>("/inventory/spools", {
    query: { query, offset, limit, ...(material ? { material } : {}) },
  });
export const inventoryProducts = (query = "", offset = 0) =>
  request<InventoryProduct[]>("/inventory/products", {
    query: { query, offset, limit: 50 },
  });
export const inventoryVendors = (query = "", offset = 0) =>
  request<InventoryVendor[]>("/inventory/vendors", {
    query: { query, offset, limit: 50 },
  });
export const inventorySources = (printer: string) =>
  request<InventorySource[]>(
    `/inventory/printers/${encodeURIComponent(printer)}/sources`,
  );
export const inventoryAssign = (
  printer: string,
  body: {
    source: string;
    spool_id: number | null;
    observed_identity?: string;
    /** Set the slot on the printer to the spool's material first. */
    set_slot?: boolean;
  },
) =>
  request<{
    provider_id: string;
    revision: number;
    assignments: InventoryAssignment[];
  }>(`/inventory/printers/${encodeURIComponent(printer)}/assignment`, {
    method: "PUT",
    body,
  });


/* Spoolman record shim: the bridge forwards these to Spoolman and answers with the record. */
export const createVendor = (body: { name: string; empty_spool_weight?: number }) =>
  request<InventoryVendor>("/inventory/vendors", { method: "POST", body, timeoutMs: 30_000 });
export const createProduct = (body: {
  name: string;
  vendor_id: number;
  material: string;
  color_hex: string;
  /** Spoolman needs a density and the bridge a package weight for every product. */
  density: number;
  weight: number;
  spool_weight?: number;
  diameter?: number;
  /** Spoolman's catalog id when the product was picked from it. */
  external_id?: string;
}) => request<InventoryProduct>("/inventory/products", { method: "POST", body, timeoutMs: 30_000 });
/** One filament of Spoolman's built-in catalog, for a product Spoolman doesn't have yet. */
export interface CatalogEntry {
  id: string;
  manufacturer: string | null;
  name: string | null;
  material: string | null;
  density: number | null;
  weight: number | null;
  spool_weight: number | null;
  diameter: number | null;
  color_hex: string | null;
}
export const inventoryCatalog = (query: string, limit = 30) =>
  request<CatalogEntry[]>("/inventory/catalog", { query: { query, limit } });
export const createSpool = (body: { filament_id: number; remaining_weight?: number }) =>
  request<InventorySpool>("/inventory/spools", { method: "POST", body, timeoutMs: 30_000 });
/** A scale reading with the spool on it (gross) or the filament alone (remaining). */
export const weighSpool = (id: number, body: { gross_weight: number } | { remaining_weight: number }) =>
  request<InventorySpool>(`/inventory/spools/${id}/weigh`, { method: "POST", body, timeoutMs: 30_000 });
/** What the bridge offers after archiving with a measured bare-spool weight that differs from the vendor's. */
export interface VendorOffer {
  vendor_id: number;
  vendor: string;
  current_empty_spool_weight: number | null;
  proposed_empty_spool_weight: number;
}
/** Archive a spool; with `empty_spool_weight` the bare spool's measured weight is stored on it too. */
export const archiveSpool = (id: number, body?: { empty_spool_weight: number }) =>
  request<InventorySpool & { vendor_offer?: VendorOffer }>(`/inventory/spools/${id}/archive`, {
    method: "POST",
    ...(body ? { body } : {}),
    timeoutMs: 30_000,
  });
export const setVendorEmptyWeight = (vendorId: number, empty_spool_weight: number) =>
  request<InventoryVendor>(`/inventory/vendors/${vendorId}/empty_spool_weight`, {
    method: "POST",
    body: { empty_spool_weight },
    timeoutMs: 30_000,
  });

/** The bridge has Spoolman and speaks this app's inventory API. */
export function inventoryReady(status: InventoryStatus | null): boolean {
  return status?.configured === true && status.api_version === 2;
}

/** What a finished print could not charge, one entry per slot, spool or filament. */
export interface Shortfall {
  kind:
    | "unassigned_slot"
    | "unmapped_filament"
    | "unweighed_filament"
    | "spool_archived"
    | "spool_missing"
    | "spool_empty"
    | "delivery_error"
    | "no_layer_plan"
    | string;
  wire_slot: number | null;
  spool_id: number | null;
  grams: number;
  material: string | null;
}
export interface Charge {
  spool_id: number;
  grams: number;
  /** The layers this spool fed, [first, last], when a refill split the print. */
  layers?: [number, number] | null;
}
/** One print on the accounting page: what was charged and what still waits. */
export interface AccountingRow {
  attempt_id: string;
  printer_id: string;
  title: string;
  created_at: number | null;
  ended_at: number | null;
  capture_id: string | null;
  start_state: string | null;
  outcome: string | null;
  delivery_state: "shadow" | "delivered" | "review" | null;
  charged: Charge[];
  shortfalls: Shortfall[];
  review_reason: string | null;
  planned_grams: number | null;
  /** How the owner closed the print; null when the ledger closed it on its own. */
  resolution?: { action: "charge" | "nothing" | "charged_elsewhere" | string; resolved_at: number } | null;
  /** Layers in the print, when the bridge counted them. */
  total_layers?: number | null;
}
export interface AccountingPage {
  attempts: AccountingRow[];
}
export const inventoryAccounting = (
  offset = 0,
  delivery_state?: "shadow" | "delivered" | "review",
  limit = 50,
) =>
  request<AccountingPage>("/inventory/accounting", {
    query: { offset, limit, ...(delivery_state ? { delivery_state } : {}) },
  });

/** A charge moved from one spool to another by the owner ("that benchy came from #12"). */
export interface Move {
  from_spool_id: number;
  to_spool_id: number;
  grams: number;
  state: "started" | "credited" | "done" | string;
  started_at: number;
  finished_at: number | null;
}
export interface HeldPrint extends AccountingRow {
  moves?: Move[];
  layer_reached: number | null;
  suggested: { spool_id: number | null; grams: number }[];
  written: { spool_id: number; written_g: number; state: string; reason: string | null }[];
  candidates: { spool_id: number; wire_slot: number }[];
  resolution: { action: string; charges: unknown[]; resolved_at: number } | null;
}
/** Move a print's grams from one spool to another; grams defaults to everything on from_spool. */
export const reassignCharge = (
  attemptId: string,
  body: { from_spool_id: number; to_spool_id: number; grams?: number },
) =>
  request<HeldPrint>(`/inventory/accounting/${encodeURIComponent(attemptId)}/reassign`, {
    method: "POST",
    body,
    timeoutMs: 60_000,
  });
/** The prints a spool fed, newest first. */
export interface SpoolPrint {
  attempt_id?: string;
  title: string;
  ended_at: number | null;
  capture_id: string | null;
  layers: [number, number] | null;
  total_layers: number | null;
  grams: number;
  share_pct: number | null;
}
export const spoolHistory = async (spoolId: number, limit = 20) =>
  (await request<{ spool_id: number; prints: SpoolPrint[] }>(`/inventory/spools/${spoolId}/history`, { query: { limit } })).prints;
export const heldPrint = (attemptId: string) =>
  request<HeldPrint>(`/inventory/accounting/${encodeURIComponent(attemptId)}`);
/** A charge names a spool and grams, or a slot and the spool that was in it (the slot's counted grams). */
export type ResolveCharge = { spool_id: number; grams: number } | { spool_id: number; slot: number };
export const resolveHeldPrint = (
  attemptId: string,
  body: { action: "charge" | "nothing" | "charged_elsewhere"; charges: ResolveCharge[] },
) =>
  request<HeldPrint>(`/inventory/accounting/${encodeURIComponent(attemptId)}/resolve`, {
    method: "POST",
    body,
    timeoutMs: 60_000,
  });

export interface StockMaterial {
  key: string;
  name: string;
  material_id: string;
  material: string;
  resource_sha256: string;
  inheritance_sha256: string;
}
export interface StockCatalogue {
  upstream_tag: string;
  catalogue_digest: string;
  runtime_qualified: boolean;
  profiles: StockMaterial[];
}
export interface MaterialMappings {
  provider_id: string;
  revision: number;
  mappings: {
    product_id: number;
    stock_key: string;
    catalogue_digest: string;
    product_fingerprint: string;
  }[];
}
export interface OrcaMaterialSelection {
  selection: {
    key: string;
    name: string;
    material_id: string;
    material: string;
    source_verified: boolean;
    runtime_qualified: boolean;
  } | null;
  issue: string | null;
}
export const stockMaterials = (material: string) =>
  request<StockCatalogue>("/inventory/stock-materials", {
    query: { material },
  });
export const materialMappings = () =>
  request<MaterialMappings>("/inventory/material-mappings");
export const selectStockMaterial = (
  productId: number,
  body: {
    provider_id: string;
    expected_revision: number;
    stock_key: string | null;
    catalogue_digest?: string;
  },
) =>
  request<MaterialMappings>(`/inventory/material-mappings/${productId}`, {
    method: "PUT",
    body,
  });

export interface DisplayPolicy {
  provider_id: string;
  revision: number;
  global_enabled: boolean;
  printers: string[];
}
export interface InventoryDisplayStatus extends DisplayPolicy {
  runtime_qualified: boolean;
  effective_enabled: boolean;
  reason: string;
  projected_slots: number[];
}
export const inventoryDisplay = () =>
  request<InventoryDisplayStatus>("/inventory/display");
export const saveInventoryDisplay = (body: {
  provider_id: string;
  expected_revision: number;
  global_enabled: boolean;
  printers: string[];
}) => request<DisplayPolicy>("/inventory/display", { method: "PUT", body });

/** Filament counted so far by the print running now, per slot, against what Spoolman was sent. */
export interface LiveUsageSlot {
  wire_slot: number;
  spool_id: number | null;
  used_g: number;
  written_g: number;
  state: string | null;
}
export interface LiveUsage {
  printing: boolean;
  attempt_id?: string;
  layer?: number | null;
  total_layers?: number | null;
  planned_grams?: number | null;
  slots?: LiveUsageSlot[];
}
export const inventoryLiveUsage = (printer: string) =>
  request<LiveUsage>(`/inventory/printers/${encodeURIComponent(printer)}/usage`);

/* Backup spool: when one AMS slot runs out, the printer continues from another (cross-colour refill). */
export interface Backup {
  from_wire: number;
  to_wire: number;
  state: "active" | "pending" | "needs_attention" | "restoring" | string;
  /** What the printer reports when the backup needs the owner; null otherwise. */
  message: string | null;
  /** Newer bridges: pending only because the printer has gone quiet since it last confirmed. */
  stale?: boolean;
  /** Wall-clock epoch seconds of the last fresh active report; null after a restart or contrary evidence. */
  confirmed_at?: number | null;
  last_confirmed_age_seconds?: number | null;
}
/** One slot of the group as the bridge stores it; `before` is what removing the backup puts back. */
export interface RefillStep {
  wire: number;
  state: string;
  before: Record<string, unknown>;
  target: Record<string, unknown>;
}
export interface RefillStatus {
  backups: Backup[];
  printer_auto_refill: boolean | null;
  printer_connected: boolean;
  /** Present on bridges since 2026-10-22; the sheet lists what the backup changed from it. */
  steps?: RefillStep[];
  /** Whether the printer's auto-refill flag and backup masks were reported in the last 30 s. */
  eligibility_fresh?: boolean;
  eligibility_age_seconds?: number | null;
  /** The printer's last word, however old; `printer_auto_refill` is null once it is stale. */
  last_reported_auto_refill?: boolean | null;
  reported_backup_masks?: number[] | null;
}
export const refillStatus = (printer: string) =>
  request<RefillStatus>(`/inventory/printers/${encodeURIComponent(printer)}/refill`);
/** Applies at once while the printer is idle; the bridge reads every slot back inside this call. */
/** `preview_digest` only from the backup preview route; the bridge then refuses (409) if the slots changed since. */
export const putBackup = (printer: string, body: { from_wire: number; to_wire: number; preview_digest?: string }) =>
  request<RefillStatus>(`/inventory/printers/${encodeURIComponent(printer)}/refill/backup`, {
    method: "PUT",
    body,
    timeoutMs: 90_000,
  });
export const deleteBackup = (printer: string, fromWire: number) =>
  request<RefillStatus>(`/inventory/printers/${encodeURIComponent(printer)}/refill/backup/${fromWire}`, {
    method: "DELETE",
    timeoutMs: 90_000,
  });

/**
 * What setting this backup would write to each slot, without writing it.
 *
 * Bridges with whole-filament refill answer on the backup route with a
 * per-field list. Older ones only have the general preview, which takes the
 * colour the group will share: the primary slot's, as the backup PUT uses.
 * Read either with previewSteps(); a 422 carries the bridge's reason.
 */
export async function previewBackup(
  printer: string,
  body: { from_wire: number; to_wire: number; from_color: string },
): Promise<{ body: unknown; digest: string | null }> {
  const base = `/inventory/printers/${encodeURIComponent(printer)}/refill`;
  try {
    const answer = await request<unknown>(`${base}/backup/preview`, {
      method: "POST",
      body: { from_wire: body.from_wire, to_wire: body.to_wire },
    });
    const digest = (answer as { preview_digest?: unknown } | null)?.preview_digest;
    return { body: answer, digest: typeof digest === "string" && /^[0-9a-f]{64}$/.test(digest) ? digest : null };
  } catch (e) {
    if (!(e instanceof BridgeError) || (e.status !== 404 && e.status !== 405)) throw e;
  }
  // The general preview's digest belongs to its own plan route; an older
  // backup PUT forbids unknown fields, so it is never passed on.
  const answer = await request<unknown>(`${base}/preview`, {
    method: "POST",
    body: { wires: [body.from_wire, body.to_wire], color: body.from_color },
  });
  return { body: answer, digest: null };
}
