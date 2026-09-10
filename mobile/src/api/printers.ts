import { request } from "./client";
import {
  PrinterSnapshot,
  PrinterSummary,
  RegisterPrinterBody,
  RegisterPrinterResponse,
} from "./types";

export function listPrinters(): Promise<PrinterSummary[]> {
  return request<PrinterSummary[]>("/printers");
}

export function getPrinter(id: string): Promise<PrinterSnapshot> {
  return request<PrinterSnapshot>(`/printers/${id}`);
}

export function registerPrinter(body: RegisterPrinterBody) {
  return request<RegisterPrinterResponse>("/printers", {
    method: "POST",
    body,
    // Onboarding probe waits up to ~10s for the MQTT telemetry watchdog
    // on E3; loosen the default 15s timeout so the user-visible cap
    // matches what the bridge can actually take.
    timeoutMs: 25_000,
  });
}

export function deletePrinter(id: string, cascadeJobs = false) {
  return request<void>(`/printers/${id}`, {
    method: "DELETE",
    query: cascadeJobs ? { cascade_jobs: "true" } : undefined,
  });
}

export function trustPrinter(id: string) {
  return request<void>(`/printers/${id}/trust`, { method: "POST" });
}

export function patchPrinter(
  id: string,
  body: { friendly_name?: string; ip?: string; access_code?: string },
) {
  return request<PrinterSummary>(`/printers/${id}`, { method: "PATCH", body });
}
