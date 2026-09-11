import { kv } from "../../lib/kv";
import { useLiveStore } from "../live";

const mockConnections: { callbacks: any; start: jest.Mock; stop: jest.Mock }[] = [];

jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(), set: jest.fn() } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../ws/live", () => ({
  applyDelta: (base: Record<string, unknown>, delta: Record<string, unknown>) => ({ ...base, ...delta,
    ...(delta.job && typeof delta.job === "object" ? { job: { ...(base.job as object), ...(delta.job as object) } } : {}) }),
  LiveConnection: jest.fn().mockImplementation((_id: string, callbacks: any) => {
    const conn = { callbacks, start: jest.fn(), stop: jest.fn() };
    mockConnections.push(conn); return conn;
  }),
}));

const mockKv = kv as unknown as { getString: jest.Mock; set: jest.Mock };

describe("live snapshot persistence", () => {
  beforeEach(() => {
    jest.useFakeTimers(); jest.clearAllMocks(); mockConnections.length = 0;
    useLiveStore.getState().reset();
  });
  afterEach(() => { useLiveStore.getState().reset(); jest.useRealTimers(); });

  it("writes the first snapshot immediately, coalesces deltas, and keeps memory current", () => {
    useLiveStore.getState().ensureConnection("p1");
    const socket = mockConnections[0];
    socket.callbacks.onMessage({ type: "snapshot", data: { phase: "printing", job: { id: "a" }, percent: 1 } });
    expect(mockKv.set).toHaveBeenCalledTimes(1);
    socket.callbacks.onMessage({ type: "delta", data: { percent: 2 } });
    socket.callbacks.onMessage({ type: "delta", data: { percent: 3 } });
    expect(useLiveStore.getState().printers.p1.snapshot?.percent).toBe(3);
    expect(mockKv.set).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(5_000);
    expect(mockKv.set).toHaveBeenCalledTimes(2);
    expect(mockKv.set.mock.lastCall[1]).toContain('"percent":3');
  });

  it("writes phase changes immediately and close flushes the latest burst", () => {
    useLiveStore.getState().ensureConnection("p1");
    const socket = mockConnections[0];
    socket.callbacks.onMessage({ type: "snapshot", data: { phase: "printing", job: { id: "a" } } });
    socket.callbacks.onMessage({ type: "delta", data: { percent: 9 } });
    socket.callbacks.onMessage({ type: "delta", data: { phase: "paused" } });
    expect(mockKv.set).toHaveBeenCalledTimes(2);
    expect(mockKv.set.mock.lastCall[1]).toContain('"phase":"paused"');
    socket.callbacks.onMessage({ type: "delta", data: { percent: 10 } });
    useLiveStore.getState().closeConnection("p1");
    expect(mockKv.set.mock.lastCall[1]).toContain('"percent":10');
  });

  it("reset cancels pending writes", () => {
    useLiveStore.getState().ensureConnection("p1");
    mockConnections[0].callbacks.onMessage({ type: "snapshot", data: { phase: "idle" } });
    mockConnections[0].callbacks.onMessage({ type: "delta", data: { temps: { nozzle: 200 } } });
    useLiveStore.getState().reset();
    jest.advanceTimersByTime(5_000);
    expect(mockKv.set).toHaveBeenCalledTimes(1);
  });
});
