/**
 * Surface — the basic card shape used everywhere. Matches the design
 * mock's tonal hierarchy: `surface` for primary cards, `surface2` for
 * nested rows, `surface3` for inset elements.
 */

import { View, ViewProps } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

interface Props extends ViewProps {
  tone?: "surface" | "surface2" | "surface3";
  padded?: boolean;
}

export function Surface({ tone = "surface", padded = false, style, ...rest }: Props) {
  const { c, radius, space } = useTheme();
  const bg =
    tone === "surface" ? c.surface : tone === "surface2" ? c.surface2 : c.surface3;
  return (
    <View
      style={[
        {
          backgroundColor: bg,
          borderRadius: radius.lg,
          borderWidth: 1,
          borderColor: c.borderSoft,
          padding: padded ? space.lg : 0,
        },
        style,
      ]}
      {...rest}
    />
  );
}
