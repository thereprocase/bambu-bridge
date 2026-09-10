/**
 * Typed control endpoints. Mirrors `bambu_bridge/api/control.py`.
 * Every screen action that talks to the printer goes through here so
 * the wire shape lives in one file — UI just calls `pause(id)`.
 *
 * Each exported function emits a `control.action` QA log line after the
 * request resolves. Payload: {action, ok, status} — no command bodies
 * (coordinates are fine per QA spec but we keep it minimal).
 */

import { qaLog } from "../lib/qalog";
import { request } from "./client";
import { BridgeError } from "./errors";

export type PrintAction = "pause" | "resume" | "stop";

/** Thin wrapper: run a control request and emit control.action regardless
 * of outcome. Re-throws on failure so callers still see the error. */
async function tracked<T>(
  action: string,
  req: Promise<T>,
): Promise<T> {
  try {
    const result = await req;
    qaLog("control.action", { action, ok: true, status: 200 });
    return result;
  } catch (e) {
    const status = e instanceof BridgeError ? e.status : 0;
    qaLog("control.action", { action, ok: false, status });
    throw e;
  }
}

export function printAction(id: string, action: PrintAction) {
  return tracked(
    action,
    request<{ sent: unknown }>(`/printers/${id}/print/${action}`, { method: "POST" }),
  );
}

export function setLight(id: string, on: boolean) {
  return tracked(
    on ? "light_on" : "light_off",
    request<{ sent: unknown }>(`/printers/${id}/light`, { method: "POST", body: { on } }),
  );
}

export function setTemperature(
  id: string,
  body: { nozzle?: number; bed?: number },
) {
  return tracked(
    "set_temperature",
    request<{ sent: unknown[] }>(`/printers/${id}/temperature`, { method: "POST", body }),
  );
}

export function setFan(id: string, part: "part" | "aux" | "chamber", percent: number) {
  return tracked(
    `fan_${part}`,
    request<{ sent: unknown }>(`/printers/${id}/fan`, { method: "POST", body: { part, percent } }),
  );
}

export function setSpeed(id: string, level: 1 | 2 | 3 | 4) {
  return tracked(
    "set_speed",
    request<{ sent: unknown }>(`/printers/${id}/speed`, { method: "POST", body: { level } }),
  );
}

export function sendGcode(id: string, line: string) {
  return tracked(
    "send_gcode",
    request<{ sent: unknown }>(`/printers/${id}/gcode`, { method: "POST", body: { line } }),
  );
}

export function home(id: string) {
  return tracked(
    "home",
    request<{ sent: unknown }>(`/printers/${id}/home`, { method: "POST" }),
  );
}

export function move(
  id: string,
  body: { axis: "X" | "Y" | "Z"; distance_mm: number; feed_mm_min?: number },
) {
  return tracked(
    `move_${body.axis.toLowerCase()}`,
    request<{ sent: unknown }>(`/printers/${id}/move`, { method: "POST", body }),
  );
}

export function amsControl(id: string, action: "pause" | "resume" | "reset") {
  return tracked(
    `ams_${action}`,
    request<{ sent: unknown }>(`/printers/${id}/ams/control`, { method: "POST", body: { action } }),
  );
}

export function amsChange(
  id: string,
  body: { target_tray: number; cur_temp?: number; tar_temp?: number },
) {
  return tracked(
    "ams_change",
    request<{ sent: unknown }>(`/printers/${id}/ams/change`, { method: "POST", body }),
  );
}

export function unloadFilament(id: string) {
  return tracked(
    "filament_unload",
    request<{ sent: unknown }>(`/printers/${id}/filament/unload`, { method: "POST" }),
  );
}

// ── Wave-1 / Controls additions ───────────────────────────────────────────────

/** Work / task light. mode "flashing" accepts optional loop_times + interval_time. */
export function setWorkLight(
  id: string,
  mode: "on" | "off" | "flashing",
  opts?: { loop_times?: number; interval_time?: number },
) {
  return tracked(
    `work_light_${mode}`,
    request<{ sent: unknown }>(`/printers/${id}/work_light`, {
      method: "POST",
      body: { mode, ...opts },
    }),
  );
}

/** Enable / disable on-printer video recording to SD card. */
export function setIpcamRecord(id: string, enabled: boolean) {
  return tracked(
    enabled ? "ipcam_record_on" : "ipcam_record_off",
    request<{ sent: unknown }>(`/printers/${id}/ipcam/record`, {
      method: "POST",
      body: { enabled },
    }),
  );
}

/** Enable / disable timelapse generation to SD card. */
export function setIpcamTimelapse(id: string, enabled: boolean) {
  return tracked(
    enabled ? "ipcam_timelapse_on" : "ipcam_timelapse_off",
    request<{ sent: unknown }>(`/printers/${id}/ipcam/timelapse`, {
      method: "POST",
      body: { enabled },
    }),
  );
}

// ── Advanced endpoints (§11) ──────────────────────────────────────────────────

/** XCam module toggle (spaghetti detector, first layer inspector, etc.). */
export function setXcam(
  id: string,
  module_name: string,
  enabled: boolean,
  print_halt?: boolean,
) {
  return tracked(
    `xcam_${module_name}`,
    request<{ sent: unknown }>(`/printers/${id}/xcam`, {
      method: "POST",
      body: { module_name, enabled, print_halt },
    }),
  );
}

/** print_option flags — auto_recovery, air_print_detect, etc. */
export function setPrintOption(id: string, flags: Record<string, boolean>) {
  return tracked(
    "print_option",
    request<{ sent: unknown }>(`/printers/${id}/print_option`, {
      method: "POST",
      body: flags,
    }),
  );
}

/** Skip specific objects (cancel-object) mid-print. */
export function skipObjects(id: string, obj_list: number[]) {
  return tracked(
    "skip_objects",
    request<{ sent: unknown }>(`/printers/${id}/skip_objects`, {
      method: "POST",
      body: { obj_list },
    }),
  );
}

/** Write filament profile to an AMS tray (untagged spool override). */
export interface AmsFilamentSettingBody {
  ams_id: number;
  tray_id: number;
  tray_info_idx: string;
  tray_color: string;         // 8-char RRGGBBAA hex
  nozzle_temp_min: number;
  nozzle_temp_max: number;
  tray_type: string;
}
export function amsFilamentSetting(id: string, body: AmsFilamentSettingBody) {
  return tracked(
    "ams_filament_setting",
    request<{ sent: unknown }>(`/printers/${id}/ams/filament_setting`, {
      method: "POST",
      body,
    }),
  );
}

/** Trigger RFID re-read for a specific AMS slot. */
export function amsRfidRead(id: string, ams_id: number, slot_id: number) {
  return tracked(
    "ams_rfid",
    request<{ sent: unknown }>(`/printers/${id}/ams/rfid`, {
      method: "POST",
      body: { ams_id, slot_id },
    }),
  );
}

/** Start AMS drying cycle. */
export interface AmsDryingBody {
  ams_id: number;
  temp: number;
  cooling_temp: number;
  duration: number;
  humidity: number;
  mode?: number;
  rotate_tray?: boolean;
}
export function amsDrying(id: string, body: AmsDryingBody) {
  return tracked(
    "ams_drying",
    request<{ sent: unknown }>(`/printers/${id}/ams/drying`, {
      method: "POST",
      body,
    }),
  );
}

/** Configure AMS RFID auto-read behaviour per unit. */
export function amsUserSetting(
  id: string,
  ams_id: number,
  startup_read_option: boolean,
  tray_read_option: boolean,
) {
  return tracked(
    "ams_user_setting",
    request<{ sent: unknown }>(`/printers/${id}/ams/user_setting`, {
      method: "POST",
      body: { ams_id, startup_read_option, tray_read_option },
    }),
  );
}

/** Run a calibration routine. option bitmask: 1=vibration, 2=bed level, 4=flow, 7=all. */
export function calibrate(id: string, option: 1 | 2 | 4 | 7, bed_type?: number) {
  return tracked(
    `calibration_${option}`,
    request<{ sent: unknown }>(`/printers/${id}/calibration`, {
      method: "POST",
      body: bed_type !== undefined ? { option, bed_type } : { option },
    }),
  );
}

/** Set nozzle type and diameter (unlocks temp clamp on the bridge). */
export function setNozzle(
  id: string,
  nozzle_type: "stainless_steel" | "hardened_steel",
  nozzle_diameter: 0.2 | 0.4 | 0.6 | 0.8,
) {
  return tracked(
    "set_nozzle",
    request<{ sent: unknown }>(`/printers/${id}/set_accessories/nozzle`, {
      method: "POST",
      body: { nozzle_type, nozzle_diameter },
    }),
  );
}

/** Request firmware/module version refresh from the printer. */
export function getVersion(id: string) {
  return tracked(
    "get_version",
    request<{ sent: unknown }>(`/printers/${id}/get_version`, { method: "POST" }),
  );
}

/** Extrude / retract — RED tier. 5-layer server-side guard chain. */
export function extrude(
  id: string,
  distance_mm: number,
  feedrate?: 120 | 300 | 600,
) {
  return tracked(
    distance_mm >= 0 ? "extrude" : "retract",
    request<{ sent: unknown }>(`/printers/${id}/extrude`, {
      method: "POST",
      body: feedrate !== undefined ? { distance_mm, feedrate } : { distance_mm },
    }),
  );
}

/** Disable all stepper motors (M84). Resets positional tracking. */
export function steppersOff(id: string) {
  return tracked(
    "steppers_off",
    request<{ sent: unknown }>(`/printers/${id}/steppers/off`, { method: "POST" }),
  );
}

/** Raw G-code console — BLACK tier. Gated by BRIDGE_ENABLE_RAW_GCODE on server. */
export function sendRawGcode(id: string, line: string) {
  return tracked(
    "raw_gcode",
    request<{ sent: unknown }>(`/printers/${id}/gcode/raw`, {
      method: "POST",
      body: { line },
    }),
  );
}
