// api.js — the authed fetch wrapper and the §2 error-envelope parser.
//
// Build-free, dependency-free. The browser loads this ES module directly.
//
// Responsibilities (per dev-notes/frontend-arch-spec §6, frontend-ux-spec §0):
//   - Inject `Authorization: Bearer <key>` on every /api/v1/* fetch.
//   - Provide ?token= helpers for <img> (camera) and WebSocket, where headers
//     cannot be set.
//   - Parse the universal §2 error envelope into a single normalized shape.
//   - The auth interceptor: bounce to the Key screen ONLY on a true bridge-key
//     failure (401/auth_invalid|auth_missing). 403/printer_auth_failed and
//     403/printer_cert_changed are PRINTER problems and must NOT bounce —
//     they key off the `error` enum, never the bare HTTP status. 503/
//     auth_not_configured is its own screen.
//
// This module holds NO state of its own beyond reading localStorage; the
// interceptor is wired by main.js via setAuthHandlers() so api.js has no hard
// dependency on the router.

// ── storage keys (the localStorage contract) ───────────────────────────────
export const LS_KEY = 'bbl.apiKey';
export const LS_BASE = 'bbl.baseUrl';

/**
 * The bridge key currently stored in localStorage (or '' if none).
 * @returns {string}
 */
export function getKey() {
  return localStorage.getItem(LS_KEY) || '';
}

/**
 * Persist (or clear, when falsy) the bridge key.
 * @param {string} key
 */
export function setKey(key) {
  if (key) localStorage.setItem(LS_KEY, key);
  else localStorage.removeItem(LS_KEY);
}

/**
 * The configured base URL. Empty string => same origin (the happy path: the
 * SPA is served BY the bridge). Settings exposes this for the rare cross-bridge
 * case. Trailing slash is stripped.
 * @returns {string}
 */
export function getBaseUrl() {
  const v = (localStorage.getItem(LS_BASE) || '').trim();
  return v.replace(/\/+$/, '');
}

/** @param {string} url */
export function setBaseUrl(url) {
  const v = (url || '').trim().replace(/\/+$/, '');
  if (v) localStorage.setItem(LS_BASE, v);
  else localStorage.removeItem(LS_BASE);
}

/** Origin to use for API calls: configured base, else this page's origin. */
function apiOrigin() {
  return getBaseUrl() || location.origin;
}

/** Absolute /api/v1 base, e.g. "https://host:8080/api/v1". */
export function apiBase() {
  return apiOrigin() + '/api/v1';
}

/**
 * Build a full /api/v1 URL with `?token=<key>` appended — for <img> src and
 * any place a Bearer header cannot be set. Master key only (the WS/camera
 * accept the master key via ?token; the viz token is NOT used here).
 * @param {string} path  e.g. "/printers/123/camera/snapshot.jpg"
 * @param {Record<string,string|number>} [params] extra query params
 * @returns {string}
 */
export function tokenUrl(path, params) {
  const u = new URL(apiBase() + path, location.href);
  u.searchParams.set('token', getKey());
  if (params) for (const k of Object.keys(params)) u.searchParams.set(k, String(params[k]));
  return u.toString();
}

/**
 * WebSocket URL for the status stream, with ws(s):// derived from the API
 * origin and the master key passed as ?token=.
 * @param {string} printerId
 * @returns {string}
 */
export function wsStatusUrl(printerId) {
  const httpUrl = tokenUrl(`/printers/${encodeURIComponent(printerId)}/status`);
  return httpUrl.replace(/^http/, 'ws');
}

// ── auth interceptor wiring (set by main.js) ────────────────────────────────
/** @type {{onKeyFailure?:Function,onAuthNotConfigured?:Function,onDevMode?:()=>boolean}} */
const _handlers = {};

/**
 * Wire the interceptor callbacks. main.js calls this once at boot so api.js
 * stays decoupled from the router.
 * @param {object} h
 * @param {() => void} [h.onKeyFailure]        bridge-key rejected -> Key screen
 * @param {() => void} [h.onAuthNotConfigured] bridge has no key set -> own screen
 * @param {() => boolean} [h.onDevMode]        returns true if Dev mode is on (gate _raw)
 */
export function setAuthHandlers(h) {
  Object.assign(_handlers, h);
}

function devModeOn() {
  try { return !!(_handlers.onDevMode && _handlers.onDevMode()); }
  catch { return false; }
}

// ── the normalized result shape ─────────────────────────────────────────────
/**
 * @typedef {Object} ApiResult
 * @property {boolean} ok          true on 2xx
 * @property {number}  status      HTTP status (0 on network failure)
 * @property {*}       [data]      parsed JSON body on success (or null for 204)
 * @property {string}  [error]     §2 enum string on failure (e.g. 'printer_auth_failed')
 * @property {string}  [message]   user-facing sentence (render VERBATIM)
 * @property {string}  [remediation_hint] menu-path remediation (render VERBATIM)
 * @property {string}  [likely_cause]
 * @property {Object}  [context]
 * @property {Array}   [issues]    422 validation issues
 * @property {Array}   [actions]   e.g. cert-changed actions[]
 * @property {Object}  [discovered] onboarding: {serial, model}
 * @property {string}  [existing_printer_id]
 * @property {Array}   [active_job_ids]
 * @property {Object}  [_raw]      dev-mode only
 * @property {boolean} [network]   true when the fetch itself threw (no bridge)
 */

/**
 * Parse a non-2xx body into the normalized failure shape. The bridge owns all
 * copy; we never invent `message`/`remediation_hint`. `_raw` is surfaced only
 * when Dev mode is on (the bridge gates whether it's even on the wire; this is
 * the client-side belt-and-braces so it never leaks by default).
 * @param {number} status
 * @param {*} body
 * @returns {ApiResult}
 */
export function parseEnvelope(status, body) {
  const b = body && typeof body === 'object' ? body : {};
  /** @type {ApiResult} */
  const r = {
    ok: false,
    status,
    error: b.error || statusFallbackError(status),
    message: b.message || defaultMessage(status),
  };
  if (b.remediation_hint) r.remediation_hint = b.remediation_hint;
  if (b.likely_cause) r.likely_cause = b.likely_cause;
  if (b.context) r.context = b.context;
  if (Array.isArray(b.issues)) r.issues = b.issues;
  if (Array.isArray(b.actions)) r.actions = b.actions;
  if (b.discovered) r.discovered = b.discovered;
  if (b.existing_printer_id) r.existing_printer_id = b.existing_printer_id;
  if (Array.isArray(b.active_job_ids)) r.active_job_ids = b.active_job_ids;
  if (b._raw && devModeOn()) r._raw = b._raw;
  return r;
}

function statusFallbackError(status) {
  if (status === 401) return 'auth_invalid';
  if (status === 403) return 'auth_invalid';
  if (status === 404) return 'not_found';
  if (status === 409) return 'conflict';
  if (status === 422) return 'invalid_input';
  if (status === 503) return 'auth_not_configured';
  return 'internal_error';
}

function defaultMessage(status) {
  if (status === 401) return 'The API key was rejected.';
  if (status === 503) return 'This bridge is not set up yet.';
  if (status >= 500) return 'The bridge hit an error.';
  return 'Something went wrong.';
}

// ── the interceptor: does this failure mean "re-enter the BRIDGE key"? ──────
/**
 * TRUE only for a genuine bridge-Bearer-key failure. The errors.py status map
 * collapses BOTH 401 and 403 to `auth_invalid`, so we branch on STATUS for the
 * generic case and explicitly exempt the printer-side 403 enums. Documented as
 * load-bearing in arch-spec §6.3.
 * @param {ApiResult} r
 * @returns {boolean}
 */
export function isBridgeKeyFailure(r) {
  // printer-side 403s are never a bridge-key problem
  if (r.error === 'printer_auth_failed' || r.error === 'printer_cert_changed') return false;
  // a real bridge-key failure is 401 (auth_missing / auth_invalid)
  if (r.status === 401) return true;
  // a 403 that is NOT a printer enum (e.g. raw_gcode_disabled) is also not a
  // key problem; only 401 bounces. (403 with no printer enum is feature-gating.)
  return false;
}

/** @param {ApiResult} r */
function runInterceptor(r) {
  if (r.error === 'auth_not_configured' || r.status === 503) {
    _handlers.onAuthNotConfigured && _handlers.onAuthNotConfigured();
    return;
  }
  if (isBridgeKeyFailure(r)) {
    _handlers.onKeyFailure && _handlers.onKeyFailure();
  }
}

// ── the core request ────────────────────────────────────────────────────────
/**
 * Authed fetch against /api/v1. Always resolves to an ApiResult (never throws
 * for HTTP errors); only a programming error would throw. Network failures
 * resolve to {ok:false, status:0, network:true}.
 *
 * @param {string} path  path under /api/v1, e.g. "/printers"
 * @param {RequestInit & {auth?:boolean, raw?:boolean}} [opts]
 *        auth=false skips the Bearer header (for /health). raw=true returns the
 *        raw Response instead of JSON (rare; used for blob downloads).
 * @returns {Promise<ApiResult|Response>}
 */
export async function api(path, opts = {}) {
  const { auth = true, raw = false, headers, ...rest } = opts;
  const h = new Headers(headers || {});
  if (auth) {
    const key = getKey();
    if (key) h.set('Authorization', `Bearer ${key}`);
  }
  let res;
  try {
    res = await fetch(apiBase() + path, { ...rest, headers: h });
  } catch (netErr) {
    return { ok: false, status: 0, network: true, error: 'network',
      message: "Can't reach the bridge.", _netErr: String(netErr) };
  }
  if (raw) return res;

  if (res.status === 204) return { ok: true, status: 204, data: null };

  let body = null;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    body = await res.json().catch(() => null);
  } else {
    // some errors may arrive as text/plain on a panic path
    const txt = await res.text().catch(() => '');
    body = txt ? { message: txt } : null;
  }

  if (res.ok) return { ok: true, status: res.status, data: body };

  const r = parseEnvelope(res.status, body);
  runInterceptor(r);
  return r;
}

// ── the two-call Test (health then printers) ────────────────────────────────
/**
 * @typedef {Object} TestResult
 * @property {'connected'|'key_rejected'|'not_configured'|'no_bridge'
 *           |'wrong_address'|'bridge_error'} outcome
 * @property {number} [printerCount]   on 'connected'
 * @property {Array}  [printers]       on 'connected'
 * @property {ApiResult} [detail]      the underlying failure result
 */

/**
 * Reproduces the onboarding Step-1 / Settings Test flow (frontend-ux-spec §1,
 * the status-line table). Two calls in order:
 *   1) GET /health  (no auth) — is the bridge reachable here?
 *   2) GET /printers (Bearer) — is the key accepted, and how many printers?
 *
 * Returns a single discriminated outcome the caller maps to a status line.
 * @returns {Promise<TestResult>}
 */
export async function testConnection() {
  const health = await api('/health', { auth: false });
  if (health.network) return { outcome: 'no_bridge', detail: health };
  if (health.status === 503 || health.error === 'auth_not_configured') {
    return { outcome: 'not_configured', detail: health };
  }
  // something answered but isn't the bridge (non-2xx, non-503 on /health)
  if (!health.ok) return { outcome: 'wrong_address', detail: health };

  const printers = await api('/printers');
  if (printers.network) return { outcome: 'no_bridge', detail: printers };
  if (printers.status === 503 || printers.error === 'auth_not_configured') {
    return { outcome: 'not_configured', detail: printers };
  }
  if (printers.status === 401) return { outcome: 'key_rejected', detail: printers };
  if (printers.status >= 500) return { outcome: 'bridge_error', detail: printers };
  if (!printers.ok) return { outcome: 'bridge_error', detail: printers };

  const list = Array.isArray(printers.data) ? printers.data : [];
  return { outcome: 'connected', printerCount: list.length, printers: list };
}

// ── convenience JSON POST/PATCH helpers ─────────────────────────────────────
/** @param {string} path @param {*} bodyObj @param {RequestInit} [opts] */
export function postJson(path, bodyObj, opts = {}) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    body: bodyObj === undefined ? undefined : JSON.stringify(bodyObj),
    ...opts,
  });
}

/** @param {string} path @param {*} bodyObj */
export function patchJson(path, bodyObj) {
  return api(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(bodyObj),
  });
}

/** @param {string} path @param {RequestInit} [opts] */
export function del(path, opts = {}) {
  return api(path, { method: 'DELETE', ...opts });
}
