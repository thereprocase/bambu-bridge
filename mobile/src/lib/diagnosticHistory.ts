import type { FaultHistoryRow, RefillHistoryRow } from "../api/types";

export function historyTime(value: string | null | undefined): string {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : "Not observed";
}

export function faultDetails(row: FaultHistoryRow): string {
  return [row.code.toUpperCase(), row.text,
    `First seen: ${historyTime(row.first_seen)}`,
    `Last seen: ${historyTime(row.last_seen)}`,
    `Occurrences: ${row.count}`,
    `Status: ${row.active ? row.stale ? "Latched" : "Active" : "Cleared"}`].join("\n");
}

export function refillDetails(row: RefillHistoryRow): string {
  const title = `${row.from_slot} → ${row.to_slot ?? "?"} · ${row.status}`;
  return [title, `Switch first observed: ${historyTime(row.started_at)}`,
    `Completion observed: ${historyTime(row.completed_at ?? (row.status === "completed" ? row.observed_at : null))}`,
    ...(row.interrupted_at ? [`Interrupted: ${historyTime(row.interrupted_at)}`] : [])].join("\n");
}
