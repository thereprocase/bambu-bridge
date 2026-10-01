/**
 * Skip Objects — OrcaSlicer 2.4.2 PartSkipDialog rules, shared by the screen
 * and its tests. Mirrors the web dashboard's static/app/skip-objects.js.
 *
 * The bridge sends the plate's pick map as run-length rows of identify_ids.
 * A tap hits the id at that pixel, as SkipPartCanvas::GetIdAtViewPt reads
 * Orca's pick image, if the id is an object of the plate.
 */

export type PartState = "unchecked" | "checked" | "skipped";

export interface SkipMap {
  width: number;
  height: number;
  /** Per image row: [id, count, id, count, ...]; 0 = no object. */
  rows: number[][];
}

export interface SkipObjectsInfo {
  job: string | null;
  plate: number;
  label_object_enabled: boolean;
  max_objects: number;
  objects: { id: number; name: string; skipped: boolean }[];
  map: SkipMap | null;
  available: boolean;
  reason: string | null;
}

export const MAX_OBJECTS = 64;

/** identify_id at image pixel (ix, iy), or 0. */
export function idAt(map: SkipMap | null, ix: number, iy: number): number {
  if (!map || ix < 0 || iy < 0 || ix >= map.width || iy >= map.height) return 0;
  const row = map.rows[iy] ?? [];
  let x = 0;
  for (let i = 0; i < row.length; i += 2) {
    x += row[i + 1];
    if (ix < x) return row[i];
  }
  return 0;
}

/** The object under a tap at (x, y) in a view of width x height showing the whole map. */
export function hitTest(
  map: SkipMap | null, objectIds: Set<number>, x: number, y: number, width: number, height: number,
): number {
  if (!map || !(width > 0) || !(height > 0)) return 0;
  const id = idAt(map, Math.floor((x * map.width) / width), Math.floor((y * map.height) / height));
  return objectIds.has(id) ? id : 0;
}

/** Orca's Skip-button refusal (tooltip) for this selection, or "" when it may apply. */
export function applyRefusal(info: SkipObjectsInfo, states: Map<number, PartState>): string {
  const checked = [...states.values()].filter((s) => s === "checked").length;
  if (!checked) return "Nothing selected";
  if (states.size > MAX_OBJECTS) return "Over 64 objects in single plate";
  if (!info.label_object_enabled) return "The current print job cannot be skipped";
  return info.available ? "" : (info.reason || "Skipping objects is unavailable");
}

/** PartSkipConfirmDialog text: skipping everything stops the print. */
export function confirmText(states: Map<number, PartState>): { title: string; body: string; all: boolean } {
  const n = [...states.values()].filter((s) => s === "checked").length;
  const all = [...states.values()].every((s) => s !== "unchecked");
  return all
    ? { title: "Skipping all objects.", body: "The printing job will be stopped. Continue?", all }
    : { title: `Skipping ${n} objects.`, body: "This action cannot be undone. Continue?", all };
}

/** Initial part states: the GET's skipped flags plus the printer's s_obj. */
export function initialStates(info: SkipObjectsInfo, reported: number[] = []): Map<number, PartState> {
  const skipped = new Set(reported);
  return new Map(info.objects.map((o) => [o.id, o.skipped || skipped.has(o.id) ? "skipped" : "unchecked"]));
}

/** PartSkipDialog::UpdatePartsStateFromPrinter: lock what the printer skipped. */
export function followPrinter(states: Map<number, PartState>, reported: number[]): Map<number, PartState> {
  if (!reported.some((id) => states.has(id) && states.get(id) !== "skipped")) return states;
  const next = new Map(states);
  for (const id of reported) if (next.has(id)) next.set(id, "skipped");
  return next;
}

/** Base64 of raw bytes, for an image data URI. */
export function toBase64(bytes: ArrayBuffer): string {
  const view = new Uint8Array(bytes);
  let binary = "";
  for (let i = 0; i < view.length; i += 0x8000) {
    binary += String.fromCharCode(...view.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}
