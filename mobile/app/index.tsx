/**
 * Index — the post-bootstrap router. No UI of its own; just decides where
 * the user lands.
 *
 *   - No baseUrl: → /settings (configure bridge first)
 *   - baseUrl but no bearer: → /settings (need API key)
 *   - All set, no printers: → /(tabs) then prompt to add
 *   - All set, printers: → /(tabs)
 */

import { Redirect } from "expo-router";

import { useBridgeStore } from "../src/store/bridge";

export default function Index() {
  const baseUrl = useBridgeStore((s) => s.baseUrl);
  const bearer = useBridgeStore((s) => s.bearer);

  if (!baseUrl || !bearer) {
    return <Redirect href="/settings" />;
  }
  return <Redirect href="/(tabs)/status" />;
}
