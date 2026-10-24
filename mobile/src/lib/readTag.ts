/**
 * Manual RFID read for an AMS slot: the owner presses
 * "Read tag" instead of pulling the spool out and pushing it back in.
 * Nothing here is automatic. The bridge sends the printer's read command;
 * the app then watches the slot's reported identity until it changes or
 * a timeout passes, and says what it found.
 */

import type { InventorySource } from "../api/inventory";
import { EXTERNAL_WIRE } from "./slotInventory";

/**
 * How long one slot's read may take before the app stops watching. Measured on
 * the P1S, slot 2, idle: about 48 s from the command to the tag data (the
 * reading bit is set from ~7.5 s to ~50 s). 75 s leaves room; "Read all tags"
 * budgets this per slot, one after another.
 */
export const READ_TIMEOUT_MS = 75_000;
export const READ_POLL_MS = 2_000;
/** What the row says while the printer reads. */
export const READING_TEXT = "Reading tag… (about a minute)";

export interface Reading {
  wire: number;
  startedAt: number;
  /** The slot as reported before the read was sent; null when it was not reported. */
  before: InventorySource | null;
  state: "reading" | "done";
  text: string;
  /** The send itself failed (the bridge refused or was unreachable); the row offers Try again. */
  failed?: boolean;
}

/** Only AMS trays carry a tag; the external holder never gets a read. */
export function readableWires(sources: InventorySource[]): number[] {
  return sources
    .map((s) => s.wire_slot)
    .filter((w) => w !== EXTERNAL_WIRE && w >= 0 && w < 4)
    .sort((a, b) => a - b);
}

export function sameReport(a: InventorySource | null, b: InventorySource | null): boolean {
  if (!a || !b) return a === b;
  return a.observed_identity === b.observed_identity && a.material === b.material && !!a.tagged === !!b.tagged;
}

/**
 * The words once the slot's report moved or the wait ran out:
 * "Tag read: PETG" / "No tag found in slot 2" / "Slot 2 reports PETG, no tag".
 * `liveName` is the printer's name for the tray when the snapshot has one.
 */
export function readOutcome(
  wire: number,
  after: InventorySource | null,
  liveName: string | null,
  timedOut: boolean,
): string {
  const slot = `slot ${wire + 1}`;
  if (after?.tagged) {
    // The tray name usually carries the material already ("Bambu PETG Translucent"); add it only when it doesn't.
    const name = (liveName ?? "").trim();
    const material = after.material.trim();
    const what = name && material && name.toLowerCase().includes(material.toLowerCase()) ? name : [name, material].filter(Boolean).join(" ");
    return `Tag read: ${what || "spool identified"}`;
  }
  if (after && !after.issues.includes("source_empty") && after.material.trim() && timedOut)
    return `No tag found in ${slot} · reports ${after.material.trim()}`;
  if (after?.issues.includes("source_empty")) return `${slot} is empty`;
  return `No tag found in ${slot}`;
}

/** Whether a read may be sent now: the printer idle, and no read running on that slot. */
export function canRead(printerIdle: boolean, readings: Record<number, Reading | undefined>, wire: number): boolean {
  return printerIdle && readings[wire]?.state !== "reading";
}
