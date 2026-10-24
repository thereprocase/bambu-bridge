/**
 * What an AMS slot declares right now, read from the live snapshot, for the
 * raw filament setting form (Advanced).
 *
 * ams_filament_setting is an absolute write: every field it sends replaces
 * the slot's. The form used to send an empty tray_info_idx and red PLA
 * defaults, so a colour fix also wiped the slot's filament profile and could
 * change its material, which ends the slot's spool assignment. The form now
 * starts from the slot's own declaration and keeps the profile unless the
 * owner picks another.
 */

export interface SlotDeclaration {
  tray_type: string;
  /** Null when the printer has not reported one; "" is a reported manual entry. */
  tray_info_idx: string | null;
  tray_color: string;
  nozzle_temp_min: number;
  nozzle_temp_max: number;
}

const int = (v: unknown): number | null => {
  const n = typeof v === "string" && /^[0-9]{1,3}$/.test(v) ? Number(v) : v;
  return typeof n === "number" && Number.isInteger(n) && n > 0 ? n : null;
};

/** The slot's declaration from `_raw.ams.ams[].tray[]`, or null when the printer has not reported it whole. */
export function reportedDeclaration(snapshot: unknown, amsId: number, trayId: number): SlotDeclaration | null {
  const raw = (snapshot as { _raw?: { ams?: { ams?: unknown } } } | null)?._raw;
  const units = raw?.ams?.ams;
  if (!Array.isArray(units)) return null;
  const unit = units.find((u) => u && String((u as { id?: unknown }).id) === String(amsId)) as { tray?: unknown } | undefined;
  const trays = Array.isArray(unit?.tray) ? unit.tray : [];
  const tray = trays.find((t) => t && String((t as { id?: unknown }).id) === String(trayId)) as Record<string, unknown> | undefined;
  if (!tray) return null;
  const type = typeof tray.tray_type === "string" ? tray.tray_type.trim().toUpperCase() : "";
  const color = typeof tray.tray_color === "string" ? tray.tray_color.trim().toUpperCase() : "";
  const low = int(tray.nozzle_temp_min), high = int(tray.nozzle_temp_max);
  if (!type || !/^[0-9A-F]{8}$/.test(color) || low == null || high == null) return null;
  return {
    tray_type: type,
    tray_info_idx: typeof tray.tray_info_idx === "string" ? tray.tray_info_idx : null,
    tray_color: color,
    nozzle_temp_min: low,
    nozzle_temp_max: high,
  };
}

/** Bambu's generic presets, as the bridge writes them (slot_setting.GENERIC_TRAY_INFO_IDX). */
export const KNOWN_PROFILES: { code: string; label: string }[] = [
  { code: "GFL99", label: "Generic PLA" },
  { code: "GFG99", label: "Generic PETG" },
  { code: "GFB99", label: "Generic ABS" },
  { code: "GFB98", label: "Generic ASA" },
  { code: "GFC99", label: "Generic PC" },
  { code: "GFN99", label: "Generic PA" },
  { code: "GFU99", label: "Generic TPU" },
  { code: "GFS99", label: "Generic PVA" },
];

/** Profile choices: keep the slot's, a known preset, or a manual entry with none. */
export const KEEP_PROFILE = "keep";
export const NO_PROFILE = "none";

/** The tray_info_idx to send for a choice; null when "keep" has nothing reported to keep. */
export function profileToSend(choice: string, current: SlotDeclaration | null): string | null {
  if (choice === KEEP_PROFILE) return current?.tray_info_idx ?? null;
  if (choice === NO_PROFILE) return "";
  return KNOWN_PROFILES.some((p) => p.code === choice) ? choice : null;
}

/** The line under the picker: the code that will be sent, or why Set is off. */
export function profileNote(choice: string, current: SlotDeclaration | null): string {
  const code = profileToSend(choice, current);
  if (code == null) return "The printer hasn't reported this slot's profile yet. Pick one to send.";
  if (choice === KEEP_PROFILE) return code === "" ? "Sends no profile, as the slot has now." : `Sends ${code}, the slot's current profile.`;
  if (code === "") return "Sends no profile (manual entry).";
  return `Sends ${profileLabel(choice, current)}.`;
}

/** The choice's words, so the owner sees the code that will be sent. */
export function profileLabel(choice: string, current: SlotDeclaration | null): string {
  if (choice === KEEP_PROFILE) {
    const code = current?.tray_info_idx;
    return code == null ? "Keep current (not reported yet)" : code === "" ? "Keep current (none)" : `Keep current (${code})`;
  }
  if (choice === NO_PROFILE) return "None (manual entry)";
  const known = KNOWN_PROFILES.find((p) => p.code === choice);
  return known ? `${known.label} (${known.code})` : choice;
}
