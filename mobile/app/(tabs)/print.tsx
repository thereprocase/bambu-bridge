import { CapabilityCard } from "../../src/components/CapabilityCard";
import { printerCapabilities } from "../../src/lib/capabilities";
/**
 * Print — browse remote files on the printer's FTPS share and submit a
 * print. Real endpoints only (operator no-stubs directive):
 *   - GET /printers/{id}/files?dir= — listing (root; .gcode and .3mf live here)
 *   - POST /jobs — submit
 *
 * AMS slot picker is shown only when the printer has loaded slots; pick
 * one or "auto" (no `ams_mapping` → printer uses currently-loaded).
 */

import { useEffect, useRef, useState } from "react";
import { Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import { BridgeError } from "../../src/api/errors";
import { listFiles, type RemoteFile } from "../../src/api/files";
import { qaLog } from "../../src/lib/qalog";
import { canStartStoredPrint, submitPrint } from "../../src/api/jobs";
import { Button } from "../../src/components/Button";
import { ChoiceTiles } from "../../src/components/ChoiceTiles";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import { viewOf } from "../../src/lib/snapshot";
import { printerStateLabels } from "../../src/lib/printerState";
import type { PrinterSnapshot } from "../../src/api/types";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function PrintScreen() {
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));
  const registeredModel = usePrintersStore((s) => s.list.find((p) => p.serial === selectedId)?.model);
  const capabilities = printerCapabilities(live?.snapshot, registeredModel);
  const view = viewOf((live?.snapshot as any) ?? null);
  const [, setClock] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);
  const facts = printerStateLabels((live?.snapshot as unknown as PrinterSnapshot) ?? null,
    live?.status ?? "closed");

  // Currently-printing job name from live store (§6 job.subtask_name).
  const activeJobName: string | null = (live?.snapshot as any)?.job?.subtask_name ?? null;

  const [files, setFiles] = useState<RemoteFile[]>([]);
  const [filesPrinter, setFilesPrinter] = useState<string | null>(null);
  const currentPrinter = useRef(selectedId);
  currentPrinter.current = selectedId;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const submissionActive = useRef(false);
  const canPrint = facts.live && canStartStoredPrint(live?.snapshot ?? null, true);
  const [pickedSlot, setPickedSlot] = useState<number | null>(null);

  async function refresh() {
    if (!selectedId) return;
    setLoading(true);
    setError(null);
    try {
      const arr = await listFiles(selectedId);
      if (selectedId !== currentPrinter.current) return;
      // Server returns files newest-first; preserve that order — no re-sort.
      // Filter to .gcode and .3mf only (P1S can't print other formats).
      const filtered = arr.filter((f) => /\.3mf$/i.test(f.name));
      setFiles(filtered);
      setFilesPrinter(selectedId);
      qaLog("files.order", {
        dir: "",
        count: filtered.length,
        dated_count: filtered.filter((f) => f.modified_at || f.sliced_at).length,
      });
    } catch (e) {
      if (selectedId !== currentPrinter.current) return;
      const message = e instanceof BridgeError ? e.envelope.message : String(e);
      setError(message);
      qaLog("files.error", { code: e instanceof BridgeError ? e.code : "network_error" });
    } finally {
      if (selectedId === currentPrinter.current) setLoading(false);
    }
  }

  useEffect(() => {
    setFiles([]);
    setFilesPrinter(null);
    setPickedSlot(null);
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  async function submit(f: RemoteFile) {
    if (!selectedId || selectedId !== filesPrinter || !canPrint || submissionActive.current) return;
    submissionActive.current = true;
    setSubmitting(f.name);
    try {
      const res = await submitPrint({
        printer_id: selectedId,
        filename: f.name,
        ams_mapping: pickedSlot != null ? [pickedSlot] : undefined,
      });
      showToast(`Job ${res.job_id} submitted`, { severity: "success" });
    } catch (e) {
      showToast(
        e instanceof BridgeError ? e.envelope.message : String(e),
        { severity: "danger" }
      );
    } finally {
      submissionActive.current = false;
      setSubmitting(null);
    }
  }

  if (!selectedId) {
    return (
      <View style={{ flex: 1, padding: space.lg }}>
        <Text style={[type.body, { color: c.muted }]}>Add a printer in Settings.</Text>
      </View>
    );
  }

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={<RefreshControl refreshing={loading} tintColor={c.accent} onRefresh={refresh} />}
    >
      <CapabilityCard title="Print" availability={capabilities.get("core")}>
      {/* AMS slot picker --------------------------------------------------- */}
      {view.ams.length > 0 && (
        <Surface padded style={{ gap: space.sm }}>
          <Text style={[type.h2, { color: c.text }]}>AMS slot</Text>
          <Text style={[type.small, { color: c.muted }]}>
            External uses the external spool. Select an AMS slot for a compatible single-filament file.
          </Text>
          <ChoiceTiles label="AMS slot" value={pickedSlot == null ? "auto" : String(pickedSlot)}
            onSelect={id => setPickedSlot(id === "auto" ? null : Number(id))}
            options={[
              { id: "auto", topLabel: "Spool", label: "External", accessibilityLabel: "External spool" },
              ...view.ams.map(slot => ({ id: String(slot.physicalSlot), label: String(slot.physicalSlot),
                topLabel: "AMS", color: slot.color ?? undefined, disabled: slot.empty || slot.physicalSlot > 4,
                accessibilityLabel: `AMS slot ${slot.physicalSlot}${slot.empty ? ", empty" : ""}` })),
            ]} />
        </Surface>
      )}

      {/* File list --------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <View style={{ flexDirection: "row", alignItems: "center" }}>
          <Text style={[type.h2, { color: c.text, flex: 1 }]}>Files on printer</Text>
          <Pressable onPress={refresh}>
            <Text style={[type.small, { color: c.accent }]}>Refresh</Text>
          </Pressable>
        </View>
        {!canPrint && <Text style={[type.small, { color: c.muted }]}>
          Printer: {facts.activity} · Connection: {facts.connection}
        </Text>}
        {error && <Text style={[type.small, { color: c.danger }]}>{error}</Text>}
        {!loading && !error && files.length === 0 && (
          <Text style={[type.small, { color: c.muted }]}>No .3mf files yet.</Text>
        )}
        {sortedFiles(filesPrinter === selectedId ? files : [], activeJobName).map((f, idx) => {
          const fileBase = basename(f.name);
          const activeBase = activeJobName != null ? basename(activeJobName) : "";
          const isPrinting = facts.live && ["Printing", "Preparing", "Paused"].includes(facts.activity) && activeJobName != null && (
            fileBase === activeBase ||
            (stem(activeBase).length > 0 && stem(fileBase) === stem(activeBase))
          );
          return (
            <View
              key={f.name}
              style={{
                flexDirection: "row",
                alignItems: "center",
                gap: space.md,
                borderTopWidth: idx > 0 ? 1 : 0,
                borderTopColor: c.borderSoft,
                paddingTop: idx > 0 ? space.md : 0,
              }}
            >
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm, flexWrap: "wrap" }}>
                  <Text style={[type.body, { color: c.text }]}>{f.name}</Text>
                  {isPrinting && (
                    <View style={{
                      backgroundColor: c.accent,
                      paddingHorizontal: 6,
                      paddingVertical: 2,
                      borderRadius: 0,
                    }}>
                      <Text style={{ color: c.onAccent, fontSize: 11, fontWeight: "600" }}>
                        printing now
                      </Text>
                    </View>
                  )}
                </View>
                {f.size != null && (
                  <Text style={[type.caption, { color: c.muted }]}>{formatSize(f.size)}</Text>
                )}
                {fileDateLine(f) != null && (
                  <Text style={[type.caption, { color: c.muted }]}>{fileDateLine(f)}</Text>
                )}
              </View>
              <Button
                label={submitting === f.name ? "Submitting…" : "Print"}
                onPress={() => submit(f)}
                loading={submitting === f.name}
                disabled={!canPrint || submitting !== null}
              />
            </View>
          );
        })}
      </Surface>
      </CapabilityCard>
    </ScrollView>
  );
}

/** Extract the bare filename (no directory) from a path like "/sdcard/foo.3mf". */
function basename(p: string): string {
  return p.replace(/.*[/\\]/, "");
}

/**
 * Strip a trailing print-file extension (.gcode.3mf, .gcode, .3mf — case-insensitive)
 * from a bare filename. Returns the input unchanged when no extension matches.
 * Guard: returns empty string only when the input itself is empty.
 */
function stem(name: string): string {
  return name.replace(/\.(gcode\.3mf|gcode|3mf)$/i, "");
}

/**
 * Returns files with the currently-printing file pinned at position 0.
 * Matching strategy:
 *   1. Exact basename match (cheap, no false positives).
 *   2. Stem match — Bambu's job.subtask_name omits the file extension, so
 *      "weather_station_reflector" must match "weather_station_reflector.gcode.3mf".
 *      Only applied when the stem is non-empty.
 * Server order is preserved for all other entries (no re-sort).
 */
function sortedFiles(files: RemoteFile[], activeJobName: string | null): RemoteFile[] {
  if (!activeJobName) return files;
  const active = basename(activeJobName);
  // 1. Exact basename match.
  let idx = files.findIndex((f) => basename(f.name) === active);
  // 2. Stem fallback — only when no exact match and the stem is non-empty.
  if (idx === -1) {
    const activeStem = stem(active);
    if (activeStem) {
      idx = files.findIndex((f) => stem(basename(f.name)) === activeStem);
    }
  }
  if (idx <= 0) return files; // already first or not found
  const out = [...files];
  const [pinned] = out.splice(idx, 1);
  out.unshift(pinned);
  return out;
}


/**
 * Returns a short secondary label for a file based on its sort_basis and
 * the corresponding timestamp, e.g. "sliced 5/19" or "modified 6/1".
 * Returns null when sort_basis is "none", absent, or no timestamp is available.
 */
function fileDateLine(f: RemoteFile): string | null {
  const basis = f.sort_basis;
  if (!basis || basis === "none") return null;
  let ts: string | null | undefined;
  if (basis === "sliced") ts = f.sliced_at;
  else if (basis === "modified") ts = f.modified_at;
  else if (basis === "created") ts = f.created_at;
  if (!ts) return null;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  const month = d.getMonth() + 1;
  const day = d.getDate();
  return `${basis} ${month}/${day}`;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
