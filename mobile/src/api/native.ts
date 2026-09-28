import { request } from "./client";
import { resolveBaseUrl } from "./endpoint";
import { downloadNativeBackup, importNativeBackup } from "../pairing/native";
import { useBridgeStore } from "../store/bridge";

export interface NativeReceipt {
  id: string;
  printer: string;
  logical: string;
  remote: string;
  kind: string;
  retained: number;
  complete: number;
  bytes: number;
  state: string;
  code: string | null;
  start_state: string | null;
  review_ack_at: number | null;
  needs_review: number;
  created: number;
  dispatched_at: number | null;
  start_requested_at: number | null;
}

export interface NativeQueuePage {
  printer_id: string;
  start_owner: string | null;
  start_owner_state?: string | null;
  start_owner_logical?: string | null;
  review_count: number;
  acknowledgeable_count: number;
  view: NativeQueueView;
  offset: number;
  has_more: boolean;
  uploads: NativeReceipt[];
}

export interface NativeBackup {
  id: string;
  created: number;
  bytes: number;
}

export type NativeAction = "cancel" | "resolve" | "retry_delivery" | "discard";
export type NativeQueueView = "review" | "active" | "all";

export function nativeQueue(offset = 0, view: NativeQueueView = "all") {
  return request<NativeQueuePage>("/native/queue", { query: { offset, limit: 100, view } });
}

export function acknowledgePastNativeReviews() {
  return request<{ acknowledged: number; review_count: number; acknowledgeable_count: number }>(
    "/native/queue/acknowledge", {
      method: "POST", body: { confirm: "Ignore past review warnings; keep active starts" },
    },
  );
}

export function nativeAction(id: string, action: NativeAction) {
  return request<{ uploads: NativeReceipt[] }>(`/native/uploads/${encodeURIComponent(id)}`, {
    method: "POST",
    body: { action, confirm: "I checked the printer and this action" },
  });
}

export function nativeBackups() {
  return request<NativeBackup[]>("/native/recovery/backups");
}

export function createNativeBackup() {
  return request<NativeBackup>("/native/recovery/backups", { method: "POST", timeoutMs: 120_000 });
}

export function deleteNativeBackup(id: string) {
  return request<void>(`/native/recovery/backups/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function restoreNativeBackup(id: string) {
  return request<{ restored: string; safety_backup: string }>(
    `/native/recovery/backups/${encodeURIComponent(id)}/restore`,
    { method: "POST", body: { confirm: "Restore native inbox and review every pending start" }, timeoutMs: 120_000 },
  );
}

async function backupTransferUrl(path: string): Promise<{ url: string; token: string }> {
  const base = await resolveBaseUrl();
  const token = useBridgeStore.getState().bearer;
  if (!base || !token) throw new Error("Bridge connection is not configured");
  return { url: base.replace(/\/$/, "") + path, token };
}

export async function exportNativeBackup(id: string) {
  const { url, token } = await backupTransferUrl(`/native/recovery/backups/${encodeURIComponent(id)}`);
  return downloadNativeBackup(url, token);
}

export async function importNativeBackupFile(uri: string) {
  const { url, token } = await backupTransferUrl("/native/recovery/backups/import");
  return JSON.parse(await importNativeBackup(url, token, uri)) as { id: string; bytes: number };
}
