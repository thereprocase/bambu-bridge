import type { MaintenanceState } from "../api/control";

export const Z_PRESETS = [["Top", 0], ["Upper", 83], ["Lower", 167], ["Bottom", 250]] as const;
export const XY_PRESETS = [
  { label: "↖\nFull", name: "Back left", x: 0, y: 256, row: 0, col: 0 },
  { label: "↗\nFull", name: "Back right", x: 256, y: 256, row: 0, col: 4 },
  { label: "↖\nHalf", name: "Halfway back left", x: 64, y: 192, row: 1, col: 1 },
  { label: "↗\nHalf", name: "Halfway back right", x: 192, y: 192, row: 1, col: 3 },
  { label: "Center", name: "Center", x: 128, y: 128, row: 2, col: 2 },
  { label: "↙\nHalf", name: "Halfway front left", x: 64, y: 64, row: 3, col: 1 },
  { label: "↘\nHalf", name: "Halfway front right", x: 192, y: 64, row: 3, col: 3 },
  { label: "↙\nFull", name: "Front left", x: 0, y: 0, row: 4, col: 0 },
  { label: "↘\nFull", name: "Front right", x: 256, y: 0, row: 4, col: 4 },
] as const;
export const XY_DIRECTIONS = [
  ["↖", "back left", -1, 1, 0, 0], ["↑", "back", 0, 1, 0, 1], ["↗", "back right", 1, 1, 0, 2],
  ["←", "left", -1, 0, 1, 0], ["→", "right", 1, 0, 1, 2],
  ["↙", "front left", -1, -1, 2, 0], ["↓", "front", 0, -1, 2, 1], ["↘", "front right", 1, -1, 2, 2],
] as const;

export function maintenanceReady(snapshot: Record<string, any> | null | undefined, axes: "Z" | "XY", now = Date.now()) {
  const raw = snapshot?._raw ?? {};
  const age = now - Date.parse(snapshot?.session?.last_telemetry_at ?? "");
  const bits = axes === "Z" ? 4 : 3;
  return snapshot?.session?.connected === true && Number.isFinite(age) && age >= 0 && age <= 15000
    && ["IDLE", "FINISH", "FAILED"].includes(raw.gcode_state)
    && Number.isInteger(raw.home_flag) && (raw.home_flag & bits) === bits;
}
export function latestMaintenance(live?: MaintenanceState, response?: MaintenanceState | null) {
  if (!response || !live || live.session_id !== response.session_id) return live;
  return response.revision > live.revision ? response : live;
}
export function maintenanceStepAllowed(state: MaintenanceState | undefined, axis: "X" | "Y" | "Z", direction: number, step: number) {
  const current = state?.last_requested?.[axis];
  if (typeof current !== "number" || !Number.isFinite(current)) return false;
  if (![1, 10, 50].includes(step)) return false;
  const limit = axis === "Z" ? 250 : 256;
  return current >= 0 && current <= limit && (direction === 0 || (direction < 0 ? current > 0 : current < limit));
}
