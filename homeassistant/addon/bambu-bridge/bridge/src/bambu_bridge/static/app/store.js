// store.js — the reactive per-printer state cache.
//
// Build-free, dependency-free. No framework. Just: a state object, a
// subscribe/notify set, the deep-merge the WS delta contract demands, a
// localStorage cold-load cache, and a phase/headline-derived view model the
// dashboard renders from (NEVER from raw mc_percent).
//
// Load-bearing rules baked in here (frontend-ux-spec §9, contract §5.3/§6.0.1):
//   - Deltas DEEP-MERGE into the cached snapshot. We never expose a delta
//     alone — `current(id)` always returns the full merged shape.
//   - On reconnect, the next snapshot REPLACES the cache (reseed).
//   - Arrays replace wholesale on merge (per contract). The §6.1.1 AMS
//     re-scan "hold last slots" policy is a VIEW concern, applied here in the
//     view model, not a merge concern.
//   - The view model renders headline.{title,subtitle,indicator} verbatim and
//     gates progress on `phase` — preparing => indeterminate, no percent.

// ── shape ───────────────────────────────────────────────────────────────────
/**
 * @typedef {Object} PrinterEntry
 * @property {Object|null} snapshot        full merged translated state (§6)
 * @property {boolean} connected           WS open AND fresh
 * @property {string|null} lastTelemetryAt ISO; from session.last_telemetry_at
 * @property {Array<{physical_slot:number,type:string,color:string}>|null} lastNonEmptySlots
 */

const state = {
  /** @type {Record<string, PrinterEntry>} */
  printers: {},
  /** @type {Array} the GET /printers summary list (authoritative on the bridge) */
  printerList: [],
  /** id of the printer the dashboard is currently showing */
  currentPrinterId: null,
};

const subs = new Set();

/**
 * Subscribe to any store change. Returns an unsubscribe function.
 * @param {(state:typeof state)=>void} fn
 * @returns {() => void}
 */
export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

function notify() {
  for (const fn of subs) {
    try { fn(state); } catch (e) { console.error('store subscriber threw', e); }
  }
}

/** @returns {typeof state} the live state object (read-only by convention) */
export function getState() { return state; }

/** @param {string|null} id */
export function setCurrentPrinter(id) {
  if (state.currentPrinterId === id) return;
  state.currentPrinterId = id;
  notify();
}

/** @param {Array} list the GET /printers summary array */
export function setPrinterList(list) {
  state.printerList = Array.isArray(list) ? list : [];
  notify();
}

/** @param {string} id @returns {PrinterEntry} */
function entry(id) {
  if (!state.printers[id]) {
    state.printers[id] = {
      snapshot: null, connected: false, lastTelemetryAt: null, lastNonEmptySlots: null,
    };
  }
  return state.printers[id];
}

/** @param {string} id @returns {Object|null} the full merged snapshot */
export function current(id) {
  const e = state.printers[id];
  return e ? e.snapshot : null;
}

// ── deep-merge (arrays replace wholesale per contract §5.3) ─────────────────
/**
 * @param {*} base
 * @param {*} patch
 * @returns {*}
 */
export function deepMerge(base, patch) {
  if (Array.isArray(patch)) return patch;            // arrays replace wholesale
  if (patch && typeof patch === 'object') {
    const out = base && typeof base === 'object' && !Array.isArray(base) ? { ...base } : {};
    for (const k of Object.keys(patch)) out[k] = deepMerge(out[k], patch[k]);
    return out;
  }
  return patch;
}

// ── snapshot / delta ingestion ──────────────────────────────────────────────
/**
 * Seed (or reseed on reconnect) the full snapshot. Discards any merged cache.
 * @param {string} id
 * @param {Object} snapshot translated state (§6)
 */
export function applySnapshot(id, snapshot) {
  const e = entry(id);
  e.snapshot = snapshot || null;
  e.connected = true;
  _touchFromSnapshot(e, snapshot);
  _rememberSlots(e, snapshot);
  cacheToStorage(id, snapshot);
  notify();
}

/**
 * Deep-merge a delta into the cached snapshot. Never call this without a prior
 * snapshot in the same connection — but if it happens (defensive), we still
 * merge into {} so we don't crash; the dashboard's connected gate keeps stale
 * partials from being shown as live.
 * @param {string} id
 * @param {Object} delta translated changed leaves
 */
export function applyDelta(id, delta) {
  const e = entry(id);
  e.snapshot = deepMerge(e.snapshot || {}, delta || {});
  e.connected = true;
  _touchFromSnapshot(e, e.snapshot);
  _rememberSlots(e, e.snapshot);
  cacheToStorage(id, e.snapshot);
  notify();
}

/**
 * Mark the printer's live link state. When false, the dashboard greys/hides
 * live numbers and stamps "last update Ns ago". Does not discard the cache.
 * @param {string} id
 * @param {boolean} connected
 */
export function setConnected(id, connected) {
  const e = entry(id);
  if (e.connected === connected) return;
  e.connected = connected;
  notify();
}

function _touchFromSnapshot(e, snap) {
  const t = snap && snap.session && snap.session.last_telemetry_at;
  if (t) e.lastTelemetryAt = t;
}

function _rememberSlots(e, snap) {
  const ams = snap && snap.ams;
  if (ams && Array.isArray(ams.slots) && ams.slots.length > 0) {
    e.lastNonEmptySlots = ams.slots;
  }
}

// ── localStorage cold-load cache (bbl.snapshot.{id}) ────────────────────────
const SNAP_PREFIX = 'bbl.snapshot.';

/** @param {string} id @param {Object|null} snap */
export function cacheToStorage(id, snap) {
  if (!snap) return;
  try { localStorage.setItem(SNAP_PREFIX + id, JSON.stringify(snap)); }
  catch { /* quota / private mode — non-fatal */ }
}

/**
 * Cold-load: render last-known immediately at open with connected:false so
 * there's no 3-second blank dashboard (frontend-ux-spec §7.2). The next WS
 * snapshot brightens/replaces it.
 * @param {string} id
 * @returns {Object|null}
 */
export function loadCachedSnapshot(id) {
  try {
    const raw = localStorage.getItem(SNAP_PREFIX + id);
    if (!raw) return null;
    const snap = JSON.parse(raw);
    const e = entry(id);
    e.snapshot = snap;
    e.connected = false;          // cold cache is never "live"
    _touchFromSnapshot(e, snap);
    _rememberSlots(e, snap);
    notify();
    return snap;
  } catch { return null; }
}

/** @param {string} id remove the cached snapshot (e.g. printer deleted) */
export function clearCachedSnapshot(id) {
  try { localStorage.removeItem(SNAP_PREFIX + id); } catch { /* */ }
}

// ── the view model the dashboard reads ──────────────────────────────────────
/**
 * @typedef {Object} ViewModel
 * @property {boolean} hasData
 * @property {boolean} connected
 * @property {string|null} lastTelemetryAt
 * @property {string} statusTitle      headline.title (or disconnected override)
 * @property {string} statusSubtitle   headline.subtitle (or "Last update Ns ago")
 * @property {string} phase            idle|preparing|printing|paused|completed|failed|unknown
 * @property {'progress'|'indeterminate'|'amber'|'green'|'red'|'none'} indicator
 * @property {boolean} showPercent     true ONLY when phase==='printing'
 * @property {number|null} percent     job.percent (null unless printing)
 * @property {number|null} remainingMin
 * @property {number|null} layerNum
 * @property {number|null} totalLayer
 * @property {string|null} subtaskName
 * @property {Object} temps            {nozzle,bed,chamber} {current_c,target_c}
 * @property {Object} cooling
 * @property {boolean} lightOn
 * @property {Object} ams              snapshot.ams plus `displaySlots` + `rescanning`
 */

/**
 * Build the dashboard's render-ready view model. This is the ONE place the
 * phase rule and the disconnected-headline override live.
 * @param {string} id
 * @returns {ViewModel}
 */
export function viewModel(id) {
  const e = state.printers[id];
  const snap = e ? e.snapshot : null;
  const connected = !!(e && e.connected);
  const lastTelemetryAt = e ? e.lastTelemetryAt : null;

  if (!snap) {
    return {
      hasData: false, connected, lastTelemetryAt,
      statusTitle: 'Connecting…', statusSubtitle: 'Loading status from your printer',
      phase: 'unknown', indicator: 'indeterminate', showPercent: false,
      percent: null, remainingMin: null, layerNum: null, totalLayer: null,
      subtaskName: null, temps: {}, cooling: {}, lightOn: false,
      ams: { present: false, slots: [], displaySlots: [], rescanning: false },
    };
  }

  const phase = snap.phase || 'unknown';
  const job = snap.job || {};
  const headline = snap.headline || {};

  // disconnected-headline override (contract §6.2): when not connected, the
  // headline doesn't reflect the phase — it reflects the dropout.
  let statusTitle, statusSubtitle, indicator;
  if (!connected) {
    statusTitle = 'Reconnecting…';
    statusSubtitle = lastTelemetryAt ? `Last update ${secsAgo(lastTelemetryAt)}s ago` : 'Reconnecting…';
    indicator = 'none';
  } else {
    statusTitle = headline.title || titleForPhase(phase);
    statusSubtitle = headline.subtitle || '';
    indicator = headline.indicator || indicatorForPhase(phase);
  }

  // the phase rule: a percentage progress bar ONLY when printing.
  const showPercent = connected && phase === 'printing';
  const percent = showPercent && typeof job.percent === 'number' ? job.percent : null;

  // AMS view: hold last-known slots during the present && slots==[] re-scan.
  const ams = snap.ams || { present: false, slots: [] };
  const slots = Array.isArray(ams.slots) ? ams.slots : [];
  const rescanning = !!ams.present && slots.length === 0;
  const displaySlots = rescanning && e.lastNonEmptySlots ? e.lastNonEmptySlots : slots;

  return {
    hasData: true,
    connected,
    lastTelemetryAt,
    statusTitle,
    statusSubtitle,
    phase,
    indicator,
    showPercent,
    percent,
    remainingMin: typeof job.remaining_min === 'number' ? job.remaining_min : null,
    layerNum: typeof job.layer_num === 'number' ? job.layer_num : null,
    totalLayer: job.total_layer_num || null,
    subtaskName: job.subtask_name || null,
    temps: snap.temps || {},
    cooling: snap.cooling || {},
    lightOn: !!(snap.lights && snap.lights.chamber_on),
    ams: { ...ams, slots, displaySlots, rescanning },
  };
}

// ── small helpers shared by the view model ──────────────────────────────────
/** @param {string} iso @returns {number} whole seconds since the ISO time */
export function secsAgo(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

function titleForPhase(phase) {
  return {
    idle: 'Ready', preparing: 'Preparing', printing: 'Printing',
    paused: 'Paused', completed: 'Done', failed: 'Print failed',
    unknown: 'Connecting…',
  }[phase] || 'Connecting…';
}

function indicatorForPhase(phase) {
  return {
    idle: 'none', preparing: 'indeterminate', printing: 'progress',
    paused: 'amber', completed: 'green', failed: 'red', unknown: 'indeterminate',
  }[phase] || 'none';
}
