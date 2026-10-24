/**
 * Words and request bodies for prints the bridge could not fully charge.
 *
 * A finished print carries `shortfalls`: each names a slot, spool or filament
 * and the grams that never reached Spoolman. The card asks one question per
 * shortfall and closes the print with one resolve call.
 */

import type { AccountingRow, ResolveCharge, Shortfall } from "../api/inventory";
import { EXTERNAL_WIRE } from "./slotInventory";

export function slotName(wire: number | null): string {
  if (wire == null) return "a slot";
  return wire === EXTERNAL_WIRE ? "the external spool" : `slot ${wire + 1}`;
}

// Whole grams: the owner wants nothing finer.
const g = (value: number) => `${Math.round(value)} g`;

/** The one-line statement of a shortfall: what happened and how much. */
export function shortfallLine(s: Shortfall): string {
  const spool = s.spool_id != null ? `#${s.spool_id}` : "";
  switch (s.kind) {
    case "unassigned_slot":
      return `${g(s.grams)} from ${slotName(s.wire_slot)} · no spool was assigned`;
    case "spool_empty":
      return `${g(s.grams)} short · ${spool} ran out in Spoolman`;
    case "spool_archived":
      return `${g(s.grams)} for ${spool} · archived in Spoolman`;
    case "spool_missing":
      return `${g(s.grams)} for ${spool} · ${spool} isn't in Spoolman any more`;
    case "delivery_error":
      return `${g(s.grams)} for ${spool} · Spoolman didn't answer`;
    case "no_layer_plan":
      return `${g(s.grams)} by the slicer's figure · the bridge counted no layers`;
    case "unmapped_filament":
      return `${g(s.grams)} of a filament the print never mapped to a slot`;
    case "unweighed_filament":
      return "A filament the file gave no weight for";
    default:
      return `${g(s.grams)} uncharged`;
  }
}

/** What the card asks for this shortfall. */
export function shortfallQuestion(s: Shortfall): string {
  switch (s.kind) {
    case "unassigned_slot":
      return `Which spool was in ${slotName(s.wire_slot)}?`;
    case "spool_empty":
      return "Charge the rest to which spool?";
    case "delivery_error":
      return `Charge #${s.spool_id} now?`;
    case "unweighed_filament":
      return "How many grams, and from which spool?";
    default:
      return "Charge it to which spool?";
  }
}

/** A finished print's one-line summary for the recent list. */
export function recentLine(row: AccountingRow): string {
  const charged = row.charged ?? [];
  const shortfalls = row.shortfalls ?? [];
  const parts: string[] = [];
  if (charged.length) parts.push(charged.map((c) => `${g(c.grams)} → #${c.spool_id}`).join(", "));
  const owed = shortfalls.reduce((sum, s) => sum + s.grams, 0);
  const action = row.resolution?.action ?? null;
  if (row.delivery_state === "review") parts.push(`${g(owed)} waiting for you`);
  // Shortfalls stay on a closed print as history; the resolution says what became of them.
  else if (action === "charge") parts.push("settled by you");
  else if (action === "charged_elsewhere") parts.push("charged by hand in Spoolman");
  else if (action === "nothing") parts.push(owed > 0 ? `${g(owed)} skipped` : "skipped");
  else if (shortfalls.length && !charged.length) parts.push(owed > 0 ? `${g(owed)} left uncharged` : "part left uncharged");
  if (!parts.length) parts.push(row.review_reason === "stopped_before_first_layer" ? "stopped before the first layer · 0 g" : "nothing to charge");
  return parts.join(" · ");
}

/** "layers 1–40 from #11 · 41–186 from #12" when a refill split the print; null otherwise. */
export function splitLine(row: AccountingRow): string | null {
  const charged = (row.charged ?? []).filter((c) => c.layers && c.layers.length === 2);
  if (charged.length < 2) return null;
  return charged
    .sort((a, b) => a.layers![0] - b.layers![0])
    .map((c, i) => `${i === 0 ? "layers " : ""}${c.layers![0]}–${c.layers![1]} from #${c.spool_id}`)
    .join(" · ");
}

/** "layers 41–186 · 77% · 12 g" for a spool's history row; a stopped print reads "layers 1–6 of 10". */
export function historyLine(print: { layers: [number, number] | null; total_layers: number | null; share_pct: number | null; grams: number }): string {
  const parts: string[] = [];
  if (print.layers) {
    const short = print.total_layers != null && print.layers[1] < print.total_layers;
    parts.push(`layers ${print.layers[0]}–${print.layers[1]}${short ? ` of ${print.total_layers}` : ""}`);
  }
  if (print.share_pct != null && !(print.layers && print.total_layers && print.layers[0] === 1 && print.layers[1] === print.total_layers))
    parts.push(`${Math.round(print.share_pct)}%`);
  parts.push(g(print.grams));
  return parts.join(" · ");
}

export function whenText(row: AccountingRow): string {
  const at = row.ended_at ?? row.created_at;
  if (!at) return "";
  const d = new Date(at * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function outcomeWord(row: AccountingRow): string {
  if (row.outcome === "completed") return "finished";
  if (row.outcome === "interrupted" || row.outcome === "stopped") return "stopped";
  return row.outcome ?? "";
}

/** One owner decision per shortfall. */
export interface Decision {
  shortfall: Shortfall;
  spoolId: number | null;
  /** The grams box as typed: whole grams, digits only. */
  gramsText: string;
}

export const MAX_GRAMS = 10_000;

/**
 * What the grams box keeps of a keystroke: the leading digits, capped at the
 * bridge's limit. "10.5" becomes 10, "1e308" becomes 1, "-5" becomes nothing;
 * nothing typed ever inflates a figure.
 */
export function gramsText(text: string): string {
  const digits = /^\s*(\d+)/.exec(text)?.[1] ?? "";
  if (!digits) return "";
  return String(Math.min(MAX_GRAMS, parseInt(digits, 10)));
}

/** The whole grams in the box, or null when the box is empty, zero or out of range. */
export function parseGrams(text: string): number | null {
  const value = /^\d+$/.test(text) ? parseInt(text, 10) : NaN;
  return Number.isInteger(value) && value > 0 && value <= MAX_GRAMS ? value : null;
}

/** Whole grams for the counted figure, the way the box shows it. */
export const countedText = (s: Shortfall) => String(Math.min(MAX_GRAMS, Math.round(s.grams)));

/** The resolve body: a slot charge when the counted figure stands, else spool and grams. */
export function chargesFor(decisions: Decision[]): ResolveCharge[] {
  const charges: ResolveCharge[] = [];
  for (const d of decisions) {
    if (d.spoolId == null) continue;
    const grams = parseGrams(d.gramsText);
    if (grams == null) continue;
    const counted = d.shortfall.kind === "unassigned_slot" && d.shortfall.wire_slot != null && d.gramsText === countedText(d.shortfall);
    charges.push(counted ? { spool_id: d.spoolId, slot: d.shortfall.wire_slot! } : { spool_id: d.spoolId, grams });
  }
  return charges;
}

/** Charge is offered only when every picked spool has usable grams and at least one spool is picked. */
export function chargeable(decisions: Decision[]): boolean {
  const picked = decisions.filter((d) => d.spoolId != null);
  return picked.length > 0 && picked.every((d) => parseGrams(d.gramsText) != null);
}
