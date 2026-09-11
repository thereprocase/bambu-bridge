/**
 * Root layout — theme provider + bridge bootstrap + global error fallback.
 *
 * On cold launch we synchronously read MMKV for baseUrl/theme (no flash)
 * and asynchronously load the bearer from SecureStore. The index route
 * routes to /settings if there's no baseUrl yet, otherwise to /(tabs).
 */

import { Stack, usePathname } from "expo-router";
import { useEffect, useRef } from "react";
import { ActivityIndicator, View } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { ToastHost } from "../src/components/Toast";
import { qaLog } from "../src/lib/qalog";
import { useBridgeStore } from "../src/store/bridge";
import { ThemeProvider, useTheme } from "../src/theme/ThemeProvider";
import { useConnectionLifecycle } from "../src/viewing/lifecycle";

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <ThemeProvider>
        <Bootstrap />
      </ThemeProvider>
    </SafeAreaProvider>
  );
}

function Bootstrap() {
  useConnectionLifecycle();
  const { c } = useTheme();
  const bootstrap = useBridgeStore((s) => s.bootstrap);
  const bootstrapped = useBridgeStore((s) => s.bootstrapped);
  const pathname = usePathname();
  // Track previous pathname so we only emit on actual transitions, not
  // on re-renders. useRef persists across renders without triggering effects.
  const prevPath = useRef<string | null>(null);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    // Emit on every pathname change (route transition). Route names only —
    // usePathname() already strips query params which may carry tokens.
    if (pathname !== prevPath.current) {
      prevPath.current = pathname;
      qaLog("nav", { to: pathname });
    }
  }, [pathname]);

  if (!bootstrapped) {
    return (
      <View style={{ flex: 1, backgroundColor: c.bg, alignItems: "center", justifyContent: "center" }}>
        <ActivityIndicator color={c.accent} />
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: c.bg }}>
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: c.bg },
          headerTintColor: c.text,
          headerTitleStyle: { fontFamily: "Inter Tight", fontWeight: "600" },
          contentStyle: { backgroundColor: c.bg },
        }}
      >
        <Stack.Screen name="index" options={{ headerShown: false }} />
        <Stack.Screen name="settings" options={{ title: "Settings", presentation: "modal" }} />
        <Stack.Screen name="add-printer" options={{ title: "Add printer", presentation: "modal" }} />
        <Stack.Screen name="config" options={{ title: "Config", presentation: "modal" }} />
        <Stack.Screen
          name="filament-memory"
          options={{ title: "Slot label", presentation: "modal" }}
        />
        {/* Full-screen (not modal) — the WebGL viewer wants the whole screen
            and the stack back-arrow, matching settings/add-printer chrome.
            Title/header colors are overridden in-screen for the dark canvas. */}
        <Stack.Screen name="viewer" options={{ title: "3D · Live" }} />
        <Stack.Screen name="camera" options={{ title: "Camera" }} />
        <Stack.Screen name="connection" options={{ title: "Check connection" }} />
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
      </Stack>
      <ToastHost />
    </View>
  );
}
