/**
 * Progress bar — accent fill on the surface3 track. Used for print
 * progress, AMS spool levels, and lifetime stats.
 */

import { View } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

interface Props {
  /** 0-100 */
  value: number;
  height?: number;
  tone?: "accent" | "warn" | "danger" | "muted";
}

export function ProgressBar({ value, height = 6, tone = "accent" }: Props) {
  const { c, radius } = useTheme();
  const clamped = Math.max(0, Math.min(100, value));
  const fill =
    tone === "accent" ? c.accent
    : tone === "warn" ? c.warn
    : tone === "danger" ? c.danger
    : c.muted;
  return (
    <View
      style={{
        height,
        backgroundColor: c.surface3,
        borderRadius: radius.pill,
        overflow: "hidden",
      }}
    >
      <View
        style={{
          height: "100%",
          width: `${clamped}%`,
          backgroundColor: fill,
          borderRadius: radius.pill,
        }}
      />
    </View>
  );
}
