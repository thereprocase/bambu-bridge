/**
 * FilterChips — one horizontal row of single-select chips with an "All"
 * chip first. Each chip can carry a count. Used by the Inventory tab's find
 * bar for material, vendor and place; also as the sort control (no "All").
 */

import { Pressable, ScrollView, Text } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

export interface Chip {
  id: string;
  label: string;
  count?: number;
}

interface Props {
  label: string;
  chips: Chip[];
  /** null selects the "All" chip. */
  value: string | null;
  onSelect: (id: string | null) => void;
  /** Omit the "All" chip (a sort control always has a value). */
  allLabel?: string | null;
}

export function FilterChips({ label, chips, value, onSelect, allLabel = "All" }: Props) {
  const { c, space, type } = useTheme();
  const all: Chip[] = allLabel ? [{ id: "", label: allLabel }] : [];
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} accessibilityLabel={label} contentContainerStyle={{ gap: space.xs }}>
      {[...all, ...chips].map((chip) => {
        const selected = chip.id === "" ? value == null : value === chip.id;
        return (
          <Pressable
            key={chip.id || "__all"}
            accessibilityRole="radio"
            accessibilityState={{ checked: selected }}
            accessibilityLabel={`${label}: ${chip.label}${chip.count != null ? `, ${chip.count}` : ""}`}
            onPress={() => onSelect(chip.id === "" ? null : chip.id)}
            style={({ pressed }) => ({
              flexDirection: "row",
              alignItems: "baseline",
              gap: 4,
              paddingHorizontal: space.md,
              paddingVertical: space.xs + 2,
              borderWidth: 1,
              borderColor: selected ? c.accent : c.border,
              backgroundColor: selected ? c.accent : c.surface2,
              opacity: pressed ? 0.75 : 1,
            })}
          >
            <Text style={[type.small, { color: selected ? c.onAccent : c.text }]}>{chip.label}</Text>
            {chip.count != null && (
              <Text style={[type.caption, { color: selected ? c.onAccent : c.muted }]}>{chip.count}</Text>
            )}
          </Pressable>
        );
      })}
    </ScrollView>
  );
}
