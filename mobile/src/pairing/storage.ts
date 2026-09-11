import * as SecureStore from "expo-secure-store";
import { parseProfile, type PairedProfile } from "./protocol";
const KEY = "bridge.pairedProfile.v1";
export async function loadPairing(): Promise<PairedProfile | null> {
  const value = await SecureStore.getItemAsync(KEY);
  return value ? parseProfile(value) : null;
}
export async function savePairing(p: PairedProfile): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(p), {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
}
export async function clearPairing(): Promise<void> { await SecureStore.deleteItemAsync(KEY); }
