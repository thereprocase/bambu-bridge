import { createSnapshotCache } from "../snapshotCache";

jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(), set: jest.fn() } }));

describe("snapshot cache", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it("coalesces bursts and writes the latest snapshot", () => {
    const store = { getString: jest.fn(), set: jest.fn() };
    const cache = createSnapshotCache(store);
    cache.schedule("p1", { phase: "printing", percent: 1 });
    cache.schedule("p1", { phase: "printing", percent: 2 });
    expect(store.set).not.toHaveBeenCalled();
    jest.advanceTimersByTime(4_999);
    expect(store.set).not.toHaveBeenCalled();
    jest.advanceTimersByTime(1);
    expect(store.set).toHaveBeenCalledWith("live.p1.snapshot", JSON.stringify({ phase: "printing", percent: 2 }));
  });

  it("flushes immediately and suppresses failed writes", () => {
    const store = { getString: jest.fn(), set: jest.fn(() => { throw new Error("MMKV unavailable"); }) };
    const cache = createSnapshotCache(store);
    expect(() => cache.schedule("p1", { phase: "idle" }, true)).not.toThrow();
    expect(() => cache.schedule("p2", { phase: "printing" })).not.toThrow();
    expect(() => cache.flush()).not.toThrow();
  });

  it("cancels pending old data", () => {
    const store = { getString: jest.fn(), set: jest.fn() };
    const cache = createSnapshotCache(store);
    cache.schedule("p1", { phase: "printing" });
    cache.cancel();
    jest.advanceTimersByTime(5_000);
    expect(store.set).not.toHaveBeenCalled();
  });
});
