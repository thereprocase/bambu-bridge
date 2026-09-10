/**
 * Bearer key storage — expo-secure-store on top of the platform keystore
 * (Android Keystore / iOS Keychain). Survives app updates, not backed up
 * to cloud by default, encrypted at rest.
 *
 * The operator's bearer key never goes near MMKV, AsyncStorage, or any
 * file we write — only here. Read-once at app launch and held in the
 * Zustand store; SecureStore is the source of truth.
 */

import * as SecureStore from "expo-secure-store";

const KEY = "bridge.bearer";

export async function loadBearer(): Promise<string | null> {
  return await SecureStore.getItemAsync(KEY);
}

export async function saveBearer(value: string): Promise<void> {
  await SecureStore.setItemAsync(KEY, value, {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
}

export async function clearBearer(): Promise<void> {
  await SecureStore.deleteItemAsync(KEY);
}
