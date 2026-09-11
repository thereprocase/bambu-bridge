/**
 * Endpoint resolver — picks the LAN URL or the remote/Tailscale URL for each
 * request, based on whether the phone is currently on a *saved* network.
 *
 * Why this exists: the bridge is reachable two ways —
 *   - remote (Tailscale): always works, anywhere, but slower; this is the
 *     `baseUrl` in the bridge store and the only guaranteed fallback.
 *   - LAN (home Wi-Fi): faster, and works even with Tailscale off, but only
 *     reachable when the phone is actually on that LAN.
 *
 * We identify "home" purely by SUBNET FINGERPRINT (USER DECISION, locked):
 * on Wi-Fi, ask expo-network for the phone's own IPv4, take its /24 prefix
 * (first three octets), and prefer LAN only if that prefix is one the user
 * has explicitly saved. No SSID, no location permission.
 *
 * Three pieces of module-level state keep this cheap and resilient:
 *   - a 15s decision cache, so we don't fire native getNetworkState/getIp
 *     calls on every single HTTP request (the status screen polls 1fps).
 *   - a 60s LAN cooldown: when a LAN request fails at the network layer
 *     (`notifyRequestFailed`), we stop preferring LAN immediately so the very
 *     next request goes remote without re-paying the LAN timeout.
 *   - a 60s REMOTE cooldown: symmetric; when a remote request fails we cool
 *     remote down so the next resolve prefers LAN if it's available.
 *   - a `lanAliveUntil` timestamp: when a LAN request SUCCEEDS we record 120s
 *     of "LAN recently worked" so the resolver keeps preferring LAN even if
 *     the Wi-Fi fingerprint probe momentarily returns false (e.g. Android
 *     briefly demotes Wi-Fi as the default network). This is the class of bug
 *     that broke the 2026-06-11 morning session.
 *
 * Both cooldowns are symmetric now — if BOTH are active simultaneously we
 * ignore them and fall back to the fingerprint preference, so there is always
 * a URL to attempt.
 */

import * as Network from "expo-network";

import { useBridgeStore } from "../store/bridge";

/** Which path a resolved URL came from — surfaced in the UI so the user can
 * see whether they're on the fast LAN path or the Tailscale fallback. */
export type ActivePath = "lan" | "remote";

const DECISION_TTL_MS = 15_000;
const LAN_COOLDOWN_MS = 60_000;
const REMOTE_COOLDOWN_MS = 60_000;
/** How long a successful LAN request extends "LAN recently worked" liveness. */
const LAN_ALIVE_MS = 120_000;

interface CachedDecision {
  url: string;
  path: ActivePath;
  at: number; // monotonic ms (uptime), see `now()`
  config: string;
}

let cached: CachedDecision | null = null;
let previousConfig = "";
function syncConfiguration(): string {
  const config = configKey();
  if (config !== previousConfig) {
    resetEndpointCache();
    previousConfig = config;
  }
  return config;
}
function configKey(): string {
  const { baseUrl, baseUrlLan, savedNetworks, pairing } = useBridgeStore.getState();
  return JSON.stringify([baseUrl, baseUrlLan, pairing?.spki, savedNetworks.map((n) => n.prefix).sort()]);
}
/** When LAN is "cooled down": monotonic ms timestamp until which we must not
 * prefer LAN. 0 = not cooled down. */
let lanCooldownUntil = 0;
/** Symmetric remote cooldown — same semantics as LAN cooldown. 0 = no cooldown. */
let remoteCooldownUntil = 0;
/** Monotonic deadline through which LAN is assumed reachable regardless of the
 * Wi-Fi fingerprint check. Populated by `notifyRequestSucceeded` on any
 * successful LAN response. 0 = not active. */
let lanAliveUntil = 0;

/** Monotonic clock — immune to wall-clock jumps (NTP sync, manual set, DST).
 * Falls back to Date.now() if performance.now is somehow unavailable. */
function now(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/** First three octets of a dotted IPv4, or null if it doesn't look like one.
 * IPv6 / link-local / malformed → null (we only fingerprint IPv4 /24s). */
export function prefixOf(ip: string | null | undefined): string | null {
  if (!ip) return null;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(ip.trim());
  if (!m) return null;
  for (let i = 1; i <= 4; i++) {
    if (Number(m[i]) > 255) return null;
  }
  return `${m[1]}.${m[2]}.${m[3]}`;
}

/**
 * Pure decision function — takes all inputs as parameters so it can be unit-
 * tested without any native modules or store singletons. `resolve()` gathers
 * the inputs and delegates here; nothing else should call the native APIs.
 *
 * Resolution order:
 *   1. Prefer LAN when `baseUrlLan` is set AND LAN is not cooled AND
 *      (fingerprintMatch OR lanAlive).
 *   2. Otherwise prefer remote.
 *   3. If the preferred side's URL is missing, use the other.
 *   4. If BOTH sides are cooled, ignore cooldowns and use fingerprint-based
 *      preference — never return null when at least one URL is configured.
 *   5. Return null only when neither URL is configured.
 */
export function decidePath(i: {
  baseUrl: string | null;
  baseUrlLan: string | null;
  fingerprintMatch: boolean;
  lanAlive: boolean;
  lanCooled: boolean;
  remoteCooled: boolean;
}): { url: string; path: ActivePath } | null {
  const { baseUrl, baseUrlLan, fingerprintMatch, lanAlive, lanCooled, remoteCooled } = i;

  // No URLs at all — caller's problem.
  if (!baseUrl && !baseUrlLan) return null;

  // When both sides are cooled, cooldowns are irrelevant — we must not leave
  // the caller without a URL.  Fall through by treating neither as cooled.
  const bothCooled = lanCooled && remoteCooled;
  const effectiveLanCooled = bothCooled ? false : lanCooled;
  const effectiveRemoteCooled = bothCooled ? false : remoteCooled;

  // LAN-eligible: configured + not cooled + (fingerprint OR recently alive).
  const lanEligible = !!baseUrlLan && !effectiveLanCooled && (fingerprintMatch || lanAlive);
  // Remote-eligible: configured + not cooled.
  const remoteEligible = !!baseUrl && !effectiveRemoteCooled;

  if (lanEligible) {
    // Primary: LAN. If baseUrlLan is somehow empty fall to remote.
    return { url: baseUrlLan!, path: "lan" };
  }

  if (remoteEligible) {
    return { url: baseUrl!, path: "remote" };
  }

  // Both cooled (or just one is available and it's cooled but nothing else
  // exists) — last-resort: return whatever URL we have, fingerprint-preferred.
  if (baseUrlLan && (fingerprintMatch || lanAlive)) {
    return { url: baseUrlLan, path: "lan" };
  }
  if (baseUrl) return { url: baseUrl, path: "remote" };
  // baseUrlLan only, no fingerprint/alive, still better than nothing.
  return { url: baseUrlLan!, path: "lan" };
}

/** Resolve the base URL to use for the next request. Caches the decision for
 * 15s so back-to-back requests (and the 1fps camera poll) don't each hit the
 * native network APIs. */
export async function resolveBaseUrl(): Promise<string> {
  return (await resolve()).url;
}

/** Like `resolveBaseUrl` but also returns which path was chosen — used by the
 * Settings/Status UI to show "via LAN" / "via Tailscale". */
export async function resolveEndpoint(): Promise<{ url: string; path: ActivePath }> {
  const d = await resolve();
  return { url: d.url, path: d.path };
}

async function resolve(): Promise<CachedDecision> {
  const config = syncConfiguration();
  const t = now();
  if (cached && cached.config === config && t - cached.at < DECISION_TTL_MS) return cached;

  const { baseUrl, baseUrlLan, savedNetworks } = useBridgeStore.getState();

  // Decide LAN-eligibility. Any failure in the native probes (permission
  // quirk, airplane-mode race) falls through to remote — never throw out of
  // the resolver, a request must always get *some* URL to try.
  let fingerprintMatch = false;
  if (baseUrlLan && (savedNetworks.length > 0 || useBridgeStore.getState().pairing)) {
    try {
      const state = await Network.getNetworkStateAsync();
      if (state.type === Network.NetworkStateType.WIFI) {
        const ip = await Network.getIpAddressAsync();
        const prefix = prefixOf(ip);
        if (useBridgeStore.getState().pairing ||
            (prefix && savedNetworks.some((n) => n.prefix === prefix))) {
          fingerprintMatch = true;
        }
      }
    } catch {
      fingerprintMatch = false;
    }
  }

  // Settings may have changed while the native network probe was pending.
  if (configKey() !== config) return resolve();
  const lanAlive = t < lanAliveUntil;
  const lanCooled = t < lanCooldownUntil;
  const remoteCooled = t < remoteCooldownUntil;

  const result = decidePath({
    baseUrl: baseUrl || null,
    baseUrlLan: baseUrlLan || null,
    fingerprintMatch,
    lanAlive,
    lanCooled,
    remoteCooled,
  });

  // decidePath returns null only when neither URL is configured; fall through
  // with an empty string so the caller surfaces "not configured" to the user.
  const decision: CachedDecision = result
    ? { url: result.url, path: result.path, at: t, config }
    : { url: baseUrl || baseUrlLan || "", path: baseUrl ? "remote" : "lan", at: t, config };

  cached = decision;
  return decision;
}

/**
 * Tell the resolver a request against `urlUsed` failed at the network layer.
 * Cools whichever configured origin matches `urlUsed` (LAN or remote) for
 * 60s and drops the cached decision so the next resolve picks the other side
 * immediately. If both sides are configured and both would be cooled the
 * resolver ignores cooldowns (see `decidePath`) so there's always a URL.
 */
export function notifyRequestFailed(urlUsed: string): void {
  syncConfiguration();
  const { baseUrl, baseUrlLan } = useBridgeStore.getState();
  const t = now();
  if (baseUrlLan && sameOrigin(urlUsed, baseUrlLan)) {
    lanCooldownUntil = t + LAN_COOLDOWN_MS;
    cached = null; // force re-resolve → remote on the very next request
  } else if (baseUrl && sameOrigin(urlUsed, baseUrl)) {
    remoteCooldownUntil = t + REMOTE_COOLDOWN_MS;
    cached = null; // force re-resolve → LAN on the very next request
  }
}

/**
 * Tell the resolver a request against `urlUsed` succeeded. Clears the
 * cooldown for that origin. If the success was on the LAN origin, also
 * records `lanAliveUntil` (120s) so LAN stays preferred even if Android
 * temporarily demotes Wi-Fi as the default network. If the cache currently
 * points at remote but LAN just succeeded, drop the cache so the next
 * resolve re-evaluates and flips back to LAN immediately.
 */
export function notifyRequestSucceeded(urlUsed: string): void {
  syncConfiguration();
  const { baseUrl, baseUrlLan } = useBridgeStore.getState();
  const t = now();
  if (baseUrlLan && sameOrigin(urlUsed, baseUrlLan)) {
    lanCooldownUntil = 0;
    lanAliveUntil = t + LAN_ALIVE_MS;
    // If cached decision was remote, drop it so the next resolve can flip to LAN.
    if (cached && cached.path === "remote") cached = null;
  } else if (baseUrl && sameOrigin(urlUsed, baseUrl)) {
    remoteCooldownUntil = 0;
  }
}

/**
 * Given the URL a failed request used, return the OTHER configured base URL
 * (LAN → remote, remote → LAN) or null if none/same origin. Used by
 * client.ts and ws/live.ts to perform a one-shot fallback retry.
 */
export function otherUrlFor(urlUsed: string): string | null {
  const { baseUrl, baseUrlLan } = useBridgeStore.getState();
  if (baseUrlLan && sameOrigin(urlUsed, baseUrlLan)) {
    // Used LAN — other is remote (if it's a different origin).
    if (baseUrl && !sameOrigin(urlUsed, baseUrl)) return baseUrl;
    return null;
  }
  if (baseUrl && sameOrigin(urlUsed, baseUrl)) {
    // Used remote — other is LAN (if configured and different).
    if (baseUrlLan && !sameOrigin(urlUsed, baseUrlLan)) return baseUrlLan;
    return null;
  }
  return null;
}

/** True if both URLs share scheme+host+port. We compare origin rather than
 * the full string so a trailing-slash or path difference doesn't make a LAN
 * failure look like a remote one. */
export function sameOrigin(a: string, b: string): boolean {
  const oa = originOf(a);
  const ob = originOf(b);
  return oa !== null && oa === ob;
}

function originOf(url: string): string | null {
  // RN's URL is reliable for http(s); guard anyway so a malformed stored URL
  // can't throw out of notifyRequestFailed mid-request.
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}`;
  } catch {
    return null;
  }
}

/** Test/diagnostic hook — clears the cached decision, both cooldowns, and the
 * LAN-alive timestamp so a fresh probe re-evaluates immediately (used after
 * the user saves/removes a network or edits a URL in Settings). */
export function resetEndpointCache(): void {
  cached = null;
  lanCooldownUntil = 0;
  remoteCooldownUntil = 0;
  lanAliveUntil = 0;
}
