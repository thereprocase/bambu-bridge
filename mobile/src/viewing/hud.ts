export const CAMERA_HUD_INTERVAL_MS = 1000;

const IMMEDIATE_STATES = new Set(["auth", "identity", "network", "unavailable"]);

export function shouldPublishCameraHud({
  previousState,
  nextState,
  publishedAt,
  now,
  hadFrame,
}: {
  previousState: string;
  nextState: string;
  publishedAt: number;
  now: number;
  hadFrame: boolean;
}): boolean {
  if (nextState === "frame" && (!hadFrame || previousState !== "frame")) return true;
  if (IMMEDIATE_STATES.has(nextState) && nextState !== previousState) return true;
  return now - publishedAt >= CAMERA_HUD_INTERVAL_MS;
}
