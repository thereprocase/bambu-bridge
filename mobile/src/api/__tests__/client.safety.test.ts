import { request } from "../client";
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
beforeEach(() => {
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
