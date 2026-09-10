/**
 * Wire-side error enum — must match the bridge's `errors.py` reserved
 * strings (contract §2.1). Anything new the bridge adds gets added here;
 * the client renders user-facing copy keyed off the enum, not the
 * `message` string, so the bridge can iterate copy without breaking us.
 */

export type BridgeErrorCode =
  | "auth_missing"
  | "auth_invalid"
  | "auth_not_configured"
  | "not_found"
  | "conflict"
  | "invalid_input"
  | "internal_error"
  | "printer_offline"
  | "printer_unreachable"
  | "printer_auth_failed"
  | "printer_cert_changed"
  | "mqtt_no_telemetry"
  | "ftps_failed"
  | "ftps_auth_failed"
  | "invalid_3mf"
  | "print_submit_failed"
  | "print_command_failed"
  | "camera_unavailable"
  | "camera_no_frame"
  | "jog_not_homed"
  | "jog_out_of_envelope"
  | "jog_step_not_allowed"
  | "extrude_state_not_allowed"
  | "nozzle_too_cold"
  | "raw_gcode_disabled";

export interface BridgeAction {
  id: string;
  label: string;
  method: "POST" | "GET" | "DELETE" | "PUT" | null;
  path?: string;
}

export interface BridgeEnvelope {
  error: BridgeErrorCode | string;
  message: string;
  likely_cause?: string;
  remediation_hint?: string;
  context?: Record<string, unknown>;
  discovered?: { serial?: string; model?: string };
  _raw?: Record<string, unknown>;
  actions?: BridgeAction[];
  issues?: (string | Record<string, unknown>)[];
}

/**
 * Thrown by the API client on any non-2xx response that carried a parseable
 * envelope body. Network-level errors (DNS, refused, timeout) throw a
 * `BridgeNetworkError` instead — the two are caught separately so the UI
 * can render different prompts ("the bridge said no" vs "we can't reach
 * the bridge").
 */
export class BridgeError extends Error {
  constructor(
    public envelope: BridgeEnvelope,
    public status: number,
  ) {
    super(envelope.message);
    this.name = "BridgeError";
  }

  get code(): string {
    return this.envelope.error;
  }
}

export class BridgeNetworkError extends Error {
  constructor(
    public outcomeUnknown = false,
  ) {
    super(outcomeUnknown
      ? "Connection lost. The command may have reached the printer. Check its status before trying again."
      : "Couldn't reach the bridge. Check your connection and try again.");
    this.name = "BridgeNetworkError";
  }
}
