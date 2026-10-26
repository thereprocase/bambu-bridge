/**
 * Swatch — a spool's colour as a disc. The fill is Spoolman's colour; the
 * optional ring is what the printer reports for the tray when it differs
 * (often left over from an earlier filament). No colour draws as an inset
 * grey disc, so the row still lines up.
 */

import { View } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

interface Props {
  /** #RRGGBB or null. */
  color: string | null;
  ring?: string | null;
  size?: number;
  /** Archived spools draw at half opacity. */
  dimmed?: boolean;
}

export function Swatch({ color, ring, size = 32, dimmed }: Props) {
  const { c } = useTheme();
  return (
    <View
      accessibilityLabel={ring ? "spool colour from Spoolman; the ring is what the printer reports" : undefined}
      style={{
        width: size,
        height: size,
        borderRadius: size / 2,
        backgroundColor: color ?? c.surface3,
        borderWidth: ring ? 3 : 1,
        borderColor: ring ?? c.border,
        opacity: dimmed ? 0.5 : 1,
      }}
    />
  );
}
