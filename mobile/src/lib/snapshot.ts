/**
 * Snapshot adapter — projects the bridge's §6 translated snapshot onto the
 * flat `View` the screens render.
 *
 * Post-PR-B the bridge does the translation at the edge: `phase`, `headline`,
 * `temps`, `cooling`, `job`, `ams.slots`, etc. all live at the snapshot ROOT
 * (the raw push_status is preserved under `_raw`). This adapter reads those
 * root blocks directly — there is no longer a `.state` wrapper.
 *
 * Defensive on purpose: every field is null-guarded and any nesting is
 * `typeof === "object"` checked. A partial snapshot (e.g. before first
 * telemetry) never throws — it just shows what it can.
 */

import { PrinterSnapshot } from "../api/types";

/**
 * Returns true only for URLs that are safe to open via Linking.openURL —
 * must start with the exact Bambu Wiki https origin.
 *
 * Exported as a pure predicate so it can be unit-tested without mocking
 * Linking.
 */
export function isSafeBambuWikiUrl(url: unknown): url is string {
  return (
    typeof url === "string" &&
    url.startsWith("https://wiki.bambulab.com/")
  );
}

/** Normalised view of one active HMS health warning. */
export interface HmsEntry {
  code: string;
  hex: string;
  text: string;
  category: string;
  /** "info" | "warn" | "error" | "unknown" */
  severity: string;
  remediation: string | null;
  wikiUrl: string | null;
  /** Phase-tuned advice from the server, or null when absent. When present,
   * the Issues card renders this instead of the generic `remediation` line. */
  contextNote: string | null;
  /** True when this entry is residue from a previous job (printer idle).
   * Stale entries are dimmed in the Issues card and skipped in pickHms when
   * fresher (non-stale) entries exist. */
  stale: boolean;
}

/** Stage block — what task the printer firmware is currently executing. */
export interface Stage {
  id: number | null;
  text: string | null;
}

export type Phase =
  | "idle"
  | "preparing"
  | "printing"
  | "paused"
  | "finished"
  | "failed"
  | "unknown";

/**
 * Job-context muxing, mirrored from the wire type but re-exported here so
 * callers can import it from the snapshot module without touching api/types.
 */
export type JobContext = "no_job" | "printing" | "finishing" | "done";

/** Normalised job anomaly — null when absent or malformed. */
export interface JobAnomalyView {
  type: string;
  text: string;
  percent: number;
}

export type Indicator =
  | "none"
  | "indeterminate"
  | "progress"
  | "amber"
  | "green"
  | "red";

export interface View {
  phase: Phase;
  /** Bridge-recommended headline title (rendered verbatim). */
  headline: string;
  /** Bridge-recommended headline subtitle (rendered verbatim). */
  subtitle: string | null;
  /** Bridge-recommended headline indicator style. */
  indicator: Indicator;
  /** 0-100, or null if not applicable to the phase */
  progress: number | null;
  subtaskName: string | null;
  layer: number | null;
  totalLayers: number | null;
  timeLeftMin: number | null;     // job.remaining_min
  /**
   * Locale-formatted ETA string ("Done ~3:47 PM") computed as
   * now + remaining_min. Only present when phase is printing or paused
   * AND remaining_min is a finite number. Null otherwise — never
   * "Invalid Date" or "NaN"; absent data renders as nothing.
   *
   * Computed at viewOf() call time (each snapshot update), not on a
   * per-second timer, so the value advances with the snapshot, not the
   * clock.
   */
  etaTimeStr: string | null;
  nozzleActual: number | null;
  nozzleTarget: number | null;
  bedActual: number | null;
  bedTarget: number | null;
  chamberTemp: number | null;
  lightOn: boolean | null;
  fanPart: number | null;          // 0-100%
  fanAux: number | null;
  fanChamber: number | null;
  speedLevel: number | null;
  /** AMS hardware attached? (§6.1.1). `amsPresent && ams.length === 0` ⇒
   * an RFID re-scan is in progress — hold the previous slot view, don't
   * latch "No AMS detected". `!amsPresent` ⇒ no AMS hardware. */
  amsPresent: boolean;
  /** AMS slot rows — index 0 = physical slot 1. */
  ams: AmsSlot[];
  printError: string | null;
  /** Full print_error object (null when none). Carries wiki_url for the issues card. */
  printErrorFull: (import("../api/types").PrintErrorWire & { wiki_url?: string }) | null;
  /** Current printer stage (firmware task). Absent blocks default to {id:null,text:null}. */
  stage: Stage;
  /** Active HMS health warnings. Empty when none or block absent. */
  hms: HmsEntry[];
  /**
   * The one-line lead status the status screen should display.
   * Computed by `statusLine()` from phase + stage + hms + print_error.
   */
  statusLine: string;
  /**
   * Job-context muxing from the server. Null when the field is absent (older
   * server) — never synthesised. Consumers must null-guard before acting on it.
   */
  jobContext: JobContext | null;
  /**
   * Job anomaly when the job ended short of 100%. Null when absent or malformed.
   */
  jobAnomaly: JobAnomalyView | null;
}

export interface AmsSlot {
  physicalSlot: number;            // 1-4
  empty: boolean;
  name?: string | null;
  color?: string | null;
  remainingPercent?: number | null;
  /** Operator-set human label remembered for this slot (server-side), or null.
   * Present even when the slot is empty or the WS is down — labels live on the
   * bridge, not in live telemetry. The screen prefers this over `name` for the
   * primary line so tagless spools read as e.g. "Polymaker PolyLite ASA". */
  memory?: SlotMemory | null;
}

/** Normalized slot label. All three fields are strings (never null) so call
 * sites don't have to null-guard each; `null` memory ⇒ no label at all. */
export interface SlotMemory {
  make: string;
  model: string;
  profile: string;
}

// The bridge `phase` enum is already user-facing; we only remap `completed`
// → the app's historical `finished` token so existing call sites that switch
// on `"finished"` keep working. Raw `gcode_state` values are NOT consulted
// here anymore — the bridge derives phase per §6.0.1 and we trust it.
const PHASE_MAP: Record<string, Phase> = {
  idle: "idle",
  preparing: "preparing",
  printing: "printing",
  paused: "paused",
  completed: "finished",
  finished: "finished",
  failed: "failed",
  unknown: "unknown",
};

function num(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Normalise a raw HMS wire entry — tolerates missing/wrong-type fields. */
function normaliseHms(raw: unknown): HmsEntry | null {
  const r = obj(raw);
  if (!r) return null;
  const text = typeof r.text === "string" ? r.text : null;
  if (!text) return null;   // no text → not renderable; skip
  const wiki = typeof r.wiki_url === "string" && isSafeBambuWikiUrl(r.wiki_url)
    ? r.wiki_url : null;
  return {
    code: str(r.code),
    hex: str(r.hex),
    text,
    category: str(r.category),
    severity: typeof r.severity === "string" ? r.severity : "unknown",
    remediation: typeof r.remediation === "string" ? r.remediation : null,
    wikiUrl: wiki,
    // Empty-string context_note must be coerced to null: `"" ?? remediation`
    // evaluates to `""` (nullish coalescing skips only null/undefined), which
    // makes IssueEntry render nothing instead of the fallback remediation text.
    contextNote: (typeof r.context_note === "string" && r.context_note !== "")
      ? r.context_note : null,
    stale: r.stale === true,
  };
}

/** Severity rank — higher is more severe. */
const SEVERITY_RANK: Record<string, number> = {
  error: 3,
  warn: 2,
  info: 1,
  unknown: 0,
};

/**
 * Pick the highest-severity HMS entry; tie-break by first occurrence.
 *
 * Stale-skip rule: if there is at least one non-stale entry, stale entries are
 * excluded from consideration. If ALL entries are stale (or the array is
 * empty), stale entries are eligible but the caller (statusLine) treats the
 * all-stale case specially — it still falls back to the headline copy rather
 * than surfacing a stale cause.
 */
function pickHms(hms: HmsEntry[]): HmsEntry | null {
  if (hms.length === 0) return null;
  const fresh = hms.filter((e) => !e.stale);
  const pool = fresh.length > 0 ? fresh : hms;
  let best = pool[0];
  for (let i = 1; i < pool.length; i++) {
    const rank = SEVERITY_RANK[pool[i].severity] ?? 0;
    const bestRank = SEVERITY_RANK[best.severity] ?? 0;
    if (rank > bestRank) best = pool[i];
  }
  return best;
}

/**
 * Format a category + text into a short cause string.
 * Adds a "CATEGORY: " prefix when the category is informative (not generic
 * sentinel values). Keeps the whole string to one screen line by truncating
 * at MAX_CAUSE_LEN characters with an ellipsis.
 */
const MAX_CAUSE_LEN = 60;
const OPAQUE_CATEGORIES = new Set(["generic", "unknown", ""]);

function formatCause(category: string, text: string): string {
  const cat = category.toLowerCase().trim();
  const usePrefix = !OPAQUE_CATEGORIES.has(cat);
  // Capitalise first letter of category for display ("ams" → "AMS").
  const prefix = usePrefix ? `${category.toUpperCase()}: ` : "";
  const full = `${prefix}${text}`;
  return full.length > MAX_CAUSE_LEN ? `${full.slice(0, MAX_CAUSE_LEN - 1)}…` : full;
}

/**
 * Determine the headline verb prefix for paused/failed states, taking job
 * context into account.
 *
 * When `jobContext` is "finishing" or "done" AND the phase is "paused", the
 * print has completed even though the printer reported a pause (typically an
 * AMS retract jam or similar cleanup issue). The user's part is safe and we
 * lead with "Done —" rather than the alarming "Paused —".
 *
 * "failed" NEVER gets the "Done" prefix — a real failure at 99% (thermal
 * runaway, motion fault) must not read as good news. The cleanup-failure
 * nuance for failed prints lives in the issues card's context_note instead.
 *
 * For all other contexts (or when context is absent) the existing phase-based
 * prefix applies: "Paused —" for paused, "Failed —" for failed.
 *
 * Exported so the test suite can drive it in isolation.
 */
export function headlinePrefix(
  phase: Phase,
  jobContext: JobContext | null,
): string {
  const isPostPrint = jobContext === "finishing" || jobContext === "done";
  if (isPostPrint && phase === "paused") return "Done";
  if (phase === "paused") return "Paused";
  if (phase === "failed") return "Failed";
  return "";
}

/**
 * Derive a single lead-status line from the view-model fields.
 *
 * Rules (in priority order):
 *   1. printing + stage.text              → stage.text
 *   2. printing, no stage text            → headline
 *   3. paused + filament-runout indicator → "{prefix} — filament runout"
 *   4. paused + error cause               → "{prefix} — [CATEGORY: ]<text>"
 *   5. paused, no error                   → headline
 *   6. failed + error cause               → "{prefix} — [CATEGORY: ]<text>"
 *   7. failed, no error                   → headline
 *   8. otherwise                          → headline
 *
 * `{prefix}` is "Done" when jobContext is "finishing"/"done" (print completed
 * even though a cleanup error was latched), else "Paused"/"Failed" as before.
 *
 * Stale-only HMS: if ALL HMS entries are stale and phase is idle/finished,
 * `pickHms` still returns the stale best, but we treat an all-stale pool as
 * "no actionable cause" — headline wins. (Fresh entries always shadow stale.)
 *
 * Error cause selection:
 *   - pick highest-severity non-stale HMS entry (error > warn > info > unknown);
 *     tie-break by first occurrence. Falls back to stale pool only if all stale.
 *   - prefer print_error text over picked HMS text when both are present
 *   - prefix with category when it adds clarity (see formatCause)
 *
 * This is pure and exported so the test suite can drive it directly without
 * building a full View or touching the WS/store layer.
 */
export function statusLine(
  phase: Phase,
  stage: Stage,
  hms: HmsEntry[],
  printError: string | null,
  headline: string,
  jobContext: JobContext | null = null,
): string {
  if (phase === "printing") {
    return stage.text ?? headline;
  }

  // Select the best HMS entry and resolve the error source.
  // All-stale pool: pickHms returns the best stale entry, but we suppress it
  // for headline purposes — stale entries never drive the headline text.
  const bestHms = pickHms(hms);
  const allStale = hms.length > 0 && hms.every((e) => e.stale);

  // print_error takes priority over HMS text when present.
  // If the only HMS candidates are stale, treat as no HMS cause.
  const effectiveHms = allStale ? null : bestHms;
  const causeText: string | null = printError ?? (effectiveHms ? effectiveHms.text : null);
  const causeCategory: string = printError
    ? ""                                          // print_error has no separate category field in the view
    : (effectiveHms ? effectiveHms.category : "");

  // Check for filament runout — keyword-match on text only; category alone is
  // not sufficient (an AMS jam is not a runout). "runout" is definitive;
  // "filament" only if paired with "runout" or "empty" nearby — use simple
  // substring heuristics on the combined text.
  function isFilamentRunout(): boolean {
    const texts: string[] = [];
    if (effectiveHms) texts.push(effectiveHms.text.toLowerCase());
    if (printError) texts.push(printError.toLowerCase());
    for (const t of texts) {
      if (t.includes("runout")) return true;
      // "filament" + "empty" / " out" signals a clear runout. Deliberately
      // NOT "spool": "filament tangle on spool" is a jam, the opposite
      // remediation flow — canonical runout texts contain "runout" anyway.
      if (t.includes("filament") && (t.includes("empty") || t.includes(" out"))) {
        return true;
      }
    }
    return false;
  }

  if (phase === "paused") {
    const pfx = headlinePrefix(phase, jobContext);
    if (isFilamentRunout()) return `${pfx} — filament runout`;
    if (causeText) return `${pfx} — ${formatCause(causeCategory, causeText)}`;
    return headline;
  }

  if (phase === "failed") {
    const pfx = headlinePrefix(phase, jobContext);
    if (causeText) return `${pfx} — ${formatCause(causeCategory, causeText)}`;
    return headline;
  }

  return headline;
}

/** Normalize a slot's `memory` block. Returns null unless at least one field is
 * a non-empty string — an all-empty/absent block must NOT render as a label
 * (which would read as an anonymous blank), so it collapses to null here. */
function slotMemory(v: unknown): SlotMemory | null {
  const m = obj(v);
  if (!m) return null;
  const make = str(m.make).trim();
  const model = str(m.model).trim();
  const profile = str(m.profile).trim();
  if (!make && !model && !profile) return null;
  return { make, model, profile };
}

export function viewOf(snapshot: PrinterSnapshot | null): View {
  const empty: View = {
    phase: "unknown",
    headline: "—",
    subtitle: null,
    indicator: "indeterminate",
    progress: null,
    subtaskName: null,
    layer: null,
    totalLayers: null,
    timeLeftMin: null,
    etaTimeStr: null,
    nozzleActual: null,
    nozzleTarget: null,
    bedActual: null,
    bedTarget: null,
    chamberTemp: null,
    lightOn: null,
    fanPart: null,
    fanAux: null,
    fanChamber: null,
    speedLevel: null,
    amsPresent: false,
    ams: [],
    printError: null,
    printErrorFull: null,
    stage: { id: null, text: null },
    hms: [],
    statusLine: "—",
    jobContext: null,
    jobAnomaly: null,
  };
  if (!snapshot) return empty;
  // Tolerate `unknown`-typed stores: the WS layer hands us a merged dict.
  const s = obj(snapshot) ?? {};
  const v = { ...empty };

  // Phase — the bridge-translated root `phase`.
  const phaseRaw = s.phase;
  if (typeof phaseRaw === "string" && PHASE_MAP[phaseRaw]) v.phase = PHASE_MAP[phaseRaw];

  // Headline — the §6.2 object {title, subtitle, indicator}, rendered verbatim.
  const headline = obj(s.headline);
  if (headline && typeof headline.title === "string") {
    v.headline = headline.title;
    v.subtitle = typeof headline.subtitle === "string" ? headline.subtitle : null;
    if (typeof headline.indicator === "string") v.indicator = headline.indicator as Indicator;
  }

  const job = obj(s.job) ?? {};
  v.subtaskName = (job.subtask_name as string) ?? null;
  // §6.1: the bridge already nulls `percent` outside `printing`; trust it.
  v.progress = num(job.percent);
  v.layer = num(job.layer_num);
  v.totalLayers = num(job.total_layer_num);
  v.timeLeftMin = num(job.remaining_min);

  // ETA: only meaningful while the printer is actively printing or paused.
  // Compute from wall-clock at viewOf() time so each snapshot update moves
  // the estimate forward. toLocaleTimeString with no locale arg uses the
  // device's locale (12h vs 24h, AM/PM) automatically.
  if (
    (v.phase === "printing" || v.phase === "paused") &&
    v.timeLeftMin != null
  ) {
    const etaMs = Date.now() + v.timeLeftMin * 60_000;
    const etaDate = new Date(etaMs);
    // Guard against any clock/NaN edge case — isNaN catches invalid Dates.
    if (!isNaN(etaDate.getTime())) {
      v.etaTimeStr = etaDate.toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
    }
  }

  const temps = obj(s.temps) ?? {};
  const nozzle = obj(temps.nozzle);
  v.nozzleActual = nozzle ? num(nozzle.current_c) : null;
  v.nozzleTarget = nozzle ? num(nozzle.target_c) : null;
  const bed = obj(temps.bed);
  v.bedActual = bed ? num(bed.current_c) : null;
  v.bedTarget = bed ? num(bed.target_c) : null;
  const chamber = obj(temps.chamber);
  v.chamberTemp = chamber ? num(chamber.current_c) : null;

  const lights = obj(s.lights);
  v.lightOn = lights && typeof lights.chamber_on === "boolean" ? lights.chamber_on : null;

  // Cooling — the bridge already converts P1S 0-15 → 0-100 percent.
  const cooling = obj(s.cooling) ?? {};
  v.fanPart = num(obj(cooling.part_fan)?.percent);
  v.fanAux = num(obj(cooling.aux_fan)?.percent);
  v.fanChamber = num(obj(cooling.chamber_fan)?.percent);

  v.speedLevel = num(obj(s.print_params)?.speed_mm_s);

  // AMS — §6.1 `ams.slots[]`, each carrying a 1-based `physical_slot`.
  // `present` (§6.1.1) is hardware-attach, independent of `slots` emptying
  // during an RFID re-scan.
  const ams = obj(s.ams);
  v.amsPresent = ams?.present === true;
  const slots = ams?.slots;
  if (Array.isArray(slots)) {
    v.ams = slots.map((row, i) => {
      const r = obj(row) ?? {};
      // Tagless spools carry no human name (rfid_tray null) — the slot is
      // identified by filament TYPE + color only. Never a tag-derived name.
      return {
        physicalSlot: num(r.physical_slot) ?? i + 1,
        empty: r.state === "empty" || r.type == null,
        name: (r.type as string) ?? null,
        color: (r.color as string) ?? null,
        remainingPercent: num(r.remaining_pct),
        // Operator label (server-side). Survives an empty slot / WS gap.
        memory: slotMemory(r.memory),
      };
    });
  }

  // print_error is the §6.1 structured object; surface its human text.
  const printErr = obj(s.print_error);
  v.printError = printErr && typeof printErr.text === "string" ? printErr.text : null;
  v.printErrorFull = printErr
    ? (s.print_error as import("../api/types").PrintErrorWire & { wiki_url?: string })
    : null;

  // stage — current firmware task. Tolerate absent block (older server).
  const stageRaw = obj(s.stage);
  if (stageRaw) {
    const stageId = num(stageRaw.id);
    const stageText = typeof stageRaw.text === "string" ? stageRaw.text : null;
    v.stage = { id: stageId, text: stageText };
  }

  // hms — active health warnings. Tolerate absent or non-array block.
  const hmsRaw = s.hms;
  if (Array.isArray(hmsRaw)) {
    const entries: HmsEntry[] = [];
    for (const item of hmsRaw) {
      const entry = normaliseHms(item);
      if (entry) entries.push(entry);
    }
    v.hms = entries;
  }

  // job_context — defensive: absent → null, never synthesised.
  const jobContextRaw = s.job_context;
  if (
    jobContextRaw === "no_job" ||
    jobContextRaw === "printing" ||
    jobContextRaw === "finishing" ||
    jobContextRaw === "done"
  ) {
    v.jobContext = jobContextRaw;
  }

  // job_anomaly — normalise defensively; malformed → null.
  // Trim text before the truthy check so whitespace-only strings are rejected;
  // a space or newline passes `aText &&` but renders invisible in IssueEntry.
  const anomalyRaw = obj(s.job_anomaly);
  if (anomalyRaw) {
    const aType = typeof anomalyRaw.type === "string" ? anomalyRaw.type.trim() : null;
    const aText = typeof anomalyRaw.text === "string" ? anomalyRaw.text.trim() : null;
    const aPct = num(anomalyRaw.percent);
    if (aType && aText && aPct !== null) {
      v.jobAnomaly = { type: aType, text: aText, percent: aPct };
    }
  }

  // statusLine — the single lead-status string for the status screen header.
  v.statusLine = statusLine(v.phase, v.stage, v.hms, v.printError, v.headline, v.jobContext);

  return v;
}
