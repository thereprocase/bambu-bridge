import { useEffect, useReducer } from "react";
import { Pressable, Text, View } from "react-native";
import { router } from "expo-router";
import type { PrinterSnapshot } from "../api/types";
import { connectionStripLabels } from "../lib/connectionStrip";
import { useLiveStore } from "../store/live";
import { usePrintersStore } from "../store/printers";
import { useNetStore } from "../store/net";
import { useTheme } from "../theme/ThemeProvider";

/** Bridge transport and printer activity are separate signals. Never infer power off. */
export function ConnectionStrip() {
  const { c, type, mode } = useTheme();
  const id = usePrintersStore(s => s.selectedId);
  const live = useLiveStore(s => id ? s.printers[id] : undefined);
  const reach = useNetStore(s => s.reach);
  const [, tick] = useReducer(n => n + 1, 0);
  useEffect(() => { const timer = setInterval(tick, 5000); return () => clearInterval(timer); }, []);
  const { bridge, activity, symbol, connection } = connectionStripLabels(
    (live?.snapshot as unknown as PrinterSnapshot) ?? null, live?.status ?? "closed", !!id, reach);
  const color = activity === "Print failed" ? c.danger : activity === "Paused" ? c.warn : c.text;
  const good = mode === "light" ? "#18743A" : "#7BD69A";
  return (
    <View style={{ flexDirection: "row", backgroundColor: c.surface2, borderBottomWidth: 1, borderTopWidth: 1, borderColor: c.borderSoft }}>
      <Pressable accessibilityRole="button" accessibilityLabel={`Bridge ${bridge}. Check connection`}
        onPress={() => router.push("/connection")}
        style={{ flex: 1, minHeight: 44, padding: 8, flexDirection: "row", alignItems: "center", gap: 6 }}>
        <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: bridge === "Connected" ? good : bridge === "Unreachable" ? c.danger : c.muted }} />
        <Text style={[type.small, { flexShrink: 1, color: c.text }]}>Bridge · {bridge}</Text>
      </Pressable>
      <View accessibilityLabel={`Printer ${activity}. ${connection}`}
        style={{ flex: 1, minHeight: 44, padding: 8, borderLeftWidth: 1, borderColor: c.borderSoft, flexDirection: "row", alignItems: "center", gap: 6 }}>
        <Text style={[type.mono, { color }]}>{symbol}</Text>
        <Text style={[type.small, { flexShrink: 1, color: c.text }]}>Printer · {activity}</Text>
      </View>
    </View>
  );
}
