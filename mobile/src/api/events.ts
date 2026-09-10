import { request } from "./client";
import { EventRow } from "./types";

export function listEvents(printerId: string, opts?: {
  since?: number;
  limit?: number;
  // Contract §8.4 severity enum is info|warn|error — the bridge 422s anything
  // else (its query pattern is ^(info|warn|error)$).
  severity?: "info" | "warn" | "error";
}) {
  return request<EventRow[]>(`/printers/${printerId}/events`, {
    query: opts,
  });
}

// Contract §8.5: dismiss is scoped under the printer —
// POST /printers/{printer_id}/events/{event_id}/dismiss.
export function dismissEvent(printerId: string, eventId: number) {
  return request<void>(`/printers/${printerId}/events/${eventId}/dismiss`, { method: "POST" });
}

export function clearEvents(printerId: string) {
  return request<void>(`/printers/${printerId}/events/clear`, { method: "POST" });
}
