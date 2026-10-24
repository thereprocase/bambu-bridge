/**
 * The raw slot write's guard (Advanced → AMS filament setting).
 *
 * The bridge refuses a raw write to a slot that holds an assigned spool or
 * belongs to a backup unless the caller says `override`. On 2026-10-24 such a
 * write changed slot 3's filament profile, the bridge took it for another
 * spool and unassigned #8. The refusal names the slot, the spool and the
 * backup in `context`; these words say what the write would do and where
 * the safe way to do it lives. The bridge's own message is the fallback for
 * anything this app does not know.
 */

import type { BridgeEnvelope } from "../api/errors";
import { slotWord } from "./backup";

export const SLOT_IN_USE = "slot_in_use";

export interface SlotGuard {
  title: string;
  /** What writing anyway does, then where to do it instead. */
  lines: string[];
  /** The override button: it names the assignment it ends, when it ends one. */
  overrideLabel: string;
}

interface GuardContext {
  wire_slot?: unknown;
  spool_id?: unknown;
  refill?: { from_wire?: unknown; to_wire?: unknown; state?: unknown } | null;
  assignment_kept?: unknown;
}

const int = (v: unknown): number | null => (Number.isInteger(v) ? (v as number) : null);

/**
 * The words for a 409 from the raw slot write, or null when it is some other refusal.
 * `requested` is the refused write; its material names what the slot would become.
 */
export function slotGuard(envelope: BridgeEnvelope, requested?: { tray_type?: string }): SlotGuard | null {
  if (envelope.error !== SLOT_IN_USE) return null;
  const ctx = (envelope.context ?? {}) as GuardContext;
  const wire = int(ctx.wire_slot);
  const spool = int(ctx.spool_id);
  const from = int(ctx.refill?.from_wire);
  const to = int(ctx.refill?.to_wire);
  const slot = wire != null ? slotWord(wire) : "This slot";
  const Slot = slot.charAt(0).toUpperCase() + slot.slice(1);
  const lines: string[] = [];

  if (spool != null) {
    lines.push(`${Slot} holds spool #${spool}. Writing here changes what the printer declares for it.`);
    // A new product, temperature or colour keeps the assignment; a new
    // material (PLA → PETG) is another spool to the bridge and ends it.
    const material = requested?.tray_type?.trim().toUpperCase();
    if (ctx.assignment_kept === true) lines.push(`Spool #${spool} stays assigned to ${slot}.`);
    else if (ctx.assignment_kept === false) {
      lines.push(`This changes ${slot} to ${material || "another material"}, which ends spool #${spool}'s assignment in that slot. You would assign it again on Filament.`);
    }
  }
  if (from != null && to != null) {
    lines.push(`${Slot} is part of the ${slotWord(from)} → ${slotWord(to)} backup. Writing here makes the backup need you.`);
    lines.push(`To change it, remove the backup first (Filament → ${slotWord(from)} → Backup → Remove backup); that puts both slots back.`);
  }
  if (spool != null && (from == null || to == null)) {
    lines.push(`To change the spool, use Filament → ${slot} → Change spool; the bridge sets the slot to match.`);
  }
  if (!lines.length) {
    lines.push(envelope.message);
    if (envelope.remediation_hint) lines.push(envelope.remediation_hint);
  }
  const ends = spool != null && ctx.assignment_kept === false;
  return { title: `${Slot} is in use`, lines, overrideLabel: ends ? `Write anyway and unassign #${spool}` : "Write anyway" };
}

export interface GuardReceipt {
  overridden?: boolean;
  assignment_kept?: boolean;
  /** The bridge answers before the printer reads the write back, so this is its prediction. */
  refill_state_expected?: string | null;
}

/** The toast after a write that went through, overridden or not. */
export function slotWriteDone(guard: GuardReceipt | null | undefined): string {
  if (!guard?.overridden) return "Filament setting sent";
  const parts = ["Written anyway"];
  if (guard.assignment_kept === true) parts.push("spool still assigned");
  if (guard.assignment_kept === false) parts.push("spool unassigned, assign it again on Filament");
  if (guard.refill_state_expected === "needs_attention") parts.push("the backup may need you, check Filament");
  return parts.join(" · ");
}
