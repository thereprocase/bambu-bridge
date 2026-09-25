/**
 * Shared bridge response types. These mirror the shapes the bridge
 * actually returns — kept narrow to what the screens read. Anything
 * the bridge sends that we don't enumerate here lives under `_raw`
 * and is reachable via `unknown` casts at the call site.
 *
 * Post-PR-B (contract §6 / §8.4): the bridge translates raw P1S telemetry
 * at the edge. `PrinterSnapshot` is the §6 *root-level* translated shape
 * (no `.state` wrapper — the curated blocks are at the root and the raw
 * push_status lives under `_raw`). `EventRow` is the §8.4 pre-rendered
 * feed row (`kind`/`ts`/`dismissed`, with `title`/`detail`/`context`
 * the APK renders verbatim).
 */

export type CertStatus = "unknown" | "trusted" | "changed";

export type SessionErrorPhase =
  | "tls_handshake"
  | "mqtt_connack"
  | "mqtt_no_telemetry"
  | "ftps_login"
  | "mqtt_protocol_error"
  | "unknown";

export type Phase =
  | "idle"
  | "preparing"
  | "printing"
  | "paused"
  | "completed"
  | "failed"
  | "unknown";

export type HeadlineIndicator =
  | "none"
  | "indeterminate"
  | "progress"
  | "amber"
  | "green"
  | "red";

export interface PrinterSummary {
  serial: string;
  friendly_name: string;
  model: string | null;
  connected: boolean;
  gcode_state?: string | null;
  progress_pct?: number | null;
  subtask_name?: string | null;
}

/** Contract §6 — the translated snapshot. Root of `GET /printers/{id}`,
 * of `{type:snapshot,data}`, and the deep-merge target of every `delta`. */
export interface PrinterSnapshot {
  printer_id: string;
  serial: string;
  friendly_name: string;
  model: string | null;
  session: {
    connected: boolean;
    last_telemetry_at: string | null;
    last_connect_attempt: string | null;
    last_failure_phase: SessionErrorPhase | null;
  };
  cert_status: CertStatus;
  expected_fingerprint?: string | null; // Internal on current bridges; do not depend on it.
  phase: Phase;
  phase_reason: string | null;
  headline: {
    title: string;
    subtitle: string;
    indicator: HeadlineIndicator;
  };
  job: {
    subtask_name: string | null;
    layer_num: number | null;
    total_layer_num: number | null;
    percent: number | null;
    estimate_total_min: number | null;
    remaining_min: number | null;
    started_at: string | null;
  };
  temps: {
    nozzle: { current_c: number | null; target_c: number | null };
    bed: { current_c: number | null; target_c: number | null };
    chamber: { current_c: number | null; target_c: number | null };
  };
  cooling: {
    part_fan: { percent: number | null; _raw: string | null };
    aux_fan: { percent: number | null; _raw: string | null };
    chamber_fan: { percent: number | null; _raw: string | null };
  };
  lights: { chamber_on: boolean | null };
  print_params: { speed_mm_s: number | null; flow_pct: number | null };
  motion: {
    x: number | null;
    y: number | null;
    z: number | null;
    e: number | null;
  };
  ams: {
    // Contract §6.1.1: AMS hardware attached? Independent of `slots`, which
    // transiently empties to [] during an RFID re-scan. `present && slots == []`
    // ⇒ re-scan in progress (hold the previous slot view), `!present` ⇒ no AMS.
    present: boolean;
    engaged_slot: number | string | null;
    slots: AmsSlotWire[];
    units?: { id: string; humidity_pct: number | null; temperature_c: number | null }[];
    external_spool: {
      in_use: boolean;
      type: string | null;
      color: string | null;
      _raw_id: number;
    };
  };
  print_error: PrintErrorWire | null;
  /** Active printer stage (firmware task). Absent on older server versions. */
  stage?: StageWire | null;
  /** Active HMS health warnings. Empty list means none. Absent on older servers. */
  hms?: HmsEntryWire[];
  /** Job-context muxing (v0.18 wave 2). Absent on older servers — treat absence
   * as null, never invent a context. */
  job_context?: JobContext | null;
  /** Anomaly when the job ended short of 100%. Absent when none. */
  job_anomaly?: JobAnomaly | null;
  _raw: Record<string, unknown>;
}

export interface AmsSlotWire {
  physical_slot: number;
  type: string | null;
  color: string | null;
  rfid_tray: string | null;
  state: "loaded" | "empty";
  remaining_g: number | null;
  remaining_pct: number | null;
  _raw_id: number;
  /** Operator-set human label for this slot, or null if none remembered.
   * Server-side (survives WS drops / slot emptying); cleared automatically by
   * the bridge when the slot's seen filament TYPE changes. */
  memory: AmsSlotMemoryWire | null;
}

/** The `{make,model,profile}` label carried inline on each AMS slot. Any field
 * may be an empty string; a present block always has at least one non-empty. */
export interface AmsSlotMemoryWire {
  make: string;
  model: string;
  profile: string;
}

export interface PrintErrorWire {
  code: string;
  hex: string;
  text: string;
  category: string;
  severity: string;
  remediation?: string;
  /** Bambu Wiki deep-link for this error, or absent. Guard before opening. */
  wiki_url?: string;
  _raw: unknown;
}

/**
 * Job-context muxing (parallel server work order, v0.18 wave 2).
 * Absent on older servers — always guard with `?? null` before consuming.
 */
export type JobContext = "no_job" | "printing" | "finishing" | "done";

/** Anomaly reported by the server when a job ends short of 100%.
 * `type` is an open string enum; "short_finish" is the only defined value
 * as of this writing. Absent when no anomaly. */
export interface JobAnomaly {
  type: string;
  text: string;
  percent: number;
}

/** One active HMS health-management warning from the bridge. */
export interface HmsEntryWire {
  code: string;
  hex: string;
  text: string;
  category: string;
  /** "info" | "warn" | "error" | "unknown" */
  severity: string;
  remediation?: string;
  wiki_url?: string;
  /** Phase-tuned advice string, or absent. Replaces generic remediation in the
   * Issues card when present (it is more specific to the current job phase). */
  context_note?: string | null;
  /** True when this entry is a latched residue from a previous job and the
   * printer is currently idle. Stale entries must not drive the headline and
   * should be visually dimmed in the Issues card. */
  stale?: boolean;
  _raw: unknown;
}

/** Printer-stage block — current task the firmware is executing. */
export interface StageWire {
  /** Numeric stage id, or null when not reported. */
  id: number | null;
  /** Human text for the stage, or null when the id is not known. */
  text: string | null;
}

export interface RegisterPrinterBody {
  host: string;
  access_code: string;
  friendly_name?: string;
}

export interface RegisterPrinterResponse {
  printer_id: string;
  serial: string;
  model: string | null;
  friendly_name: string;
  connected: boolean;
  first_telemetry_at: string;
}

// JobState (DB enum). PR B will add "submitted" + "preparing"; keep both
// shapes here so the app survives the wire bump in either direction.
export type JobState =
  | "queued"
  | "uploading"
  | "started"      // pre-PR-B
  | "submitted"    // post-PR-B
  | "preparing"    // post-PR-B
  | "printing"
  | "completed"
  | "failed"
  | "canceled";

export interface Job {
  id: string;
  printer_id: string;
  state: JobState;
  filename: string;
  started_at?: string | null;
  completed_at?: string | null;
  failure_reason?: string | null;
}

export interface QueueItem {
  id: string;
  printer_id: string;
  file_name: string;
  file_path: string;
  ams_mapping: number[] | null;
  notes?: string | null;
  position: number;
  created_at: string;
}

export interface Spool {
  id: string;
  name: string;
  material: string;
  color_hex?: string;
  remaining_g?: number;
  percent?: number;
}

/** Contract §8.4 — one pre-rendered row of the flat per-printer event feed.
 * `title`/`detail`/`context` are bridge-formatted; the APK renders verbatim. */
export interface EventRow {
  id: number;
  ts: string;            // ISO-8601
  severity: "info" | "warn" | "error";
  kind: string;          // e.g. "print_started", "cert_changed", "filament_runout"
  title: string;
  detail: string;
  context: string;
  job_id?: string | null;
  dismissed: boolean;
}
