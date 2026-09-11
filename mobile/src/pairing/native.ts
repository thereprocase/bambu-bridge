import { NativeEventEmitter, NativeModules, Platform } from "react-native";
import type { PairedProfile } from "./protocol";

interface Result { status: number; contentType: string; body: string }
interface NativeBridge {
  scan(): Promise<string | null>;
  configure(base: string | null, pin: string | null, remote: string | null): Promise<void>;
  claim(base: string, pin: string, secret: string, name: string): Promise<string>;
  request(id: string, url: string, method: string, headers: Record<string, string>, body: string | null,
    timeout: number, binary: boolean): Promise<Result>;
  cancelRequest(id: string): void;
  connect(id: string, url: string, token: string): void;
  send(id: string, data: string): void;
  close(id: string, code: number): void;
  addListener(name: string): void;
  removeListeners(count: number): void;
}
function native(): NativeBridge {
  if (Platform.OS !== "android" || !NativeModules.SecureBridge) {
    throw new Error("Secure pairing requires the updated Android app.");
  }
  return NativeModules.SecureBridge as NativeBridge;
}
let serial = 0;
export const scanPairingCode = () => native().scan();
export const configureTransport = (p: PairedProfile | null) =>
  native().configure(p?.baseUrl ?? null, p?.spki ?? null, p?.remoteUrl ?? null);
export const claimPairing = (base: string, pin: string, secret: string, name: string) =>
  native().claim(base, pin, secret, name);

export class PairingSecurityError extends Error {
  constructor() { super("Couldn't verify the paired bridge. Check its identity and pair again."); }
}
export async function pairedFetch(url: string, method: string, headers: Record<string, string>,
  body: string | undefined, timeout: number, binary: boolean, signal: AbortSignal): Promise<Response> {
  const id = `request-${++serial}`;
  const module = native();
  const abort = () => module.cancelRequest(id);
  if (signal.aborted) throw new Error("Request cancelled");
  signal.addEventListener("abort", abort);
  try {
    const result = await module.request(id, url, method, headers, body ?? null, timeout, binary);
    return {
      status: result.status, ok: result.status >= 200 && result.status < 300,
      headers: new Headers({ "content-type": result.contentType }),
      text: async () => result.body,
      arrayBuffer: async () => decodeBase64(result.body),
    } as Response;
  } catch (e) {
    if ((e as { code?: string })?.code === "PAIR_IDENTITY") throw new PairingSecurityError();
    throw e;
  } finally { signal.removeEventListener("abort", abort); }
}

export function decodeBase64(value: string): ArrayBuffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const pad = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  const bytes = new Uint8Array(value.length / 4 * 3 - pad);
  let pos = 0;
  for (let i = 0; i < value.length; i += 4) {
    const bits = (alphabet.indexOf(value[i]) << 18) | (alphabet.indexOf(value[i + 1]) << 12) |
      (Math.max(0, alphabet.indexOf(value[i + 2])) << 6) | Math.max(0, alphabet.indexOf(value[i + 3]));
    if (pos < bytes.length) bytes[pos++] = (bits >> 16) & 255;
    if (pos < bytes.length) bytes[pos++] = (bits >> 8) & 255;
    if (pos < bytes.length) bytes[pos++] = bits & 255;
  }
  return bytes.buffer;
}

/** Minimal WebSocket interface used by LiveSocket; native OkHttp owns WSS/TLS. */
export class PairedSocket {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: { message: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  private id = `socket-${++serial}`;
  private subscription: { remove(): void };
  private module = native();
  constructor(url: string, token: string) {
    const emitter = new NativeEventEmitter(this.module);
    this.subscription = emitter.addListener("secureBridgeSocket", (event) => {
      if (event.id !== this.id || this.readyState === 3) return;
      if (event.type === "open") { this.readyState = 1; this.onopen?.(); }
      if (event.type === "message") this.onmessage?.({ data: event.data });
      if (event.type === "error") this.onerror?.({ message: event.data });
      if (event.type === "close") {
        this.readyState = 3; this.subscription.remove();
        this.onclose?.({ code: event.code, reason: "" });
      }
    });
    this.module.connect(this.id, url, token);
  }
  send(data: string) { if (this.readyState === 1) this.module.send(this.id, data); }
  close(code = 1000, _reason?: string) {
    if (this.readyState === 3) return;
    this.readyState = 3; this.subscription.remove(); this.module.close(this.id, code);
    // LiveSocket's timeout relies on close driving reconnect too.
    this.onclose?.({ code, reason: "" });
  }
}
