/**
 * MMKV — small/fast synchronous KV. Used for non-secret state that
 * needs to be read at app launch without an `await` (theme override,
 * current printer id, last-known cache snapshot per printer).
 *
 * Secrets (bearer key) go through SecureStore in `src/lib/secrets.ts`
 * — MMKV is not encrypted by default and we don't want the API key in
 * plain MMKV even on a personal phone.
 *
 * NOTE: MMKV needs the new architecture + a custom dev client; it does
 * NOT work in Expo Go. The whole point of this APK is the custom build,
 * so that's fine — but don't try to dev against Expo Go.
 */

import { createMMKV } from "react-native-mmkv";

// `createMMKV` is the v4 entry point — v3 exposed a constructor directly,
// but v4 moved to a factory + Nitro Modules. Same surface (get/set/delete
// per key), still synchronous.
export const kv = createMMKV({ id: "bambu-bridge-app" });
