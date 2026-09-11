import { BridgeError, BridgeNetworkError } from "../../api/errors";
import { PairingSecurityError } from "../../pairing/native";
import { diagnosticError, formatDiagnosticReport, runDiagnostics } from "../diagnostics";
import { request, requestExact } from "../../api/client";

jest.mock("expo-constants", () => ({ __esModule: true, default: { expoConfig: { version: "0.20.0" } } }));
jest.mock("../../api/client", () => ({ request: jest.fn(), requestExact: jest.fn() }));
const mockBridgeConfig: { baseUrl: string; baseUrlLan: string; pairing?: { remoteUrl?: string } } = {
  baseUrl: "https://remote.invalid/api/v1", baseUrlLan: "https://home.invalid/api/v1",
};
jest.mock("../../store/bridge", () => ({ useBridgeStore: { getState: () => mockBridgeConfig } }));
jest.mock("../../store/net", () => ({ useNetStore: { getState: () => ({ reach: "remote" }) } }));

beforeEach(() => { jest.clearAllMocks(); delete mockBridgeConfig.pairing; });
test("raw exception text and server errors cannot leak into a shared report", () => {
  const secret = "private-credential-and-printer-name";
  for (const error of [new Error(secret), new BridgeError({ error: "internal_error", message: secret }, 502), new BridgeNetworkError(false), new PairingSecurityError()]) {
    expect(diagnosticError(error)).not.toContain(secret);
  }
  expect(diagnosticError(new PairingSecurityError())).toMatch(/identity/);
  expect(diagnosticError(new BridgeError({ error: "auth_invalid", message: secret }, 401))).toMatch(/Access rejected/);
});

test("checks real read endpoints and reports a completed job without exposing identifiers", async () => {
  (requestExact as jest.Mock).mockResolvedValue([]);
  (request as jest.Mock).mockImplementation(async path => {
    if (path === "/version") return { version: "0.5.0" };
    if (path === "/printers") return [];
    if (path.endsWith("/snapshot.jpg")) return { bytes: new Uint8Array([255,216,255,217]).buffer, contentType: "image/jpeg" };
    if (path.endsWith("/viz")) return { bytes: new ArrayBuffer(200), contentType: "text/html" };
    return { session: { connected: true }, phase: "finished", job: { subtask_name: "private-file" } };
  });
  const result = await runDiagnostics("private-printer", () => {});
  expect(result.checks.every(check => check.state === "passed")).toBe(true);
  const text = formatDiagnosticReport(result);
  expect(text).toContain("0.5.0");
  for (const privateValue of ["remote.invalid", "home.invalid", "private-printer", "private-file"]) expect(text).not.toContain(privateValue);
  for (const [, options] of (request as jest.Mock).mock.calls) expect(options?.method).toBeUndefined();
});

test("one failed check does not hide other results, and missing jobs are skipped", async () => {
  (requestExact as jest.Mock).mockRejectedValue(new BridgeNetworkError(false));
  (request as jest.Mock).mockRejectedValue(new BridgeError({ error: "not_found", message: "private" }, 404));
  const result = await runDiagnostics(null, () => {});
  expect(result.checks.filter(check => check.state === "failed")).toHaveLength(4);
  expect(result.checks.at(-1)?.state).toBe("skipped");
});

test("a locally paired bridge cannot be reported as a verified remote route", async () => {
  mockBridgeConfig.pairing = {};
  (requestExact as jest.Mock).mockResolvedValue([]);
  (request as jest.Mock).mockResolvedValue({ version: "0.5.0" });
  const result = await runDiagnostics(null, () => {});
  expect(result.checks.find(check => check.name === "Remote route")?.state).toBe("skipped");
  expect(requestExact).toHaveBeenCalledTimes(1);
});
