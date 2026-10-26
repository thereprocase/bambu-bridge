/**
 * SpoolRow — one Spoolman spool as a list row: swatch · name, second line,
 * fill bar · an optional place badge and chevron. The Filament tab's shelf
 * and the Inventory tab both draw spools with it, so a spool looks the same
 * wherever the owner meets it. Children render under the row when `open`.
 */

import { Ionicons } from "@expo/vector-icons";
import { Pressable, Text, View, type ViewStyle } from "react-native";

import type { InventorySpool } from "../api/inventory";
import { LOW_PERCENT, spoolColor, storedPercent } from "../lib/shelf";
import { gramsLeft, spoolLabel } from "../lib/slotInventory";
import { useTheme } from "../theme/ThemeProvider";
import { ProgressBar } from "./ProgressBar";
import { Swatch } from "./Swatch";

export const SPOOL_ROW_SWATCH = 24;
const CHEVRON_W = 16;

interface Props {
  spool: InventorySpool;
  /** The second line; defaults to the grams left. */
  detail?: string;
  /** The printer's tray colour when the spool sits in a slot and it differs. */
  ring?: string | null;
  /** "SLOT 2" / "EXT" on the right; nothing for a stored spool. */
  badge?: string | null;
  swatchSize?: number;
  /** Draws a chevron and reports expanded state; children show when true. */
  expandable?: boolean;
  open?: boolean;
  onPress: () => void;
  /** Appended to the accessibility label after the grams. */
  accessibilityHint?: string;
  style?: ViewStyle;
  children?: React.ReactNode;
}

export function SpoolRow({ spool, detail, ring, badge, swatchSize = SPOOL_ROW_SWATCH, expandable, open, onPress, accessibilityHint, style, children }: Props) {
  const { c, space, type } = useTheme();
  const percent = storedPercent(spool);
  const low = !spool.archived && percent != null && percent < LOW_PERCENT;
  const grams = gramsLeft(spool);
  const line = detail ?? grams;
  const label = [
    spoolLabel(spool),
    grams,
    low ? "running low" : null,
    spool.archived ? "archived" : null,
    badge ? `in ${badge.toLowerCase()}` : null,
    accessibilityHint ?? null,
    expandable ? `${open ? "close" : "open"} actions` : null,
  ].filter(Boolean).join(", ");
  return (
    <View style={style}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityState={expandable ? { expanded: !!open } : undefined}
        onPress={onPress}
        style={{ flexDirection: "row", alignItems: "flex-start", gap: space.md, opacity: spool.archived ? 0.6 : 1 }}
      >
        <View style={{ marginTop: 1 }}>
          <Swatch color={spoolColor(spool)} ring={ring} size={swatchSize} dimmed={spool.archived} />
        </View>
        <View style={{ flex: 1, gap: 2 }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
            <Text style={[type.body, { color: c.text, flex: 1 }]} numberOfLines={2}>{spoolLabel(spool)}</Text>
            {badge && (
              <Text style={[type.caption, { color: c.accent, borderWidth: 1, borderColor: c.accent, paddingHorizontal: 4, paddingVertical: 1 }]}>
                {badge}
              </Text>
            )}
          </View>
          <Text style={[type.small, { color: low ? c.warn : c.muted }]} numberOfLines={1}>
            {low ? `running low · ${line}` : line}
          </Text>
          {percent != null && !spool.archived && <ProgressBar value={percent} tone={low ? "warn" : "accent"} />}
        </View>
        <View style={{ width: CHEVRON_W, alignItems: "center", paddingTop: space.xs }}>
          {expandable && <Ionicons name={open ? "chevron-up" : "chevron-down"} size={CHEVRON_W} color={c.muted} />}
        </View>
      </Pressable>
      {expandable && open && children && (
        <View style={{ marginLeft: swatchSize + space.md, marginTop: space.sm, gap: space.xs, alignItems: "flex-start" }}>
          {children}
        </View>
      )}
    </View>
  );
}
