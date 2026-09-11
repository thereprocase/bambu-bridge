import { useIsFocused } from "@react-navigation/native";
import { useEffect, useState } from "react";
import { Text } from "react-native";
import { Button } from "../components/Button";
import { Surface } from "../components/Surface";
import { useTheme } from "../theme/ThemeProvider";
import { startMonitoring, viewingNative } from "./native";

export function MonitorControls({ printer }: { printer: string }) {
  const { c, type, space } = useTheme();
  const focused = useIsFocused();
  const [status, setStatus] = useState<Awaited<ReturnType<typeof viewingNative.monitorStatus>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!focused) return;
    let cancelled = false;
    const refresh = () => { void viewingNative.monitorStatus().then(value => { if (!cancelled) setStatus(value); }).catch(() => {}); };
    refresh(); const timer = setInterval(refresh, 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [focused]);
  async function toggle() {
    setBusy(true); setError("");
    try {
      if (status?.running) await viewingNative.stopMonitor();
      else await startMonitoring(printer);
      setStatus(await viewingNative.monitorStatus());
    } catch (e) { setError(e instanceof Error ? e.message : "Couldn't change monitoring. Try again."); }
    finally { setBusy(false); }
  }
  const state = !status?.running ? "Off" : status.printer !== printer ? "Monitoring another printer"
    : status.state === "connected" ? "On · connected" : status.state === "printer_offline" ? "On · printer offline"
    : "On · reconnecting; alerts may be delayed";
  return <Surface padded style={{ gap: space.sm }}>
    <Text style={[type.h2, { color: c.text }]}>Print alerts · {state}</Text>
    <Text style={[type.small, { color: c.muted }]}>Get notified when a print finishes, pauses, or reports an error—even with the app in the background. Monitoring uses a persistent notification; the camera stays off.</Text>
    <Button label={busy ? "Updating…" : status?.running ? "Stop monitoring" : "Enable print alerts"} disabled={busy} onPress={() => { void toggle(); }} />
    {status?.running && status.batteryRestricted && <>
      <Text style={[type.small, { color: c.muted }]}>Android may delay alerts during deep sleep. Allow the background connection for more reliable alerts while locked.</Text>
      <Button label="Allow background connection" variant="secondary" onPress={() => viewingNative.batterySettings()} />
    </>}
    {error ? <Text style={[type.small, { color: c.danger }]}>{error}</Text> : null}
  </Surface>;
}
