/**
 * Field — label + text input. Used in settings + onboarding + AMS-slot
 * dialogs. Single-line by default; pass `multiline` for the rare
 * multiline case (none currently in this app, but keep the option).
 */

import { TextInput, TextInputProps, Text, View } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

interface Props extends TextInputProps {
  label: string;
  hint?: string;
  error?: string | null;
}

export function Field({ label, hint, error, style, ...rest }: Props) {
  const { c, radius, space, type } = useTheme();
  return (
    <View style={{ gap: space.xs }}>
      <Text style={[type.caption, { color: c.muted, textTransform: "uppercase" }]}>
        {label}
      </Text>
      <TextInput
        placeholderTextColor={c.muted}
        style={[
          type.body,
          {
            color: c.text,
            backgroundColor: c.surface2,
            borderColor: error ? c.danger : c.border,
            borderWidth: 1,
            borderRadius: radius.md,
            paddingHorizontal: space.md,
            paddingVertical: space.md,
            minHeight: 44,
          },
          style,
        ]}
        autoCorrect={false}
        autoCapitalize="none"
        {...rest}
      />
      {(hint || error) && (
        <Text
          style={[
            type.small,
            { color: error ? c.danger : c.muted, marginTop: 2 },
          ]}
        >
          {error || hint}
        </Text>
      )}
    </View>
  );
}
