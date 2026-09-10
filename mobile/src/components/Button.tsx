/**
 * Button — three variants: primary (accent fill), secondary (surface w/
 * border), danger (red fill). Disabled state dims to muted.
 *
 * Hand-rolled because react-native-paper's looks don't match the design
 * mock's "industrial machine" aesthetic. The mock uses a rectangular pill
 * with a single-pixel border and no shadow — easy enough to spell out.
 */

import { ActivityIndicator, Pressable, Text, ViewStyle } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

interface Props {
  label: string;
  onPress: () => void;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  loading?: boolean;
  fullWidth?: boolean;
  style?: ViewStyle;
}

export function Button({
  label,
  onPress,
  variant = "primary",
  disabled,
  loading,
  fullWidth,
  style,
}: Props) {
  const { c, radius, space, type } = useTheme();

  const palette =
    variant === "primary"
      ? { bg: c.accent, fg: "#0a0b0d", border: c.accent }
      : variant === "danger"
        ? { bg: c.danger, fg: "#0a0b0d", border: c.danger }
        : { bg: c.surface2, fg: c.text, border: c.border };

  return (
    <Pressable
      disabled={disabled || loading}
      onPress={onPress}
      // A scroll swipe that begins on the button must NOT navigate. Setting a
      // short unstable_pressDelay (100 ms) means a fast flick-swipe (finger
      // lifts before the delay elapses) never reaches onPress, while a
      // deliberate tap (users naturally hold 150–250 ms) always does.
      unstable_pressDelay={100}
      style={({ pressed }) => [
        {
          backgroundColor: disabled ? c.surface3 : palette.bg,
          borderColor: disabled ? c.borderSoft : palette.border,
          borderWidth: 1,
          borderRadius: radius.md,
          paddingVertical: space.md,
          paddingHorizontal: space.lg,
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "row",
          gap: space.sm,
          opacity: pressed ? 0.85 : 1,
          minHeight: 44,
          alignSelf: fullWidth ? "stretch" : "flex-start",
        },
        style,
      ]}
    >
      {loading && <ActivityIndicator size="small" color={palette.fg} />}
      <Text
        style={[
          type.h2,
          { color: disabled ? c.muted : palette.fg, fontSize: 15 },
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}
