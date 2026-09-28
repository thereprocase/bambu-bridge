/**
 * Bottom tabs. Six per the design (Status / Print / Files / Controls /
 * Filament / Activity); Settings sits at the top-right of each tab as
 * a gear icon → /settings modal.
 *
 * We trigger a printers.refresh + WS connection here on mount so the
 * tabs all read from the same live store without each one having to
 * fetch itself.
 */

import { Ionicons } from "@expo/vector-icons";
import { Link, Redirect, Tabs } from "expo-router";
import { useEffect } from "react";
import { Pressable, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { ConnectionStrip } from "../../src/components/ConnectionStrip";

import { ReachabilityBanner } from "../../src/components/ReachabilityBanner";
import { useBridgeStore } from "../../src/store/bridge";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function TabsLayout() {
  const { c } = useTheme();
  const baseUrl = useBridgeStore((s) => s.baseUrl);
  const bearer = useBridgeStore((s) => s.bearer);
  const baseUrlLan = useBridgeStore((s) => s.baseUrlLan);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const refresh = usePrintersStore((s) => s.refresh);
  const ensure = useLiveStore((s) => s.ensureConnection);
  const close = useLiveStore((s) => s.closeConnection);

  useEffect(() => {
    refresh();
  }, [refresh, baseUrl, bearer]);

  useEffect(() => {
    if (!selectedId || !baseUrl || !bearer) return;
    close(selectedId);
    ensure(selectedId);
    return () => close(selectedId);
  }, [selectedId, baseUrl, baseUrlLan, bearer, ensure, close]);

  if (!baseUrl || !bearer) return <Redirect href="/settings" />;

  return (
    <SafeAreaView edges={["top", "left", "right"]} style={{ flex: 1, backgroundColor: c.surface2 }}>
      <ConnectionStrip />
      <ReachabilityBanner />
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: c.title },
        headerStatusBarHeight: 0,
        headerTintColor: c.onTitle,
        headerTitleStyle: { fontFamily: "IBMPlexSans_600SemiBold" },
        tabBarLabelStyle: { fontFamily: "IBMPlexSans_600SemiBold" },
        headerShadowVisible: false,
        tabBarStyle: {
          backgroundColor: c.surface2,
          borderTopColor: c.borderSoft,
        },
        tabBarActiveTintColor: c.onTitle,
        tabBarActiveBackgroundColor: c.title,
        tabBarInactiveTintColor: c.muted,
        headerRight: () => <View style={{ flexDirection: "row", alignItems: "center" }}>
          <Link href="/native-recovery" asChild>
            <Pressable accessibilityRole="button" accessibilityLabel="Bridge queue" hitSlop={12}
              style={{ paddingHorizontal: 12 }}>
              <Ionicons name="list-outline" size={24} color={c.onTitle} />
            </Pressable>
          </Link>
          <Link href="/settings" asChild>
            <Pressable accessibilityRole="button" accessibilityLabel="Settings" hitSlop={12}
              style={{ paddingHorizontal: 16 }}>
              <Ionicons name="settings-outline" size={22} color={c.onTitle} />
            </Pressable>
          </Link>
        </View>,
      }}
    >
      <Tabs.Screen
        name="status"
        options={{
          title: "Status",
          tabBarIcon: ({ color }) => <Ionicons name="pulse" color={color} size={22} />,
        }}
      />
      <Tabs.Screen
        name="print"
        options={{
          title: "Print",
          tabBarIcon: ({ color }) => <Ionicons name="play-circle" color={color} size={22} />,
        }}
      />
      <Tabs.Screen
        name="controls"
        options={{
          title: "Controls",
          tabBarIcon: ({ color }) => <Ionicons name="options-outline" color={color} size={22} />,
        }}
      />
      <Tabs.Screen
        name="filament"
        options={{
          title: "Filament",
          tabBarIcon: ({ color }) => <Ionicons name="layers-outline" color={color} size={22} />,
        }}
      />
      <Tabs.Screen
        name="activity"
        options={{
          title: "Activity",
          tabBarIcon: ({ color }) => <Ionicons name="time-outline" color={color} size={22} />,
        }}
      />
      <Tabs.Screen
        name="advanced"
        options={{
          title: "Advanced",
          tabBarIcon: ({ color }) => <Ionicons name="flask-outline" color={color} size={22} />,
        }}
      />
    </Tabs>
    </SafeAreaView>
  );
}
