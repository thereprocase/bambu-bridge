import { buildViewerUrl, isViewerNavigationAllowed, safeVizTimings } from "../viewer";
import { resolveBaseUrl } from "../endpoint";

jest.mock("../../store/bridge", () => ({ useBridgeStore: { getState: () => ({ bearer: "synthetic-viewer-secret" }) } }));
jest.mock("../endpoint", () => ({ resolveBaseUrl: jest.fn(async () => "http://bridge.invalid/api/v1") }));

it("resolves the current endpoint on every viewer retry", async () => {
  const first = await buildViewerUrl("synthetic");
  jest.mocked(resolveBaseUrl).mockResolvedValueOnce("http://fallback.invalid/api/v1");
  const next = await buildViewerUrl("synthetic");
  expect(first).toContain("bridge.invalid"); expect(next).toContain("fallback.invalid");
});
it("blocks navigation outside the configured bridge viewer", () => {
  const current = "http://bridge.invalid/api/v1/printers/synthetic/viz?token=secret";
  expect(isViewerNavigationAllowed(current, current + "#layer")).toBe(true);
  for (const target of ["https://outside.invalid/", "intent://anything", "javascript:alert(1)", "http://bridge.invalid/app/", "http://user:pass@bridge.invalid/api/v1/printers/synthetic/viz"]) {
    expect(isViewerNavigationAllowed(current, target)).toBe(false);
  }
});
it("forwards only finite numeric timings and the known format enum", () => {
  expect(safeVizTimings({ fetch_ms: 10, parse_ms: Infinity, bytes: -1, segments: 123,
    fmt: "bin", hidden: { token: "private" }, buffers_ms: "http://private.invalid" })).toEqual({ fetch_ms: 10, segments: 123, fmt: "bin" });
});
