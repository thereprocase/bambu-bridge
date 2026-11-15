/**
 * When the bridge accepts an unload (api/control.py require_not_busy): never
 * during a print, and while paused only when the external spool is the one
 * loaded. Mirrored here so the buttons are disabled instead of sending a
 * command the bridge refuses.
 */
const PRINTING_STATES = new Set(["PREPARE", "RUNNING", "SLICING"]);

export interface UnloadAvailability {
  allowed: boolean;
  /** Shown under a disabled button. */
  reason: string | null;
  /** What is loaded, for the confirmation: "AMS slot 2", "the external spool", or null. */
  loaded: string | null;
  externalLoaded: boolean;
}

export function unloadAvailability(snapshot: unknown): UnloadAvailability {
  const s = (snapshot ?? {}) as { _raw?: { gcode_state?: unknown }; ams?: { engaged_slot?: unknown } };
  const state = String(s._raw?.gcode_state ?? "");
  const engaged = s.ams?.engaged_slot;
  const externalLoaded = engaged === "external";
  const loaded = externalLoaded ? "the external spool"
    : typeof engaged === "number" ? `AMS slot ${engaged}` : null;
  if (PRINTING_STATES.has(state)) {
    return { allowed: false, reason: "Unloading is available when the print ends.", loaded, externalLoaded };
  }
  if (state === "PAUSE" && !externalLoaded) {
    return { allowed: false, reason: "While a print is paused, only the external spool can be unloaded.",
      loaded, externalLoaded };
  }
  return { allowed: true, reason: null, loaded, externalLoaded };
}
