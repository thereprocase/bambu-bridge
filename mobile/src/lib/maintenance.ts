import type { MaintenanceState } from "../api/control";
import type { LiveStatus } from "../ws/live";

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

/** Step sizes (mm) the maintenance step endpoints accept; tests/test_jog_step_parity.py keeps this equal to the bridge and web lists. */
export const MAINTENANCE_STEPS = [1, 10, 50] as const;

const IDLE_STATES = ["IDLE", "FINISH", "FAILED"];

/**
 * Why movement is unavailable, or null when the client allows it. Status age
 * is not checked: an idle P1S reports in bursts about 27 s apart, and the
 * bridge asks the printer for a fresh report before it moves, so the server
 * decides whether the data is fresh enough.
 */
export function maintenanceBlocker(
  snapshot: Record<string, any> | null | undefined,
  axes: "Z" | "XY",
  link: LiveStatus = "open",
): string | null {
  if (link !== "open") return "Bridge link is down; waiting to reconnect.";
  if (snapshot?.session?.connected !== true) return "Printer is offline.";
  const raw = snapshot?._raw ?? {};
  const bits = axes === "Z" ? 4 : 3;
  if (typeof raw.gcode_state !== "string" || !Number.isInteger(raw.home_flag)) {
    return "Waiting for the first printer status report.";
  }
  if (!IDLE_STATES.includes(raw.gcode_state)) return "Printer is busy.";
  if ((raw.home_flag & bits) !== bits) {
    return axes === "Z" ? "Z axis is not homed." : "X and Y axes are not homed.";
  }
  return null;
}

export function maintenanceReady(
  snapshot: Record<string, any> | null | undefined,
  axes: "Z" | "XY",
  link: LiveStatus = "open",
) {
  return maintenanceBlocker(snapshot, axes, link) === null;
}

/** The one line shown under the controls while buttons are disabled; null when nothing is blocked. */
export function maintenanceDisabledReason(
  snapshot: Record<string, any> | null | undefined,
  link: LiveStatus,
  pending: boolean,
): string | null {
  const blocked = maintenanceBlocker(snapshot, "Z", link) ?? maintenanceBlocker(snapshot, "XY", link);
  if (blocked) return blocked;
  return pending ? "A movement command is in progress." : null;
}
export function latestMaintenance(live?: MaintenanceState, response?: MaintenanceState | null) {
  if (!response || !live || live.session_id !== response.session_id) return live;
  return response.revision > live.revision ? response : live;
}
export function maintenanceStepAllowed(state: MaintenanceState | undefined, axis: "X" | "Y" | "Z", direction: number, step: number) {
  const current = state?.last_requested?.[axis];
  if (typeof current !== "number" || !Number.isFinite(current)) return false;
  if (!(MAINTENANCE_STEPS as readonly number[]).includes(step)) return false;
  const limit = axis === "Z" ? 250 : 256;
  return current >= 0 && current <= limit && (direction === 0 || (direction < 0 ? current > 0 : current < limit));
}
