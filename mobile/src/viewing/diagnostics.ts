import Constants from "expo-constants";
import { request, requestExact, type RawResponse } from "../api/client";
import { BridgeError, BridgeNetworkError } from "../api/errors";
import { PairingSecurityError } from "../pairing/native";
import { useBridgeStore } from "../store/bridge";
import { useNetStore } from "../store/net";
import { canViewJob } from "./job";

export type Check = { name: string; state: "passed" | "failed" | "skipped"; detail: string; ms?: number };
export type DiagnosticReport = { appVersion: string; serverVersion: string; route: string; checks: Check[] };
export function diagnosticError(error: unknown): string {
  if (error instanceof PairingSecurityError) return "Bridge identity could not be verified. Check pairing.";
  if (error instanceof BridgeNetworkError) return "Cannot reach this route. Check Wi-Fi or Tailscale.";
  if (error instanceof BridgeError) {
    if (error.status === 401 || error.status === 403) return "Access rejected. Check pairing or the API key.";
    if (error.status === 404) return "Not available on this bridge or job.";
    if (error.status === 422) return "No viewable job data is currently available.";
    if (error.status === 503) return "Bridge reachable; printer service is unavailable.";
    return `Bridge returned HTTP ${error.status}.`;
  }
  return "Check could not be completed. Retry after checking the connection.";
}
const version = (value: unknown): string => typeof value === "string" && /^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$/.test(value) ? value : "unknown";

/** Only allowlisted result categories enter the report: never URLs, names, keys or raw errors. */
export async function runDiagnostics(printer: string | null, changed: (report: DiagnosticReport) => void): Promise<DiagnosticReport> {
  const report: DiagnosticReport = { appVersion: version(Constants.expoConfig?.version), serverVersion: "unknown", route: "unknown", checks: [] };
  const update = () => { changed({ ...report, checks: [...report.checks] }); };
  async function check(name: string, run: () => Promise<string>) {
    const started = Date.now();
    try { report.checks.push({ name, state: "passed", detail: await run(), ms: Date.now()-started }); }
    catch (error) { report.checks.push({ name, state: "failed", detail: diagnosticError(error), ms: Date.now()-started }); }
    update();
  }
  const { baseUrl, baseUrlLan, pairing } = useBridgeStore.getState();
  const remoteBase = pairing ? pairing.remoteUrl : baseUrl === baseUrlLan ? null : baseUrl;
  for (const [label, base] of [["Home network route", baseUrlLan], ["Remote route", remoteBase]] as const) {
    if (!base) { report.checks.push({ name: label, state: "skipped", detail: "Not configured." }); continue; }
    await check(label, async () => {
      await requestExact(base, "/printers", { timeoutMs: 6000 });
      return "Bridge reachable and authentication accepted.";
    });
  }
  await check("Bridge version", async () => {
    const health = await request<{ version?: string }>("/version", { timeoutMs: 6000 });
    report.serverVersion = version(health.version);
    return `Server ${report.serverVersion}`;
  });
  await check("Authentication", async () => {
    await request("/printers", { timeoutMs: 6000 });
    return "This app's credential is accepted.";
  });
  let snapshot: Record<string, unknown> | null = null;
  if (printer) {
    const root = `/printers/${encodeURIComponent(printer)}`;
    await check("Printer connection", async () => {
      snapshot = await request<Record<string, unknown>>(root, { timeoutMs: 6000 });
      const session = snapshot.session as { connected?: boolean } | undefined;
      if (session?.connected !== true) throw new BridgeError({ error: "printer_offline", message: "" }, 503);
      return "Printer is connected to the bridge.";
    });
    await check("Camera frame", async () => {
      const image = await request<RawResponse>(`${root}/camera/snapshot.jpg`, { timeoutMs: 10000, rawBytes: true });
      const bytes = new Uint8Array(image.bytes);
      if (bytes.length < 4 || bytes[0] !== 255 || bytes[1] !== 216 || bytes[bytes.length-2] !== 255 || bytes[bytes.length-1] !== 217) throw new Error();
      return "A JPEG camera frame arrived.";
    });
    await check("3D viewer page", async () => {
      const page = await request<RawResponse>(`${root}/viz`, { timeoutMs: 10000, rawBytes: true });
      if (!page.contentType.includes("text/html") || page.bytes.byteLength < 100) throw new Error();
      return "Viewer page is available.";
    });
    report.checks.push({ name: "3D job data", state: canViewJob(snapshot) ? "passed" : "skipped",
      detail: canViewJob(snapshot) ? "A job is available. Open 3D to verify rendering." : "No current or completed job is reported." });
  } else report.checks.push({ name: "Printer checks", state: "skipped", detail: "Select a printer first." });
  const reach = useNetStore.getState().reach;
  report.route = reach === "lan" ? "Home network" : reach === "remote" ? "Remote" : "Unavailable";
  update(); return report;
}

export function formatDiagnosticReport(report: DiagnosticReport): string {
  return [`Bambu Bridge Android ${report.appVersion}`, `Server: ${report.serverVersion}`, `Connection: ${report.route}`,
    ...report.checks.map(check => `${check.name}: ${check.state} — ${check.detail}${check.ms === undefined ? "" : ` (${check.ms} ms)`}`)].join("\n");
}
