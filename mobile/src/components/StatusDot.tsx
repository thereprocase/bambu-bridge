/**
 * The colored dot that appears next to status text. Same three colors
 * as the dashboard headline severity (success/warn/danger) plus a
 * "neutral" muted variant.
 */

import { View } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

export type DotState = "ok" | "warn" | "danger" | "neutral";

export function StatusDot({ state, size = 8 }: { state: DotState; size?: number }) {
  const { c } = useTheme();
  const fill =
    state === "ok" ? c.accent
    : state === "warn" ? c.warn
    : state === "danger" ? c.danger
    : c.muted;
  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: size / 2,
        backgroundColor: fill,
      }}
    />
  );
}
