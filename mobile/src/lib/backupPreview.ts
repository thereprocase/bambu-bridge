/**
 * What a backup tells the printer, field by field, for the refill sheet.
 *
 * The printer only switches to a slot that declares the same filament, so a
 * backup may rewrite the backup slot's colour, profile and temperatures. The
 * spool in Spoolman does not change; the Filament row then shows the spool's
 * colour as the fill and the printer's new colour as the ring.
 *
 * Bridges answer in one of two shapes: a per-field list (`changes`), or the
 * whole declaration before and after (`before` / `target`, the older
 * preview and the refill status steps). Both reduce to the same lines here.
 */

export interface FieldChange {
  field: string;
  before: unknown;
  after: unknown;
}

export interface PreviewStep {
  wire: number;
  /** False for a slot the bridge never writes (e.g. a tagged anchor). */
  writes: boolean;
  /** The slot every other member copies. Either row can be it (a tagged backup anchors its primary). */
  anchor?: boolean;
  tagged?: boolean;
  changes: FieldChange[];
}

type Declaration = Record<string, unknown>;

/** The order the owner reads them in: what it is, then how hot, then colour. */
const ORDER = ["tray_type", "tray_info_idx", "nozzle_temp", "tray_color"];

/** A colour as RRGGBBAA when it is one; RRGGBB means opaque. Anything else stays as given. */
function colour8(v: unknown): string | null {
  const text = String(v ?? "").replace(/^#/, "").toUpperCase();
  if (/^[0-9A-F]{8}$/.test(text)) return text;
  if (/^[0-9A-F]{6}$/.test(text)) return `${text}FF`;
  return null;
}

/** Whether a field really changes: "FFFFFF" and "ffffffFF" are one colour, null and "" one blank. */
function differs(field: string, before: unknown, after: unknown): boolean {
  if (field === "tray_color") {
    const [b, a] = [colour8(before), colour8(after)];
    if (b && a) return b !== a;
  }
  return String(before ?? "") !== String(after ?? "");
}

function diff(before: Declaration, after: Declaration): FieldChange[] {
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  return keys
    .filter((k) => differs(k, before[k], after[k]))
    .map((field) => ({ field, before: before[field], after: after[field] }));
}

const isDeclaration = (v: unknown): v is Declaration => !!v && typeof v === "object" && !Array.isArray(v);

/** Read either preview shape; anything unrecognised gives no steps rather than a guess. */
export function previewSteps(body: unknown): PreviewStep[] {
  if (!body || typeof body !== "object") return [];
  const root = body as Record<string, unknown>;
  const plan = isDeclaration(root.plan) ? root.plan : root;
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  return steps.flatMap((raw): PreviewStep[] => {
    if (!raw || typeof raw !== "object") return [];
    const step = raw as Record<string, unknown>;
    if (!Number.isInteger(step.wire)) return [];
    let changes: FieldChange[] | null = null;
    if (Array.isArray(step.changes)) {
      // A bridge listing a field it does not change would read "x → x"; drop it.
      changes = step.changes.flatMap((c) =>
        c && typeof c === "object" && typeof (c as FieldChange).field === "string" &&
        differs((c as FieldChange).field, (c as FieldChange).before, (c as FieldChange).after) ? [c as FieldChange] : [],
      );
    } else if (isDeclaration(step.before) && isDeclaration(step.target)) {
      changes = diff(step.before, step.target);
    }
    if (changes == null) return [];
    // Only a bridge that knows it skips a slot (a tagged anchor) says so.
    const writes = typeof step.writes === "boolean" ? step.writes : true;
    return [{
      wire: step.wire as number, writes, changes,
      ...(typeof step.anchor === "boolean" ? { anchor: step.anchor } : {}),
      ...(typeof step.tagged === "boolean" ? { tagged: step.tagged } : {}),
    }];
  });
}

/**
 * RGB is enough to read a colour; the alpha byte shows only when it differs
 * between the two, so a change in it alone still reads as a change. A value
 * that is not a colour is shown as sent.
 */
function colourLine(before: unknown, after: unknown): string {
  const [b, a] = [colour8(before), colour8(after)];
  const word = (v: unknown, c: string | null, alpha: boolean) =>
    c ? `#${alpha ? c : c.slice(0, 6)}` : shown(v);
  const alpha = !!b && !!a && b.slice(6) !== a.slice(6);
  return `Colour: ${word(before, b, alpha)} → ${word(after, a, alpha)}`;
}
const shown = (v: unknown) => (v == null || v === "" ? "none" : String(v));

/** One line per changed setting; the two nozzle limits read as one range. */
export function changeLines(changes: FieldChange[]): string[] {
  const by = new Map(changes.map((c) => [c.field, c]));
  const lines: { key: string; text: string }[] = [];
  const low = by.get("nozzle_temp_min"), high = by.get("nozzle_temp_max");
  if (low && high) {
    lines.push({ key: "nozzle_temp", text: `Nozzle: ${shown(low.before)}–${shown(high.before)} °C → ${shown(low.after)}–${shown(high.after)} °C` });
  } else if (low) {
    lines.push({ key: "nozzle_temp", text: `Lowest nozzle: ${shown(low.before)} → ${shown(low.after)} °C` });
  } else if (high) {
    lines.push({ key: "nozzle_temp", text: `Highest nozzle: ${shown(high.before)} → ${shown(high.after)} °C` });
  }
  for (const c of changes) {
    if (c.field === "nozzle_temp_min" || c.field === "nozzle_temp_max") continue;
    const text =
      c.field === "tray_color" ? colourLine(c.before, c.after)
        : c.field === "tray_type" ? `Material: ${shown(c.before)} → ${shown(c.after)}`
          : c.field === "tray_info_idx" ? `Filament profile: ${shown(c.before)} → ${shown(c.after)}`
            : `${c.field}: ${shown(c.before)} → ${shown(c.after)}`;
    lines.push({ key: c.field, text });
  }
  const rank = (k: string) => (ORDER.indexOf(k) < 0 ? ORDER.length : ORDER.indexOf(k));
  return lines.sort((a, b) => rank(a.key) - rank(b.key)).map((l) => l.text);
}

/** Heading for one slot's block in the sheet. */
export function stepHeading(step: PreviewStep): string {
  const slot = `Slot ${step.wire + 1}`;
  // Rendered from the bridge's anchor/writes flags, never from which slot was tapped first.
  if (step.anchor && step.tagged) return `${slot} · RFID spool, not written; the other slot copies it`;
  if (step.anchor && !step.changes.length) return `${slot} · stays as it is; the other slot copies it`;
  if (!step.writes) return `${slot} · not written`;
  if (!step.changes.length) return `${slot} · stays as it is`;
  return `${slot} · changes`;
}

/** The colour the printer will declare after the step, for the ring swatch; null when unchanged. */
export function ringAfter(step: PreviewStep): string | null {
  const colour = step.changes.find((c) => c.field === "tray_color");
  const after = colour ? colour8(colour.after) : null;
  return after ? `#${after.slice(0, 6)}` : null;
}
