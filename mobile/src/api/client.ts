/**
 * Bridge HTTP client.
 *
 * Tiny on purpose: typed `request<T>()` that adds the bearer header,
 * parses the universal envelope on non-2xx, and surfaces network failures
 * as a distinct error type. Per-endpoint helpers live in their own files
 * (`printers.ts`, `jobs.ts`, etc.) — keep this file free of route knowledge.
 *
 * baseUrl + bearer come from the bridge store and are read on each call,
 * so a settings change takes effect on the next request without restart.
 *
 * Fallback strategy (symmetric as of v0.17):
 *   For GET reads only, on BridgeNetworkError mark the primary failed and retry
 *   once against the other configured base URL (LAN↔remote). If the retry
 *   succeeds, mark that path successful and emit a `net.fallback` QA event.
 *   If the retry also fails (or no other URL exists), mark it failed too and
 *   set reachability "down" — the user needs to act (banner will appear).
 */

import { qaLog } from "../lib/qalog";
import { pairedFetch, PairingSecurityError } from "../pairing/native";
import { useBridgeStore } from "../store/bridge";
import { useNetStore, type Reach } from "../store/net";
import {
  notifyRequestFailed,
  notifyRequestSucceeded,
  otherUrlFor,
  resolveBaseUrl,
  sameOrigin,
} from "./endpoint";
import { BridgeEnvelope, BridgeError, BridgeNetworkError } from "./errors";

// Gate for success-path api.req logs. Errors always log regardless.
// Toggle on during QA sessions: import { setQaVerbose } from "./client"; setQaVerbose(true)
let QA_VERBOSE = false;
export function setQaVerbose(on: boolean) { QA_VERBOSE = on; }

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  method?: Method;
  body?: unknown;            // JSON-serialized for non-GET
  query?: Record<string, string | number | undefined | null>;
  /** Override timeout (ms). Default 15s — long enough for the bridge's
   * MQTT-watchdog phases on register, short enough that the UI doesn't
   * appear hung if the LAN is dead. */
  timeoutMs?: number;
  /** Send `Accept: application/octet-stream` and return the raw bytes
   * (used for camera snapshots). Errors still envelope through. */
  rawBytes?: boolean;
}

export interface RawResponse {
  status: number;
  bytes: ArrayBuffer;
  contentType: string;
}

function buildUrl(base: string, path: string, query?: RequestOptions["query"]): string {
  const trimmed = base.replace(/\/+$/, "");
  let url = `${trimmed}${path.startsWith("/") ? path : "/" + path}`;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null) params.set(k, String(v));
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  return url;
}

/** Derive a human-readable path label from a base URL — "lan" if it matches
 * the stored LAN base, "remote" otherwise. Never exposes the URL itself. */
function pathLabel(base: string): "lan" | "remote" {
  const { baseUrlLan } = useBridgeStore.getState();
  return baseUrlLan && sameOrigin(base, baseUrlLan) ? "lan" : "remote";
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  // Resolve LAN-vs-remote once per request. The resolver caches its decision
  // for 15s so this stays cheap under the 1fps camera poll.
  const base = await resolveBaseUrl();
  if (!base) {
    throw new Error("bridge baseUrl not configured — open Settings");
  }

  try {
    const result = await requestVia<T>(base, path, opts);
    // Success on the primary path.
    notifyRequestSucceeded(base);
    reportReach(base);
    return result;
  } catch (e) {
    if (e instanceof PairingSecurityError) throw e;
    // Network-layer failure (not an HTTP error the bridge sent) → try the
    // other configured URL exactly once. HTTP 4xx/5xx errors don't trigger
    // fallback — they came from the bridge successfully, meaning the path
    // itself is fine and the request was the problem.
    if (e instanceof BridgeNetworkError) {
      notifyRequestFailed(base);
      // A lost response does not prove a command was rejected. Replaying a
      // print/jog/upload over the other path can execute it twice.
      const other = otherUrlFor(base);
      if ((opts.method ?? "GET") !== "GET") {
        if (!other) useNetStore.getState().setReach("down");
        throw e;
      }
      if (other) {
        try {
          const result = await requestVia<T>(other, path, opts);
          // Fallback succeeded.
          notifyRequestSucceeded(other);
          reportReach(other);
          // SECURITY: log path labels only — never URLs, hosts, or tokens.
          qaLog("net.fallback", {
            from: pathLabel(base),
            to: pathLabel(other),
            path,
          });
          return result;
        } catch (e2) {
          if (e2 instanceof BridgeNetworkError) {
            // Both paths failed at the network layer — genuinely unreachable.
            notifyRequestFailed(other);
            useNetStore.getState().setReach("down");
          } else {
            // HTTP error via the fallback path — the bridge answered, so the
            // path is alive; only this request failed.
            notifyRequestSucceeded(other);
            reportReach(other);
          }
          throw e2;
        }
      }
      // Network failure with no other URL configured to try.
      useNetStore.getState().setReach("down");
      throw e;
    }
    // HTTP error from the bridge on the primary path — reachable; the
    // request itself was the problem. Never show the "down" banner for this.
    notifyRequestSucceeded(base);
    reportReach(base);
    throw e;
  }
}

/**
 * Probe a *specific* base URL, bypassing the LAN/remote resolver. Used by
 * Settings to test the LAN URL on its own (so the user gets a per-field
 * result) regardless of which network they're currently on. Returns the
 * parsed body or throws the same BridgeError/BridgeNetworkError as request().
 */
export async function requestExact<T>(
  base: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  if (!base) throw new Error("no base URL to probe");
  return requestVia<T>(base, path, opts);
}

/** Perform the actual HTTP call against an already-resolved base URL. */
async function requestVia<T>(
  base: string,
  path: string,
  opts: RequestOptions,
): Promise<T> {
  const { bearer } = useBridgeStore.getState();

  const url = buildUrl(base, path, opts.query);
  const method = opts.method ?? "GET";
  const t0 = Date.now();
  const headers: Record<string, string> = {
    Accept: opts.rawBytes ? "application/octet-stream" : "application/json",
  };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), opts.timeoutMs ?? 15_000);

  // The timeout covers headers AND the body; a stalled body must not leave
  // camera polling or controls waiting forever.
  try {
    const response = useBridgeStore.getState().pairing
      ? await pairedFetch(url, method, headers, body, opts.timeoutMs ?? 15_000,
        !!opts.rawBytes, controller.signal)
      : await fetch(url, { method, headers, body, signal: controller.signal });
    if (response.status === 204) return undefined as T;
    if (!response.ok) {
      const envelope = await safeEnvelope(response);
      qaLog("api.error", { path, method, status: response.status, code: envelope.error, ms: Date.now() - t0 });
      throw new BridgeError(envelope, response.status);
    }
    if (opts.rawBytes) {
      const bytes = await response.arrayBuffer();
      if (QA_VERBOSE) qaLog("api.req", { path, method, status: response.status, ms: Date.now() - t0 });
      return { status: response.status, bytes,
        contentType: response.headers.get("content-type") ?? "application/octet-stream" } as T;
    }
    const text = await response.text();
    if (QA_VERBOSE) qaLog("api.req", { path, method, status: response.status, ms: Date.now() - t0 });
    if (!text) return undefined as T;
    try { return JSON.parse(text) as T; }
    catch {
      throw new BridgeError({ error: "internal_error", message: "The bridge returned an unreadable response." }, response.status);
    }
  } catch (e) {
    if (e instanceof BridgeError || e instanceof PairingSecurityError) throw e;
    qaLog("api.error", { path, method, status: 0, code: "network_error", ms: Date.now() - t0 });
    throw new BridgeNetworkError(method !== "GET");
  } finally {
    clearTimeout(t);
  }
}

/** Report current path to the net reachability store — cheap, no I/O. */
function reportReach(base: string): void {
  const label: Reach = pathLabel(base);
  useNetStore.getState().setReach(label);
}

async function safeEnvelope(r: Response): Promise<BridgeEnvelope> {
  // The bridge always envelopes errors per contract §2 — but if a
  // misconfigured proxy in front of us returns a plaintext 502, we
  // synthesise something usable rather than crash the JSON.parse.
  try {
    const text = await r.text();
    if (!text) {
      return { error: "internal_error", message: `HTTP ${r.status}` };
    }
    const parsed = JSON.parse(text);
    if (typeof parsed === "object" && parsed && "error" in parsed) {
      return parsed as BridgeEnvelope;
    }
    return {
      error: "internal_error",
      message: `Unexpected response (HTTP ${r.status})`,
      _raw: { body: text.slice(0, 500) },
    };
  } catch (e) {
    return {
      error: "internal_error",
      message: `Couldn't parse bridge response (HTTP ${r.status})`,
      _raw: { parseError: String(e) },
    };
  }
}
