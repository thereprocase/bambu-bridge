import { request } from "./client";
import { Job, QueueItem, Spool } from "./types";

export function listJobs(params?: { printer_id?: string; limit?: number }) {
  return request<Job[]>("/jobs", { query: params });
}

/** Start a 3MF already in the printer's root storage through the queue API. */
export async function submitPrint(body: {
  printer_id: string;
  filename: string;
  ams_mapping?: number[]; // Physical slots 1–4; the bridge converts to protocol indices.
}) {
  const item = await request<QueueItem>(`/printers/${encodeURIComponent(body.printer_id)}/queue`, {
    method: "POST",
    body: { file_path: `/${body.filename}`, file_name: body.filename, ams_mapping: body.ams_mapping },
  });
  // Keep the queued item on an error. A lost response may mean the start was
  // accepted, so neither retry nor delete it automatically.
  const job = await request<Job>(`/queue/${encodeURIComponent(item.id)}/start`, {
    method: "POST", timeoutMs: 60_000,
  });
  return { job_id: job.id, state: job.state };
}

export function canStartStoredPrint(snapshot: Record<string, unknown> | null, live: boolean): boolean {
  const session = snapshot?.session;
  return live && !!session && typeof session === "object" && !Array.isArray(session) &&
    (session as Record<string, unknown>).connected === true &&
    (snapshot?.phase === "idle" || snapshot?.phase === "completed");
}

export function cancelJob(id: string) {
  return request<{ id: string; state: Job["state"] }>(`/jobs/${id}/cancel`, {
    method: "POST",
  });
}

// Queue (PR 0984f2d) ----------------------------------------------------------

export function listQueue(printerId: string) {
  return request<QueueItem[]>(`/printers/${printerId}/queue`);
}

export function enqueue(
  printerId: string,
  body: { filename: string; physical_slot?: number },
) {
  return request<QueueItem>(`/printers/${printerId}/queue`, {
    method: "POST",
    body: { file_path: `/${body.filename}`, file_name: body.filename,
      ams_mapping: body.physical_slot == null ? undefined : [body.physical_slot] },
  });
}

export function dequeue(_printerId: string, queueItemId: string) {
  return request<void>(`/queue/${encodeURIComponent(queueItemId)}`, {
    method: "DELETE",
  });
}

// Spools ----------------------------------------------------------------------

export function listSpools() {
  return request<Spool[]>("/spools");
}

export function createSpool(body: Omit<Spool, "id">) {
  return request<Spool>("/spools", { method: "POST", body });
}

export function deleteSpool(id: string) {
  return request<void>(`/spools/${id}`, { method: "DELETE" });
}
