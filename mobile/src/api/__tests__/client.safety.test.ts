import { request, resetCommandRepeat } from "../client";
import { BridgeNetworkError } from "../errors";
import { otherUrlFor } from "../endpoint";

jest.mock("../../store/bridge", () => ({ useBridgeStore: { getState: () => ({ bearer: "test-key", baseUrlLan: "http://lan.invalid/api/v1" }) } }));
jest.mock("../../store/net", () => ({ useNetStore: { getState: () => ({ setReach: jest.fn() }) } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../endpoint", () => ({
  resolveBaseUrl: jest.fn(async () => "http://lan.invalid/api/v1"),
  otherUrlFor: jest.fn(() => "http://remote.invalid/api/v1"),
  notifyRequestFailed: jest.fn(), notifyRequestSucceeded: jest.fn(), sameOrigin: () => false,
}));

const ok = () => ({ ok: true, status: 200, headers: new Headers(), text: async () => '{"ready":true}' }) as Response;
const featured = () => ({ ...ok(), headers: new Headers({ "x-bridge-features": "idempotency-key" }) }) as Response;
const keyOf = (call: unknown[]) => (call[1] as { headers: Record<string, string> }).headers["Idempotency-Key"];
beforeEach(() => {
  resetCommandRepeat();
  jest.mocked(otherUrlFor).mockReturnValue("http://remote.invalid/api/v1");
});
afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

it.each(["POST", "PUT", "PATCH", "DELETE"] as const)("never replays %s after an ambiguous network failure", async (method) => {
  const fetcher = jest.spyOn(global, "fetch").mockRejectedValue(new TypeError("response lost"));
  const failure = await request("/printers/synthetic/control", { method }).catch((e) => e);
  expect(failure).toBeInstanceOf(BridgeNetworkError);
  expect(failure.outcomeUnknown).toBe(true);
  expect(failure.message).toContain("may have reached");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(String(failure)).not.toContain(".invalid");
});

it("still retries a GET once over the other configured connection", async () => {
  const fetcher = jest.spyOn(global, "fetch").mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(ok());
  await expect(request("/printers")).resolves.toEqual({ ready: true });
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls[1][0]).toBe("http://remote.invalid/api/v1/printers");
});

it("does not replay an HTTP rejection", async () => {
  const fetcher = jest.spyOn(global, "fetch").mockResolvedValue({ ...ok(), ok: false, status: 401,
    text: async () => '{"error":"auth_invalid","message":"Rejected"}' } as Response);
  await expect(request("/printers")).rejects.toMatchObject({ status: 401 });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("keeps the timeout active while reading a response body", async () => {
  jest.useFakeTimers();
  jest.mocked(otherUrlFor).mockReturnValue(null);
  jest.spyOn(global, "fetch").mockImplementation(async (_url, options) => ({
    ...ok(), text: () => new Promise<string>((_resolve, reject) => {
      options?.signal?.addEventListener("abort", () => reject(new Error("aborted body")));
    }),
  }) as Response);
  const pending = request("/printers", { timeoutMs: 30 }).catch((e) => e);
  await jest.advanceTimersByTimeAsync(31);
  expect(await pending).toBeInstanceOf(BridgeNetworkError);
});

describe("with a bridge that deduplicates by idempotency key", () => {
  it("repeats a failed command with the same key, same address first", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured())
      .mockRejectedValueOnce(new TypeError("reset")).mockResolvedValueOnce(featured());
    await request("/printers");
    await expect(request("/printers/synthetic/fan", { method: "POST", body: { percent: 50 } }))
      .resolves.toEqual({ ready: true });
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls[2][0]).toBe("http://lan.invalid/api/v1/printers/synthetic/fan");
    expect(keyOf(fetcher.mock.calls[1])).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
    expect(keyOf(fetcher.mock.calls[2])).toBe(keyOf(fetcher.mock.calls[1]));
  });

  it("moves to the other address when the first one stays down", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured())
      .mockRejectedValueOnce(new TypeError("reset")).mockRejectedValueOnce(new TypeError("reset"))
      .mockResolvedValueOnce(featured());
    await request("/printers");
    await expect(request("/printers/synthetic/home", { method: "POST" })).resolves.toEqual({ ready: true });
    expect(fetcher.mock.calls[3][0]).toBe("http://remote.invalid/api/v1/printers/synthetic/home");
    expect(new Set(fetcher.mock.calls.slice(1).map(keyOf)).size).toBe(1);
  });

  it("reports an unknown outcome when every attempt fails", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured())
      .mockRejectedValue(new TypeError("reset"));
    await request("/printers");
    const failure = await request("/printers/synthetic/home", { method: "POST" }).catch((e) => e);
    expect(failure).toBeInstanceOf(BridgeNetworkError);
    expect(failure.outcomeUnknown).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it("returns the bridge's refusal from a repeat without trying again", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured())
      .mockRejectedValueOnce(new TypeError("reset"))
      .mockResolvedValueOnce({ ...featured(), ok: false, status: 409,
        text: async () => '{"error":"conflict","message":"Busy"}' } as Response);
    await request("/printers");
    await expect(request("/printers/synthetic/home", { method: "POST" })).rejects.toMatchObject({ status: 409 });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("sends a deadline-bound command once", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured())
      .mockRejectedValue(new TypeError("reset"));
    await request("/printers");
    await expect(request("/printers/synthetic/move/hold/renew", { method: "POST", repeat: false }))
      .rejects.toBeInstanceOf(BridgeNetworkError);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("does not repeat a command whose first attempt hung past the window", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured());
    await request("/printers");
    const clock = jest.spyOn(Date, "now");
    let now = 1_000_000;
    clock.mockImplementation(() => now);
    fetcher.mockImplementationOnce(async () => { now += 12_000; throw new TypeError("timeout"); });
    await expect(request("/printers/synthetic/home", { method: "POST" })).rejects.toBeInstanceOf(BridgeNetworkError);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("stops repeating once the bridge no longer advertises the feature", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockResolvedValueOnce(featured()).mockResolvedValueOnce(ok())
      .mockRejectedValue(new TypeError("reset"));
    await request("/printers");
    await request("/printers");
    await expect(request("/printers/synthetic/home", { method: "POST" })).rejects.toBeInstanceOf(BridgeNetworkError);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("gives every command its own key and reads none", async () => {
    const fetcher = jest.spyOn(global, "fetch").mockImplementation(async () => featured());
    await request("/printers");
    await request("/printers/synthetic/home", { method: "POST" });
    await request("/printers/synthetic/home", { method: "POST" });
    expect(keyOf(fetcher.mock.calls[0])).toBeUndefined();
    expect(keyOf(fetcher.mock.calls[1])).not.toBe(keyOf(fetcher.mock.calls[2]));
  });
});
