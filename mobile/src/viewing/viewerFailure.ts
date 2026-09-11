export type ViewerFailure = { message: string; fatal: boolean } | null;
export type ViewerFailureEvent =
  | { type: "failed"; message: string; fatal?: boolean }
  | { type: "ready" }
  | { type: "reload" };

/** A page can recover its own loading error, but cannot override native trust decisions. */
export function viewerFailureReducer(current: ViewerFailure, event: ViewerFailureEvent): ViewerFailure {
  if (event.type === "reload") return null;
  if (current?.fatal) return current;
  if (event.type === "ready") return null;
  return { message: event.message, fatal: event.fatal ?? false };
}
