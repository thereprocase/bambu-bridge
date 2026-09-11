/**
 * Bridge connection config — base URL + bearer + last health probe.
 *
 * baseUrl persists to MMKV (non-secret, may show in screenshots).
 * bearer comes from SecureStore on app launch (loadBearer()) — the
 * store holds the in-memory copy; writes go through `setBearer()` so
 * SecureStore stays the source of truth.
 *
 * Health is the result of the last GET /api/v1/health — used by the
 * settings screen to render the dot and by the root layout to decide
 * whether to bounce to onboarding when the user opens the app cold.
 */

import { create } from "zustand";

import { kv } from "../lib/kv";
import { registerQaSecret } from "../lib/qalog";
import { clearBearer, loadBearer, saveBearer } from "../lib/secrets";
import { configureTransport } from "../pairing/native";
import { type PairedProfile, secureBase } from "../pairing/protocol";
import { loadPairing, savePairing, clearPairing } from "../pairing/storage";

const KV_BASE_URL = "bridge.baseUrl";
const KV_BASE_URL_LAN = "bridge.baseUrlLan";
const KV_SAVED_NETWORKS = "bridge.savedNetworks";

/** A network the user has marked as "home" (or any trusted LAN). `prefix`
 * is the first three octets of the /24 — e.g. "192.168.1". We match on the
 * /24 because that's the broadcast domain the bridge's LAN IP lives in; a
 * phone on the same Wi-Fi gets an address in the same /24. */
export interface SavedNetwork {
  prefix: string;   // "192.168.1"
  addedAt: number;  // epoch ms
}

function loadSavedNetworks(): SavedNetwork[] {
  const raw = kv.getString(KV_SAVED_NETWORKS);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Defensive: only keep well-formed entries so a hand-edited/corrupt
    // MMKV value can't crash the resolver on every request.
    return parsed.filter(
      (e): e is SavedNetwork =>
        e && typeof e.prefix === "string" && typeof e.addedAt === "number",
    );
  } catch {
    return [];
  }
}

export type HealthState =
  | { state: "unknown" }                  // never probed
  | { state: "ok"; at: number }           // 2xx /health
  | { state: "auth_invalid"; at: number } // 401, bearer wrong
  | { state: "unreachable"; at: number; detail: string }; // network / 5xx

interface BridgeStore {
  pairing: PairedProfile | null;
  activatePairing(p: PairedProfile): Promise<void>;
  setPairedRemote(url: string): Promise<void>;
  forgetPairing(): Promise<void>;
  baseUrl: string;            // remote/Tailscale URL — the always-available fallback
  baseUrlLan: string | null;  // optional faster LAN URL, used only on a saved network
  savedNetworks: SavedNetwork[];
  bearer: string | null;
  health: HealthState;
  bootstrapped: boolean;  // SecureStore has been read at least once

  setBaseUrl(url: string): void;
  setBaseUrlLan(url: string): void;   // "" clears it
  addSavedNetwork(prefix: string): void;
  removeSavedNetwork(prefix: string): void;
  setBearer(value: string): Promise<void>;
  clearBearerAsync(): Promise<void>;
  setHealth(h: HealthState): void;
  bootstrap(): Promise<void>;
}

export const useBridgeStore = create<BridgeStore>((set, get) => ({
  pairing: null,
  activatePairing: async (p) => {
    const previous = get().pairing;
    await configureTransport(p);
    try { await savePairing(p); }
    catch (e) { await configureTransport(previous); throw e; }
    registerQaSecret(p.token);
    set({ pairing: p, baseUrl: p.remoteUrl || p.baseUrl, baseUrlLan: p.baseUrl,
      bearer: p.token, health: { state: "unknown" } });
  },
  setPairedRemote: async (url) => {
    const current = get().pairing;
    if (!current) throw new Error("Pair a bridge first.");
    await get().activatePairing({ ...current, remoteUrl: url.trim() ? secureBase(url.trim()) : undefined });
  },
  forgetPairing: async () => {
    const bearer = await loadBearer().catch(() => null);
    await clearPairing();
    await configureTransport(null);
    set({ pairing: null, baseUrl: kv.getString(KV_BASE_URL) ?? "",
      baseUrlLan: kv.getString(KV_BASE_URL_LAN) || null, bearer, health: { state: "unknown" } });
  },
  baseUrl: kv.getString(KV_BASE_URL) ?? "",
  baseUrlLan: kv.getString(KV_BASE_URL_LAN) || null,
  savedNetworks: loadSavedNetworks(),
  bearer: null,
  health: { state: "unknown" },
  bootstrapped: false,

  setBaseUrl: (url) => {
    if (get().pairing) throw new Error("Disconnect the paired bridge before using manual setup.");
    const trimmed = url.trim();
    kv.set(KV_BASE_URL, trimmed);
    set({ baseUrl: trimmed, health: { state: "unknown" } });
  },

  setBaseUrlLan: (url) => {
    if (get().pairing) throw new Error("Disconnect the paired bridge before using manual setup.");
    const trimmed = url.trim();
    if (trimmed) {
      kv.set(KV_BASE_URL_LAN, trimmed);
      set({ baseUrlLan: trimmed, health: { state: "unknown" } });
    } else {
      kv.remove(KV_BASE_URL_LAN);
      set({ baseUrlLan: null, health: { state: "unknown" } });
    }
  },

  addSavedNetwork: (prefix) => {
    const p = prefix.trim();
    if (!p) return;
    const existing = get().savedNetworks;
    if (existing.some((n) => n.prefix === p)) return; // idempotent
    const next = [...existing, { prefix: p, addedAt: Date.now() }];
    kv.set(KV_SAVED_NETWORKS, JSON.stringify(next));
    set({ savedNetworks: next });
  },

  removeSavedNetwork: (prefix) => {
    const next = get().savedNetworks.filter((n) => n.prefix !== prefix);
    kv.set(KV_SAVED_NETWORKS, JSON.stringify(next));
    set({ savedNetworks: next });
  },

  setBearer: async (value) => {
    if (get().pairing) throw new Error("Disconnect the paired bridge before using manual setup.");
    const trimmed = value.trim();
    registerQaSecret(trimmed);
    await saveBearer(trimmed);
    set({ bearer: trimmed, health: { state: "unknown" } });
  },

  clearBearerAsync: async () => {
    await clearBearer();
    set({ bearer: null, health: { state: "unknown" } });
  },

  setHealth: (h) => set({ health: h }),

  bootstrap: async () => {
    try {
      const pairing = await loadPairing();
      if (pairing) {
        await configureTransport(pairing);
        registerQaSecret(pairing.token);
        set({ pairing, baseUrl: pairing.remoteUrl || pairing.baseUrl,
          baseUrlLan: pairing.baseUrl, bearer: pairing.token, bootstrapped: true });
        return;
      }
      const bearer = await loadBearer();
      registerQaSecret(bearer);
      set({ bearer, bootstrapped: true });
    } catch {
      // A Keystore read failure must not strand startup on a permanent spinner.
      // Leave stored credentials intact; Settings can retry or accept a new key.
      set({ bearer: null, bootstrapped: true,
        health: { state: "unreachable", at: Date.now(), detail: "Couldn't read the saved API key. Unlock the phone and reopen the app, or enter the key in Settings." } });
    }
  },
}));
