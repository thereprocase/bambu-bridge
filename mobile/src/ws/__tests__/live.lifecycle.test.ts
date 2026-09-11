import { LiveConnection } from "../live";
import { resolveBaseUrl, notifyRequestFailed } from "../../api/endpoint";

jest.mock("../../store/bridge", () => ({ useBridgeStore: { getState: () => ({ bearer: "synthetic-ws-secret", baseUrl: "http://bridge.invalid/api/v1", baseUrlLan: null }) } }));
jest.mock("../../store/net", () => ({ useNetStore: { getState: () => ({ setReach: jest.fn() }) } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../api/endpoint", () => ({ resolveBaseUrl: jest.fn(async () => "http://bridge.invalid/api/v1"),
  notifyRequestFailed: jest.fn(), notifyRequestSucceeded: jest.fn(), sameOrigin: () => false }));

class MockSocket {
  static OPEN = 1;
  static instances: MockSocket[] = [];
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: { message: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  send = jest.fn();
  close = jest.fn();
  constructor(public url: string, _protocol: unknown, public options: unknown) { MockSocket.instances.push(this); }
  emit(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
const originalSocket = global.WebSocket;
const connections: LiveConnection[] = [];
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
function connection() {
  const onMessage = jest.fn(); const onStatus = jest.fn();
  const conn = new LiveConnection("synthetic/printer", { onMessage, onStatus });
  connections.push(conn); conn.start();
  return { conn, onMessage, onStatus };
}
beforeEach(() => {
  jest.useFakeTimers(); jest.clearAllMocks(); MockSocket.instances = [];
  global.WebSocket = MockSocket as unknown as typeof WebSocket;
  jest.mocked(resolveBaseUrl).mockResolvedValue("http://bridge.invalid/api/v1");
});
afterEach(() => {
  connections.splice(0).forEach((c) => c.stop());
  jest.clearAllTimers(); jest.useRealTimers(); global.WebSocket = originalSocket;
});

it("waits for a valid fresh snapshot before enabling the live status", async () => {
  const { onMessage, onStatus } = connection(); await flush();
  const socket = MockSocket.instances[0]; socket.onopen?.();
  socket.emit({ type: "delta", data: { phase: "printing" } });
  socket.emit({ type: "snapshot", data: null });
  expect(onStatus).not.toHaveBeenCalledWith("open");
  expect(onMessage).not.toHaveBeenCalled();
  socket.emit({ type: "snapshot", data: { phase: "printing" } });
  expect(onStatus).toHaveBeenLastCalledWith("open");
  expect(socket.url).toContain("synthetic%2Fprinter/status");
  expect(socket.url).not.toContain("secret");
  expect(socket.options).toEqual({ headers: { Authorization: "Bearer synthetic-ws-secret" } });
});

it("answers server pings and stops its keepalive when closed", async () => {
  const { conn } = connection(); await flush(); const socket = MockSocket.instances[0]; socket.onopen?.();
  socket.emit({ type: "snapshot", data: {} }); socket.emit({ type: "ping" });
  expect(socket.send).toHaveBeenCalledWith('{"type":"pong"}');
  conn.stop(); socket.send.mockClear(); await jest.advanceTimersByTimeAsync(60_000);
  expect(socket.send).not.toHaveBeenCalled();
});

it("reconnects when an established socket stops sending incoming messages", async () => {
  const { onStatus } = connection(); await flush();
  const socket = MockSocket.instances[0]; socket.onopen?.();
  socket.emit({ type: "snapshot", data: {} });
  onStatus.mockClear();
  await jest.advanceTimersByTimeAsync(65_000);
  expect(onStatus).toHaveBeenCalledWith("closed");
  expect(socket.close).toHaveBeenCalledWith(1000, "incoming_timeout");
  await jest.advanceTimersByTimeAsync(999);
  expect(MockSocket.instances).toHaveLength(1);
  await jest.advanceTimersByTimeAsync(1);
  expect(MockSocket.instances).toHaveLength(2);
});

it("reconnects after the initial snapshot deadline without an onclose callback", async () => {
  const { onStatus } = connection(); await flush();
  const socket = MockSocket.instances[0]; socket.onopen?.();
  await jest.advanceTimersByTimeAsync(15_000);
  expect(socket.close).toHaveBeenCalledWith(1000, "status_timeout");
  expect(onStatus).toHaveBeenCalledWith("closed");
  await jest.advanceTimersByTimeAsync(1_000);
  expect(MockSocket.instances).toHaveLength(2);
});

it("reconnects when an incoming timeout close throws", async () => {
  const { onStatus } = connection(); await flush();
  const socket = MockSocket.instances[0]; socket.onopen?.();
  socket.emit({ type: "snapshot", data: {} });
  socket.close.mockImplementation(() => { throw new Error("socket already gone"); });
  await jest.advanceTimersByTimeAsync(65_000);
  expect(onStatus).toHaveBeenCalledWith("closed");
  await jest.advanceTimersByTimeAsync(1_000);
  expect(MockSocket.instances).toHaveLength(2);
});

it("refreshes incoming liveness on healthy heartbeats", async () => {
  const { onStatus } = connection(); await flush();
  const socket = MockSocket.instances[0]; socket.onopen?.();
  socket.emit({ type: "snapshot", data: {} });
  onStatus.mockClear();
  await jest.advanceTimersByTimeAsync(60_000);
  socket.emit({ type: "ping" });
  await jest.advanceTimersByTimeAsync(60_000);
  expect(socket.close).not.toHaveBeenCalled();
  expect(onStatus).not.toHaveBeenCalledWith("closed");
});

it("cleans the incoming watchdog across stop and restart", async () => {
  const { conn, onStatus } = connection(); await flush();
  const old = MockSocket.instances[0]; old.onopen?.(); old.emit({ type: "snapshot", data: {} });
  conn.stop(); conn.start(); await flush();
  const current = MockSocket.instances[1]; current.onopen?.(); current.emit({ type: "snapshot", data: {} });
  onStatus.mockClear();
  await jest.advanceTimersByTimeAsync(64_999);
  expect(old.close).toHaveBeenCalled();
  expect(current.close).not.toHaveBeenCalled();
  expect(onStatus).not.toHaveBeenCalledWith("closed");
  await jest.advanceTimersByTimeAsync(1);
  expect(current.close).toHaveBeenCalledWith(1000, "incoming_timeout");
});

it("does not let an old socket close or message overwrite a restarted connection", async () => {
  const { conn, onMessage, onStatus } = connection(); await flush(); const old = MockSocket.instances[0];
  conn.stop(); conn.start(); await flush(); const current = MockSocket.instances[1];
  current.emit({ type: "snapshot", data: { phase: "idle" } });
  onMessage.mockClear(); onStatus.mockClear();
  old.onclose?.({ code: 1006, reason: "old" }); old.emit({ type: "snapshot", data: { phase: "printing" } });
  expect(onMessage).not.toHaveBeenCalled(); expect(onStatus).not.toHaveBeenCalled();
  await jest.advanceTimersByTimeAsync(5000); expect(MockSocket.instances).toHaveLength(2);
});

it("ignores an endpoint lookup that completes after stop and restart", async () => {
  let finish!: (value: string) => void;
  jest.mocked(resolveBaseUrl).mockImplementationOnce(() => new Promise((r) => { finish = r; }));
  const { conn } = connection(); conn.stop(); conn.start(); await flush();
  finish("http://stale.invalid/api/v1"); await flush();
  expect(MockSocket.instances).toHaveLength(1);
  expect(MockSocket.instances[0].url).not.toContain("stale");
});

it.each([1008, 4000])("stops retrying after policy/protocol close %i", async (code) => {
  const { onStatus } = connection(); await flush();
  MockSocket.instances[0].onclose?.({ code, reason: "token=private" });
  await jest.advanceTimersByTimeAsync(60_000);
  expect(MockSocket.instances).toHaveLength(1);
  expect(notifyRequestFailed).not.toHaveBeenCalled();
  expect(JSON.stringify(onStatus.mock.calls)).not.toContain("private");
});

it("rejects a protocol mismatch and never opens a duplicate on start", async () => {
  const { conn, onStatus } = connection(); conn.start(); await flush();
  expect(MockSocket.instances).toHaveLength(1);
  MockSocket.instances[0].emit({ type: "hello", protocol_version: 2 });
  expect(onStatus).toHaveBeenLastCalledWith("error", "This bridge needs a newer app version.");
  await jest.advanceTimersByTimeAsync(60_000); expect(MockSocket.instances).toHaveLength(1);
});

it("backs off across upgrades that fail before a snapshot", async () => {
  connection(); await flush(); let socket = MockSocket.instances[0]; socket.onopen?.();
  socket.onclose?.({ code: 1006, reason: "" }); await jest.advanceTimersByTimeAsync(1000);
  socket = MockSocket.instances[1]; socket.onopen?.(); socket.onclose?.({ code: 1006, reason: "" });
  await jest.advanceTimersByTimeAsync(1000); expect(MockSocket.instances).toHaveLength(2);
  await jest.advanceTimersByTimeAsync(1000); expect(MockSocket.instances).toHaveLength(3);
});
