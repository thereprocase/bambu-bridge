import { request } from "./client";

export interface RemoteFile {
  name: string;
  size?: number;
  /** ISO-8601 UTC timestamp when the file was last sliced (may be null). */
  sliced_at?: string | null;
  /** ISO-8601 UTC timestamp of the file's last modification (may be null). */
  modified_at?: string | null;
  /** ISO-8601 UTC timestamp when the file was created (may be null). */
  created_at?: string | null;
  /**
   * Which timestamp the server used for sort ordering.
   * "sliced" | "modified" | "created" | "none"
   */
  sort_basis?: string;
}

/**
 * Bridge returns `{ dir: "", files: [...], file_names: [...] }`.
 * Each entry in `files` is an object: `{ name, sliced_at, modified_at,
 * created_at, sort_basis }`. The server pre-sorts newest-first using the
 * sliced→modified→created timestamp chain. Trust server order — do NOT
 * re-sort the returned array.
 *
 * A backward-compat `file_names` flat string list is also present; we ignore
 * it and always use `files`.  The adapter below also tolerates a plain string
 * entry for forward-compat with any legacy bridge version.
 *
 * Allowed dirs (P1S firmware, confirmed 2026-06-11):
 *   ""          — root "/"; .gcode.3mf and .gcode print files live here
 *   "cache"     — transient per-job expanded files
 *   "timelapse" — .avi clips (optional; may not exist on all firmware variants)
 * The old "model" slug does not exist on the printer and is rejected by the
 * server with HTTP 422.
 */
export async function listFiles(
  printerId: string,
  dir: "" | "cache" | "timelapse" = "",
): Promise<RemoteFile[]> {
  const raw = await request<{ dir: string; files: (string | RemoteFile)[] }>(
    `/printers/${printerId}/files`,
    { query: { dir } },
  );
  const arr = raw?.files ?? [];
  return arr.map((f) => (typeof f === "string" ? { name: f } : f));
}

/**
 * The bridge takes the filename in the path, not as a query — the route is
 * `DELETE /printers/{id}/files/{filename}`.
 */
export function deleteFile(printerId: string, filename: string) {
  return request<void>(
    `/printers/${printerId}/files/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
}

export async function downloadSnapshot(printerId: string): Promise<ArrayBuffer> {
  // Camera snapshot — `.jpg` suffix is part of the route. Bridge serves
  // image/jpeg directly, plus envelopes the auth-required-but-no-frame
  // case as a 503 with `camera_no_frame`.
  const raw = await request<{ status: number; bytes: ArrayBuffer }>(
    `/printers/${printerId}/camera/snapshot.jpg`,
    { rawBytes: true, timeoutMs: 8_000 },
  );
  return raw.bytes;
}
