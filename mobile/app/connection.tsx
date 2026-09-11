import { useEffect, useRef, useState } from "react";
import { ScrollView, Share, Text } from "react-native";
import { Button } from "../src/components/Button";
import { Surface } from "../src/components/Surface";
import { usePrintersStore } from "../src/store/printers";
import { useTheme } from "../src/theme/ThemeProvider";
import { DiagnosticReport, formatDiagnosticReport, runDiagnostics } from "../src/viewing/diagnostics";

export default function ConnectionScreen() {
  const { c, type, space } = useTheme();
  const printer = usePrintersStore(s => s.selectedId);
  const [report, setReport] = useState<DiagnosticReport | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function run() {
    setBusy(true); setReport(null);
    try { await runDiagnostics(printer, next => { if (mounted.current) setReport(next); }); }
    finally { if (mounted.current) setBusy(false); }
  }
  return <ScrollView style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.md }}>
    <Text style={[type.h1, { color: c.text }]}>Check your connection</Text>
    <Text style={[type.body, { color: c.muted }]}>Check each route, access, printer, camera, and viewer. These checks only read status; they do not control the printer.</Text>
    <Button label={busy ? "Checking…" : "Run checks"} disabled={busy} onPress={() => { void run(); }} />
    {report && <Text style={[type.small, { color: c.muted }]}>App {report.appVersion} · Server {report.serverVersion} · {report.route}</Text>}
    {report?.checks.map(check => <Surface key={check.name} padded style={{ gap: space.sm }}>
      <Text style={[type.h2, { color: check.state === "failed" ? c.danger : c.text }]}>{check.name} · {check.state}</Text>
      <Text style={[type.body, { color: c.muted }]}>{check.detail}</Text>
      {check.ms !== undefined && <Text style={[type.small, { color: c.muted }]}>{check.ms} ms</Text>}
    </Surface>)}
    {report && !busy && <>
      <Text style={[type.small, { color: c.muted }]}>The report includes results and versions. It excludes keys, addresses, printer identifiers, and file names.</Text>
      <Button label="Share diagnostic report" variant="secondary" onPress={() => { void Share.share({ message: formatDiagnosticReport(report) }).catch(() => {}); }} />
    </>}
  </ScrollView>;
}
