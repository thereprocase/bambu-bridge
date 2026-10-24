/**
 * Backup spool: which slots can stand in for which, and the words for it.
 *
 * The printer's cross-colour refill continues a print from another slot
 * that declares the same colour; the bridge writes those declarations.
 * One backup chain per printer in this beta.
 */

import type { Backup, RefillStatus } from "../api/inventory";
import { type SlotView } from "./slotInventory";

/** The bridge's refill drives the first AMS only (its backup routes take wires 0-3). */
const onRefillAms = (wire: number) => Number.isInteger(wire) && wire >= 0 && wire <= 3;

/**
 * AMS slots that can back up `from`: loaded, same material, at most one of the
 * pair tagged. A tagged spool is never written; it anchors the pair and the
 * untagged slot copies its tag's details. A tag still being read has no
 * details to copy yet. The bridge has the last word in the preview.
 */
export function backupCandidates(from: SlotView, views: SlotView[]): SlotView[] {
  const material = from.source.material.trim().toUpperCase();
  return views.filter(
    (v) =>
      v.source.wire_slot !== from.source.wire_slot &&
      onRefillAms(v.source.wire_slot) &&
      !(v.source.tagged && from.source.tagged) &&
      !v.source.tag_reading &&
      !v.source.issues.includes("source_empty") &&
      !!material &&
      v.source.material.trim().toUpperCase() === material,
  );
}

/** Whether the row for `from` offers a backup at all. */
export function canOfferBackup(from: SlotView, views: SlotView[]): boolean {
  return (
    onRefillAms(from.source.wire_slot) &&
    !from.source.tag_reading &&
    !!from.assignment &&
    !from.source.issues.includes("source_empty") &&
    backupCandidates(from, views).length > 0
  );
}

/** How often an open Filament tab asks about a chain that has not settled. */
export const BACKUP_POLL_MS = 5_000;

export const slotWord = (wire: number) => `slot ${wire + 1}`;

/** The one chain on this printer, if any. */
export function currentBackup(status: RefillStatus | null): Backup | null {
  return status?.backups?.[0] ?? null;
}

/**
 * A pending chain the printer last reported as running, with that report's age.
 *
 * The bridge calls a chain active only on a report under 30 s old. An idle
 * printer goes quiet, so the bridge drops to pending while the last thing
 * the printer said (auto-refill on, this pair's backup mask) still stands
 * (seen live 2026-10-24: active → pending on a quiet printer, back to active
 * with mask 12 on the next frame). That is staleness, not a change, and the
 * rows say so instead of "waiting for the printer to confirm".
 * Null when the last report does not show this chain running.
 */
export function lastConfirmed(backup: Backup, status: RefillStatus | null): { ageSeconds: number | null } | null {
  if (backup.state !== "pending" || status?.printer_connected === false) return null;
  // A bridge that says it also checked ownership and declarations the
  // app cannot see; its word wins. The derivation below is for older bridges.
  if (typeof backup.stale === "boolean") {
    if (!backup.stale) return null;
    const age = backup.last_confirmed_age_seconds;
    return { ageSeconds: typeof age === "number" && age >= 0 ? age : null };
  }
  if (!status) return null;
  if (status.eligibility_fresh !== false || status.last_reported_auto_refill !== true) return null;
  const wires = status.steps?.length ? status.steps.map((s) => s.wire) : [backup.from_wire, backup.to_wire];
  const mask = wires.reduce((bits, wire) => bits | (1 << wire), 0);
  if (!(status.reported_backup_masks ?? []).some((bits) => bits === mask)) return null;
  const age = status.eligibility_age_seconds;
  return { ageSeconds: typeof age === "number" && age >= 0 ? age : null };
}

const ago = (seconds: number | null) =>
  seconds == null ? "a while ago"
    : seconds < 90 ? `${Math.round(seconds)} s ago`
      : seconds < 90 * 60 ? `${Math.round(seconds / 60)} min ago`
        : `${Math.round(seconds / 3600)} h ago`;

/** The chain's state words; null only for a state this app does not know.
 *
 * Both rows and the sheet take this one value, so they cannot disagree.
 * A running chain says "confirmed" so a bare line never has to mean "fine"
 * (2026-10-24 flow scrub: the owner could not tell confirmed from unsaid).
 * `pending` covers four different waits; a dropped connection, a quiet
 * printer whose last report showed the chain running, and the bridge's
 * message (auto-refill off) are named, since none of them is the printer
 * still confirming a new backup.
 */
export function backupStatus(backup: Backup, status: RefillStatus | null = null): string | null {
  switch (backup.state) {
    case "active":
      return "confirmed";
    case "pending": {
      if (status?.printer_connected === false) return "printer offline";
      const held = lastConfirmed(backup, status);
      if (held) return `last confirmed ${ago(held.ageSeconds)}`;
      return backup.message ? lowerFirst(backup.message) : "waiting for the printer to confirm";
    }
    case "restoring":
    case "restoration_pending":
      return "being removed";
    case "needs_attention":
      return "needs you";
    default:
      return null;
  }
}

/** Whether the screen should keep asking: any chain the printer does not run yet.
 *
 * The printer reports the new backup mask a few seconds after the write
 * (6-10 s in the N9 capture), and an owner fixing "needs you" or turning on
 * auto-refill does it on the printer, so none of these states end on a tap
 * in the app.
 */
export function backupSettling(backup: Backup | null): boolean {
  return !!backup && backup.state !== "active";
}

const lowerFirst = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

/** The row's chain line for a slot that is part of the chain, else null. */
export function backupLine(wire: number, backup: Backup | null, status: RefillStatus | null = null): string | null {
  if (!backup) return null;
  const base =
    backup.from_wire === wire
      ? `Backup: ${slotWord(backup.to_wire)}`
      : backup.to_wire === wire
        ? `Backs up ${slotWord(backup.from_wire)}`
        : null;
  if (!base) return null;
  const words = backupStatus(backup, status);
  return words ? `${base} · ${words}` : base;
}

/** What the sheet says when the owner wants a different chain while one exists. */
export function chainInUse(from: number, backup: Backup | null): string | null {
  if (!backup || backup.from_wire === from) return null;
  return `Backup in use: ${slotWord(backup.from_wire)} → ${slotWord(backup.to_wire)}. Remove it first.`;
}
