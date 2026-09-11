/** Viewing never sends commands. Finished/failed jobs can still have useful geometry. */
export function canViewJob(snapshot: Record<string, unknown> | null): boolean {
  const job = snapshot?.job;
  if (!job || typeof job !== "object" || Array.isArray(job)) return false;
  const data = job as Record<string, unknown>;
  return [data.subtask_name, data.gcode_file].some(value => typeof value === "string" && value.trim().length > 0);
}
