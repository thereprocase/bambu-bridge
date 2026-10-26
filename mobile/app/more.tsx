/**
 * Menu — opened by the gear at the top right of every tab. Lists the screens
 * that are not in the bottom bar (Controls, Activity, Advanced), the bridge
 * queue, and Settings. Each entry is one tap; a folded tab opens under the
 * closed menu, a modal replaces it.
 */

import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { Pressable, ScrollView, Text, View } from "react-native";

import { Button } from "../src/components/Button";
import { Surface } from "../src/components/Surface";
import { MENU_ITEMS, type MenuItem } from "../src/lib/moreMenu";
import { useTheme } from "../src/theme/ThemeProvider";

export default function MoreScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();

  function open(item: MenuItem) {
    if (item.tab) router.dismissTo(item.href as never);
    else router.replace(item.href as never);
  }

  return (
    <ScrollView style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
      <Surface>
        {MENU_ITEMS.map((item, index) => (
          <Pressable
            key={item.id}
            accessibilityRole="button"
            accessibilityLabel={item.label}
            accessibilityHint={item.hint}
            onPress={() => open(item)}
            style={({ pressed }) => ({
              flexDirection: "row",
              alignItems: "center",
              gap: space.md,
              padding: space.lg,
              borderTopWidth: index ? 1 : 0,
              borderTopColor: c.borderSoft,
              backgroundColor: pressed ? c.surface2 : "transparent",
            })}
          >
            <Ionicons name={item.icon} size={24} color={c.accent} />
            <View style={{ flex: 1, gap: 2 }}>
              <Text style={[type.body, { color: c.text }]}>{item.label}</Text>
              <Text style={[type.small, { color: c.muted }]}>{item.hint}</Text>
            </View>
            <Ionicons name="chevron-forward" size={18} color={c.muted} />
          </Pressable>
        ))}
      </Surface>
      <Button label="Close" variant="secondary" onPress={() => router.back()} fullWidth />
    </ScrollView>
  );
}
