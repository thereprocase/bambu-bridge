/**
 * ReachabilityBanner — slim full-width banner that appears only when the app
 * cannot reach the bridge on either path (reach === "down").
 *
 * Mounted once in the tabs layout so every tab gets it without each screen
 * having to subscribe. When down, it probes expo-network lazily (on mount and
 * on each "down" transition) to give the user a targeted action: join Wi-Fi /
 * enable Tailscale when the phone does have a network, or a no-network hint
 * when the device is offline entirely. The probe is not on a timer — it only
 * runs when the banner becomes visible, so it costs nothing during normal
 * operation.
 *
 * Color/style tokens from the design system (theme/tokens.ts).
 */

import * as Network from "expo-network";
import { useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";

import { useNetStore } from "../store/net";
import { useTheme } from "../theme/ThemeProvider";

export function ReachabilityBanner() {
  const { c, space, type } = useTheme();
  const reach = useNetStore((s) => s.reach);
  const [hasNetwork, setHasNetwork] = useState<boolean | null>(null);

  // Probe connectivity lazily when the banner becomes relevant.
  useEffect(() => {
    if (reach !== "down") {
      // Not down — reset probe so a future "down" transition re-probes fresh.
      setHasNetwork(null);
      return;
    }
    let cancelled = false;
    Network.getNetworkStateAsync()
      .then((state) => {
        if (!cancelled) setHasNetwork(state.isConnected ?? false);
      })
      .catch(() => {
        if (!cancelled) setHasNetwork(null);
      });
    return () => {
      cancelled = true;
    };
  }, [reach]);

  if (reach !== "down") return null;

  const hint =
    hasNetwork === false
      ? "Phone has no network connection."
      : "Join home Wi-Fi or turn on Tailscale — showing cached data.";

  return (
    <View
      style={[
        styles.banner,
        {
          backgroundColor: c.dangerDeep,
          borderBottomColor: c.danger,
          paddingHorizontal: space.lg,
          paddingVertical: space.sm,
        },
      ]}
    >
      <Text style={[type.small, { color: c.danger, fontWeight: "600" }]}>
        Can&apos;t reach the bridge
      </Text>
      <Text style={[type.small, { color: c.muted }]}>{hint}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    width: "100%",
    borderBottomWidth: 1,
    gap: 2,
  },
});
