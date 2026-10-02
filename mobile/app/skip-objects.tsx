/**
 * Skip Objects — OrcaSlicer 2.4.2's PartSkipDialog on the phone.
 *
 * Opened from the Status tab's quick actions:
 *   router.push({ pathname: "/skip-objects", params: { printer: <serial> } })
 *
 * The bridge sends the plate's objects and Orca's pick map as run-length rows;
 * the map image is the bridge's rendering in Orca's skip-canvas colours with
 * the current selection. A tap is hit-tested against the rows (the id at that
 * pixel, as SkipPartCanvas does), the list toggles the same states, objects
 * the printer has skipped are locked, and Skip asks Orca's confirmation.
 * Selecting every remaining object stops the print, as in Orca.
 *
 * The POST echoes the job identity the screen was built from and the action
 * the user confirmed; the bridge refuses (409) when either no longer holds.
 * The screen reloads when the printer reports another job or another run.
 */

import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Image, Pressable, ScrollView, Text, View } from "react-native";

import { getSkipMap, getSkipObjects, skipObjects } from "../src/api/control";
import { BridgeError } from "../src/api/errors";
import { Button } from "../src/components/Button";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import {
  applyRefusal,
  confirmText,
  followPrinter,
  hitTest,
  initialStates,
  jobChanged,
  skipRequest,
  toBase64,
  type PartState,
  type SkipObjectsInfo,
} from "../src/lib/skipObjects";
import { useLiveStore } from "../src/store/live";
import { useTheme } from "../src/theme/ThemeProvider";

// A stable fallback: a fresh [] per selector call makes zustand re-render forever.
const NO_IDS: number[] = [];

function message(e: unknown): string {
  return e instanceof BridgeError ? e.envelope.message : String(e);
}

export default function SkipObjectsScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const params = useLocalSearchParams<{ printer?: string }>();
  const printer = Array.isArray(params.printer) ? params.printer[0] : params.printer;
  const reported: number[] = useLiveStore(
    (s) => ((printer ? s.printers[printer]?.snapshot : undefined) as any)?.job?.skipped_objects ?? NO_IDS,
  );
  const reportedKey = reported.join(",");
  const liveSubtask: unknown = useLiveStore(
    (s) => ((printer ? s.printers[printer]?.snapshot : undefined) as any)?._raw?.subtask_name,
  );
  const liveFile: unknown = useLiveStore(
    (s) => ((printer ? s.printers[printer]?.snapshot : undefined) as any)?._raw?.gcode_file,
  );
  const liveStarted: unknown = useLiveStore(
    (s) => ((printer ? s.printers[printer]?.snapshot : undefined) as any)?.job?.started_at,
  );
  const sending = useRef(false);
  const reloadedFor = useRef<string | null>(null);

  const [info, setInfo] = useState<SkipObjectsInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [states, setStates] = useState<Map<number, PartState>>(new Map());
  const [mapUri, setMapUri] = useState<string | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!printer) return;
    setError(null);
    setInfo(null);
    try {
      const data = await getSkipObjects(printer);
      setInfo(data);
      const snap = useLiveStore.getState().printers[printer]?.snapshot as any;
      setStates(initialStates(data, snap?.job?.skipped_objects ?? []));
    } catch (e) {
      setError(message(e));
    }
  }, [printer]);

  useEffect(() => { load(); }, [load]);

  // Another job on the printer: reload, once per job it reports, so a bridge
  // answer that still disagrees with the live snapshot cannot loop.
  useEffect(() => {
    if (!info || !jobChanged(info, { subtask_name: liveSubtask, gcode_file: liveFile }, liveStarted)) {
      return;
    }
    const key = `${String(liveSubtask)}|${String(liveFile)}|${String(liveStarted)}`;
    if (reloadedFor.current === key) return;
    reloadedFor.current = key;
    load();
  }, [info, liveSubtask, liveFile, liveStarted, load]);

  // PartSkipDialog::UpdatePartsStateFromPrinter.
  useEffect(() => {
    setStates((prev) => followPrinter(prev, reportedKey ? reportedKey.split(",").map(Number) : []));
  }, [reportedKey]);

  const checked = useMemo(() => [...states].filter(([, s]) => s === "checked").map(([id]) => id), [states]);
  const skipped = useMemo(() => [...states].filter(([, s]) => s === "skipped").map(([id]) => id), [states]);
  const checkedKey = checked.join(",");
  const skippedKey = skipped.join(",");

  useEffect(() => {
    if (!printer || !info?.map) return;
    let live = true;
    getSkipMap(printer, checkedKey ? checkedKey.split(",").map(Number) : [],
      skippedKey ? skippedKey.split(",").map(Number) : [], info.digest)
      .then((bytes) => { if (live) setMapUri(`data:image/png;base64,${toBase64(bytes)}`); })
      .catch(() => { /* the list still works without the picture */ });
    return () => { live = false; };
  }, [printer, info, checkedKey, skippedKey]);

  const ids = useMemo(() => new Set(states.keys()), [states]);
  const open = [...states.values()].filter((s) => s !== "skipped").length;
  const allChecked = open > 0 && [...states.values()].every((s) => s !== "unchecked");
  const refusal = info ? applyRefusal(info, states) : "";

  function toggle(id: number) {
    setStates((prev) => {
      const s = prev.get(id);
      if (s === undefined || s === "skipped") return prev;
      return new Map(prev).set(id, s === "checked" ? "unchecked" : "checked");
    });
  }

  function selectAll() {
    setStates((prev) => new Map([...prev].map(([id, s]) =>
      [id, s === "skipped" ? s : allChecked ? "unchecked" : "checked"] as [number, PartState])));
  }

  function apply() {
    if (!printer || !info || refusal || !checked.length || sending.current) return;
    // One confirm and one POST at a time, from the first tap.
    sending.current = true;
    setBusy(true);
    const done = () => { sending.current = false; setBusy(false); };
    const text = confirmText(states);
    const body = skipRequest(info, states);
    Alert.alert(text.title, text.body, [
      { text: "Cancel", style: "cancel", onPress: done },
      {
        text: "Continue",
        style: "destructive",
        onPress: async () => {
          try {
            const result = await skipObjects(printer, body);
            showToast(result.action === "stop" ? "Stopping…" : `Skipping ${body.obj_list.length} objects`,
              { severity: "success" });
            router.back();
          } catch (e) {
            showToast(message(e), { severity: "danger" });
          } finally {
            done();
          }
        },
      },
    ], { cancelable: true, onDismiss: done });
  }

  if (!printer) {
    return <View style={{ flex: 1, padding: space.lg }}><Text style={[type.body, { color: c.muted }]}>No printer selected.</Text></View>;
  }
  if (error) {
    return (
      <View style={{ flex: 1, padding: space.lg, gap: space.md }}>
        <Text style={[type.body, { color: c.text }]}>Load skipping objects information failed. Please try again.</Text>
        <Text style={[type.small, { color: c.muted }]}>{error}</Text>
        <Button label="Retry" onPress={load} />
      </View>
    );
  }
  if (!info) {
    return <View style={{ flex: 1, padding: space.lg }}><Text style={[type.body, { color: c.muted }]}>Loading…</Text></View>;
  }

  const box = (s: PartState | undefined) => (
    <Ionicons name={s === "unchecked" ? "square-outline" : "checkbox"} size={22}
      color={s === "skipped" ? c.muted : s === "checked" ? c.danger : c.text} />
  );

  return (
    <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.md }}>
      {info.map ? (
        <Pressable
          accessibilityLabel="Plate map"
          onLayout={(e) => setSize({ width: e.nativeEvent.layout.width, height: e.nativeEvent.layout.height })}
          onPress={(e) => {
            const id = hitTest(info.map, ids, e.nativeEvent.locationX, e.nativeEvent.locationY, size.width, size.height);
            if (id) toggle(id);
          }}
          style={{ width: "100%", aspectRatio: info.map.width / info.map.height }}
        >
          {mapUri && <Image source={{ uri: mapUri }} style={{ width: "100%", height: "100%" }} resizeMode="stretch" />}
        </Pressable>
      ) : (
        <Text style={[type.small, { color: c.muted }]}>This print file has no object map; pick objects from the list.</Text>
      )}

      <Surface padded style={{ gap: space.sm }}>
        <Pressable onPress={selectAll} disabled={!open} style={{ flexDirection: "row", gap: space.sm, alignItems: "center" }}>
          {box(allChecked ? "checked" : "unchecked")}
          <Text style={[type.body, { color: c.text }]}>Select All</Text>
        </Pressable>
        {info.objects.map((o) => (
          <Pressable key={o.id} onPress={() => toggle(o.id)} disabled={states.get(o.id) === "skipped"}
            accessibilityLabel={o.name || `Object ${o.id}`}
            style={{ flexDirection: "row", gap: space.sm, alignItems: "center" }}>
            {box(states.get(o.id))}
            <Text style={[type.body, { color: states.get(o.id) === "skipped" ? c.muted : c.text }]}>
              {o.name || `Object ${o.id}`}
            </Text>
          </Pressable>
        ))}
      </Surface>

      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
        <Text style={[type.body, { color: c.text }]}>{checked.length} /{open} Selected</Text>
        <Button label="Skip" variant="danger" disabled={!!refusal || busy} onPress={apply} />
      </View>
      {!!refusal && <Text style={[type.small, { color: c.muted }]}>{refusal}</Text>}
    </ScrollView>
  );
}
