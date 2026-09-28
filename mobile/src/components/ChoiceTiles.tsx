import { useState } from "react";
import { Pressable, ScrollView, Text, useWindowDimensions, View } from "react-native";
import { useTheme } from "../theme/ThemeProvider";

export interface TileChoice {
  id: string;
  label: string;
  topLabel?: string;
  color?: string;
  disabled?: boolean;
  accessibilityLabel?: string;
}

export function tileWidth(width: number, count: number, fontScale: number): number {
  return Math.max(48 * Math.max(1, fontScale), (width - Math.max(0, count - 1) * 4) / Math.max(1, count));
}

/** Equal two-line targets. Large text and narrow screens scroll as one row. */
export function ChoiceTiles({ label, options, value, onSelect }: {
  label: string; options: TileChoice[]; value?: string; onSelect: (id: string) => void;
}) {
  const { c, type } = useTheme();
  const { fontScale } = useWindowDimensions();
  const [width, setWidth] = useState(0);
  const size = tileWidth(width, options.length, fontScale);
  return (
    <View onLayout={event => setWidth(event.nativeEvent.layout.width)}>
      <ScrollView horizontal showsHorizontalScrollIndicator accessibilityLabel={label}
        contentContainerStyle={{ gap: 4, alignItems: "stretch" }}>
        {options.map(option => {
          const selected = value === option.id;
          const foreground = selected ? c.onAccent : c.text;
          return <Pressable key={option.id}
            accessibilityRole={value === undefined ? "button" : "radio"}
            accessibilityLabel={option.accessibilityLabel ?? [option.topLabel, option.label].filter(Boolean).join(" ")}
            accessibilityState={{ ...(value === undefined ? {} : { checked: selected }), disabled: !!option.disabled }}
            disabled={option.disabled} onPress={() => onSelect(option.id)}
            style={({ pressed }) => ({
              width: size, minHeight: 72, paddingHorizontal: 4, paddingVertical: 10,
              justifyContent: "center", alignItems: "center", gap: 6,
              borderWidth: 1, borderColor: selected ? c.accent : c.border,
              backgroundColor: selected ? c.accent : c.surface2,
              opacity: option.disabled ? 0.4 : pressed ? 0.75 : 1,
            })}>
            {option.color ? <View style={{ width: 18, height: 18, borderRadius: 9,
              backgroundColor: option.color, borderWidth: 1, borderColor: c.muted }} />
              : option.topLabel ? <Text style={[type.small, { color: foreground, textAlign: "center" }]}>{option.topLabel}</Text> : null}
            <Text style={[type.small, { color: foreground, textAlign: "center" }]}>{option.label}</Text>
          </Pressable>;
        })}
      </ScrollView>
    </View>
  );
}
