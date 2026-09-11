/**
 * Live WebSocket client — `wss://bridge/api/v1/printers/{id}/ws` (auto-
 * converts http→ws/https→wss based on baseUrl).
 *
 * Wire schema per bridge spec 7:
 *   {type: "snapshot", data: {...full state...}}
 *   {type: "delta",    data: {...changed leaves only...}}
 *   {type: "event",    event: "<name>", data: {...}}
 *
 * Reconnect: 1s → 2s → 4s … capped at 30s (matches the bridge's MQTT
 * reconnect cadence). Cancelled if `stop()` is called.
 *
 * Auth: the bearer key is sent as an `Authorization: Bearer` header on the
 * WebSocket upgrade request. React Native's WebSocket supports a `headers`
 * option as the third constructor argument (unlike browsers, which cannot set
 * headers on a WS upgrade). The bridge (api/status.py) accepts the header
 * (spec 10: it checks `?token=` first, then the `Authorization` header).
 * Sending the token in the header keeps it out of server access logs and
 * proxy logs that record the request path.
 */

import { qaLog } from "../lib/qalog";
import { PairedSocket } from "../pairing/native";
import { notifyRequestFailed, notifyRequestSucceeded, resolveBaseUrl, sameOrigin } from "../api/endpoint";
import { useBridgeStore } from "../store/bridge";
import { useNetStore } from "../store/net";

export type LiveMessage =
  | { type: "snapshot"; data: Record<string, unknown> }
  | { type: "delta"; data: Record<string, unknown> }
  | { type: "event"; event: string; data: Record<string, unknown> }
  | { type: "hello"; protocol_version: number; printer_id: string }
  | { type: "ping" };

export type LiveStatus =
  | "connecting"
  | "open"
  | "closed"
  | "error";

export interface LiveOpts {
  onMessage(msg: LiveMessage): void;
  onStatus?(status: LiveStatus, detail?: string): void;
}

export class LiveConnection {
  private ws: WebSocket | PairedSocket | null = null;
  private stopped = true;
  private generation = 0;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private snapshotTimer: ReturnType<typeof setTimeout> | null = null;
  private keepaliveTimer: ReturnType<typeof setInterval> | null = null;

  constructor(private printerId: string, private opts: LiveOpts) {}

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  private clearTimers() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.snapshotTimer) clearTimeout(this.snapshotTimer);
    if (this.keepaliveTimer) clearInterval(this.keepaliveTimer);
    this.reconnectTimer = this.snapshotTimer = this.keepaliveTimer = null;
  }

  stop() {
    this.stopped = true;
    this.generation += 1;
    this.clearTimers();
    const socket = this.ws;
    this.ws = null;
    socket?.close();
    this.opts.onStatus?.("closed");
  }

  private connect() {
    const generation = ++this.generation;
    this.opts.onStatus?.("connecting");
    void this.resolveAndConnect(generation);
  }

  private async resolveAndConnect(generation: number) {
    let baseUrl: string;
    try { baseUrl = await resolveBaseUrl(); }
    catch { baseUrl = useBridgeStore.getState().baseUrl; }
    if (this.stopped || generation !== this.generation) return;
    const { bearer } = useBridgeStore.getState();
    if (!baseUrl || !bearer) {
      this.opts.onStatus?.("error", "Open Settings to configure the bridge connection.");
      return;
    }
    const wsBase = baseUrl.replace(/^http:/, "ws:").replace(/^https:/, "wss:").replace(/\/+$/, "");
    const url = `${wsBase}/printers/${encodeURIComponent(this.printerId)}/status`;
    let socket: WebSocket | PairedSocket;
    try {
      const NativeSocket = WebSocket as unknown as new (
        url: string, protocols: undefined, options: { headers: Record<string, string> },
      ) => WebSocket;
      socket = useBridgeStore.getState().pairing
        ? new PairedSocket(url, bearer)
        : new NativeSocket(url, undefined, { headers: { Authorization: `Bearer ${bearer}` } });
    } catch {
      this.opts.onStatus?.("error", "Couldn't open the live connection.");
      this.scheduleReconnect();
      return;
    }
    this.ws = socket;
    let gotSnapshot = false;
    const current = () => !this.stopped && generation === this.generation && this.ws === socket;
    const pong = () => {
      if (current() && socket.readyState === WebSocket.OPEN) {
        try { socket.send(JSON.stringify({ type: "pong" })); } catch { /* close drives retry */ }
      }
    };
    this.snapshotTimer = setTimeout(() => {
      if (current() && !gotSnapshot) socket.close(1000, "status_timeout");
    }, 15_000);
    socket.onopen = () => {
      if (!current()) return;
      // An upgrade alone is not fresh printer state. Controls remain disabled
      // until a valid snapshot replaces the cached state.
      notifyRequestSucceeded(baseUrl);
      const { baseUrlLan } = useBridgeStore.getState();
      useNetStore.getState().setReach(baseUrlLan && sameOrigin(baseUrl, baseUrlLan) ? "lan" : "remote");
      this.keepaliveTimer = setInterval(pong, 30_000);
    };
    socket.onmessage = (ev: { data: unknown }) => {
      if (!current()) return;
      let msg: LiveMessage;
      try { msg = JSON.parse(String(ev.data)); } catch { return; }
      if (!msg || typeof msg !== "object" || Array.isArray(msg)) return;
      if (msg.type === "ping") { pong(); return; }
      if (msg.type === "hello") {
        if (msg.protocol_version !== 1) {
          this.stop();
          this.opts.onStatus?.("error", "This bridge needs a newer app version.");
          return;
        }
      } else if (msg.type === "snapshot" || msg.type === "delta") {
        if (!msg.data || typeof msg.data !== "object" || Array.isArray(msg.data)) return;
        if (msg.type === "delta" && !gotSnapshot) return;
        if (msg.type === "snapshot") {
          gotSnapshot = true;
          this.attempt = 0;
          if (this.snapshotTimer) clearTimeout(this.snapshotTimer);
          this.snapshotTimer = null;
          this.opts.onMessage(msg);
          if (current()) this.opts.onStatus?.("open");
          return;
        }
      } else if (msg.type !== "event" || typeof msg.event !== "string") return;
      this.opts.onMessage(msg);
    };
    socket.onerror = () => {
      if (!current()) return;
      qaLog("ws.state", { status: "error" });
      this.opts.onStatus?.("error", "The live connection was interrupted.");
    };
    socket.onclose = (ev: { code: number }) => {
      if (!current()) return;
      this.ws = null;
      this.clearTimers();
      qaLog("ws.state", { status: "closed", code: ev.code });
      if (ev.code === 1008 || ev.code === 4000) {
        this.stopped = true;
        this.opts.onStatus?.("error", ev.code === 4000
          ? "This bridge needs a newer app version."
          : "Check your API key and selected printer in Settings.");
        return;
      }
      notifyRequestFailed(baseUrl);
      this.opts.onStatus?.("closed");
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    if (this.stopped || this.reconnectTimer) return;
    const delay = Math.min(30_000, 1_000 * 2 ** Math.min(this.attempt, 5));
    this.attempt += 1;
    qaLog("ws.state", { status: "reconnect_scheduled", attempt: this.attempt, delay_ms: delay });
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}

/**
 * Deep merge a delta payload into a snapshot object. Mirrors the bridge's
 * `_deep_merge` so what the UI holds matches what the bridge thinks it has.
 * Lists/scalars replace; nested dicts merge recursively.
 */
export function applyDelta(
  base: Record<string, unknown>,
  delta: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base };
  for (const [k, v] of Object.entries(delta)) {
    const cur = out[k];
    if (
      cur && typeof cur === "object" && !Array.isArray(cur) &&
      v && typeof v === "object" && !Array.isArray(v)
    ) {
      out[k] = applyDelta(
        cur as Record<string, unknown>,
        v as Record<string, unknown>,
      );
    } else {
      out[k] = v;
    }
  }
  return out;
}
