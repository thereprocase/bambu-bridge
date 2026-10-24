/** Browser compatibility mode does not impersonate native certificate pinning. */
import type { PairedProfile } from "./protocol";
export async function loadPairing(): Promise<PairedProfile | null> {
  return null;
}
export async function savePairing(_profile: PairedProfile): Promise<void> {
  throw new Error(
    "QR pairing requires the Android app. Use manual setup in this browser session.",
  );
}
export async function clearPairing(): Promise<void> {
  /* No browser pairing is stored. */
}
