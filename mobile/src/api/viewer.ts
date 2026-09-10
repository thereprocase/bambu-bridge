/**
 * 3D viewer URL builder.
 *
 * The bridge serves a self-contained WebGL viewer (printing part in 3D, live
 * layer progress, scrubbing, color modes) at:
 *
 *   GET {base}/printers/{printer_id}/viz?token={bearer}
 *
 * The token rides in the QUERY, not an Authorization header — embedded/browser
 * views can't set request headers, and this is the documented auth path for the
 * viz route specifically.
 *
 * `base` MUST come from the same resolver the API client uses (`resolveBaseUrl`)
 * so the viewer follows the same LAN-vs-Tailscale selection as every other
 * request — we never hardcode or bypass that path choice.
 *
 * SECURITY: the returned URL embeds the bearer. It is handed straight to the
 * browser/opener and is NEVER logged, toasted, or rendered. Callers must treat
 * it as a secret-bearing value.
 */

import { useBridgeStore } from "../store/bridge";
import { resolveBaseUrl } from "./endpoint";

/** Build the tokenized viewer URL for a printer, or null if we can't (no base
 * URL resolved, or no bearer configured). Caller decides what to do with null
 * — typically surface "open Settings". Never throws. */
export async function buildViewerUrl(printerId: string): Promise<string | null> {
  const base = await resolveBaseUrl();
  if (!base) return null;
  const { bearer } = useBridgeStore.getState();
  if (!bearer) return null;

  // Mirror client.ts buildUrl trailing-slash handling so a base stored with or
  // without a trailing "/" yields the same URL.
  const trimmed = base.replace(/\/+$/, "");
  // encodeURIComponent the token so any non-URL-safe chars survive the query.
  return `${trimmed}/printers/${encodeURIComponent(printerId)}/viz?token=${encodeURIComponent(bearer)}`;
}

/** Only the configured bridge viewer may navigate inside this credential-bearing WebView. */
export function isViewerNavigationAllowed(current: string, target: string): boolean {
  try {
    const expected = new URL(current);
    const next = new URL(target);
    return (next.protocol === "http:" || next.protocol === "https:") &&
      !next.username && !next.password && next.origin === expected.origin &&
      next.pathname === expected.pathname;
  } catch { return false; }
}

/** A page supplies numbers and one format enum, never arbitrary diagnostic text. */
export function safeVizTimings(msg: Record<string, unknown>): Record<string, number | string> {
  const out: Record<string, number | string> = {};
  for (const key of ["fetch_ms", "parse_ms", "buffers_ms", "first_frame_ms", "bytes", "segments"]) {
    const value = msg[key];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) out[key] = value;
  }
  if (msg.fmt === "bin" || msg.fmt === "json") out.fmt = msg.fmt;
  return out;
}
