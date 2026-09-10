/**
 * Filament-memory editor (modal) — set or forget the human label remembered
 * for one AMS slot.
 *
 * Why a label at all: tagless spools (no RFID) carry only a filament-TYPE code
 * and color; on the AMS list they read as an anonymous "PLA"/blank. The bridge
 * remembers an operator-set make/model/profile per slot (server-side, survives
 * WS drops and the slot emptying) so the spool reads as e.g.
 * "Polymaker PolyLite ASA". This screen is that editor.
 *
 * Opened from the Filament tab by tapping a slot:
 *   router.push({ pathname: "/filament-memory",
 *                 params: { printer: <serial>, slot: "1" } })
 *
 * It seeds the three fields from the slot's current `memory` in the live
 * snapshot (read live, so a Save's immediate snapshot update is reflected if
 * the user reopens). Save → PUT, Forget → DELETE-with-confirm. Save is disabled
 * while all three fields are empty (the bridge rejects an all-empty body).
 */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useMemo, useState } from "react";
import { Alert, KeyboardAvoidingView, Platform, ScrollView, Text, View } from "react-native";

import { BridgeError } from "../src/api/errors";
import {
  clearFilamentMemory,
  FILAMENT_MEMORY_MAX,
  setFilamentMemory,
} from "../src/api/filament";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { useToastStore } from "../src/components/Toast";
import { viewOf } from "../src/lib/snapshot";
import { useLiveStore } from "../src/store/live";
import { useTheme } from "../src/theme/ThemeProvider";

function firstParam(v: string | string[] | undefined): string | undefined {
  if (v == null) return undefined;
  const s = Array.isArray(v) ? v[0] : v;
  return s && s.length ? s : undefined;
}

export default function FilamentMemoryEditor() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const params = useLocalSearchParams();

  const printerId = firstParam(params.printer);
  const slotNum = Number(firstParam(params.slot));
  const slotValid = Number.isInteger(slotNum) && slotNum >= 1;

  // Read the slot live so the seed reflects the current server label (and any
  // immediate snapshot update after a prior Save). Find by physical slot, not
  // array index, so a re-ordered/partial slots array can't mis-target.
  const live = useLiveStore((s) => (printerId ? s.printers[printerId] : undefined));
  const slot = useMemo(() => {
    const view = viewOf((live?.snapshot as any) ?? null);
    return view.ams.find((sl) => sl.physicalSlot === slotNum) ?? null;
  }, [live?.snapshot, slotNum]);

  const existing = slot?.memory ?? null;
  const liveType = slot && !slot.empty ? slot.name ?? null : null;

  const [make, setMake] = useState(existing?.make ?? "");
  const [model, setModel] = useState(existing?.model ?? "");
  const [profile, setProfile] = useState(existing?.profile ?? "");
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);

  // expo-router reuses modal screen instances across navigations — the
  // component does NOT remount when the user closes slot 1 and opens slot 2.
  // useState initializers only run once (on first mount), so without a reset
  // effect the fields retain the previous slot's text. Reseed whenever the
  // target slot changes (slotNum) so each slot always opens with its own
  // saved values, or empty fields when none exist.
  useEffect(() => {
    setMake(existing?.make ?? "");
    setModel(existing?.model ?? "");
    setProfile(existing?.profile ?? "");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slotNum]);

  const allEmpty = !make.trim() && !model.trim() && !profile.trim();
  // Reflect the bridge's per-field ≤120-char cap inline rather than via a 422.
  const tooLong =
    make.length > FILAMENT_MEMORY_MAX ||
    model.length > FILAMENT_MEMORY_MAX ||
    profile.length > FILAMENT_MEMORY_MAX;
  const busy = saving || clearing;

  // A misformed deep-link / lost param: don't render dead fields, explain it.
  if (!printerId || !slotValid) {
    return (
      <View style={{ flex: 1, backgroundColor: c.bg, padding: space.lg, gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Slot label</Text>
        <Text style={[type.body, { color: c.muted }]}>
          Couldn&apos;t tell which slot to edit. Go back and tap the slot again.
        </Text>
        <Button label="Back" variant="secondary" onPress={() => router.back()} fullWidth />
      </View>
    );
  }

  async function save() {
    if (allEmpty || tooLong || busy) return;
    setSaving(true);
    try {
      // Send only filled fields; the bridge stores exactly what it's given.
      await setFilamentMemory(printerId!, slotNum, {
        make: make.trim() || undefined,
        model: model.trim() || undefined,
        profile: profile.trim() || undefined,
      });
      showToast(`Slot ${slotNum} label saved`, { severity: "success" });
      router.back();
    } catch (e) {
      showToast(e instanceof BridgeError ? e.envelope.message : String(e), {
        severity: "danger",
      });
    } finally {
      setSaving(false);
    }
  }

  function confirmClear() {
    Alert.alert(
      `Forget slot ${slotNum} label?`,
      "The slot will fall back to showing its live filament type.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Forget",
          style: "destructive",
          onPress: async () => {
            setClearing(true);
            try {
              await clearFilamentMemory(printerId!, slotNum);
              showToast(`Slot ${slotNum} label cleared`, { severity: "success" });
              router.back();
            } catch (e) {
              showToast(e instanceof BridgeError ? e.envelope.message : String(e), {
                severity: "danger",
              });
            } finally {
              setClearing(false);
            }
          },
        },
      ],
    );
  }

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: c.bg }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
        <View style={{ gap: space.xs }}>
          <Text style={[type.h1, { color: c.text }]}>Slot {slotNum} label</Text>
          {/* Show the live type so the operator knows what's physically loaded
              while they name it — never "unknown"/"null", just omit when absent. */}
          {liveType ? (
            <Text style={[type.small, { color: c.muted }]}>
              Loaded now: {liveType}
            </Text>
          ) : slot?.empty ? (
            <Text style={[type.small, { color: c.muted }]}>
              This slot is empty — you can still set a label for when a spool
              goes in.
            </Text>
          ) : null}
        </View>

        <View style={{ gap: space.md }}>
          <Field
            label="Make"
            value={make}
            onChangeText={setMake}
            placeholder="Polymaker"
            autoCapitalize="words"
            maxLength={FILAMENT_MEMORY_MAX}
            editable={!busy}
          />
          <Field
            label="Model"
            value={model}
            onChangeText={setModel}
            placeholder="PolyLite"
            autoCapitalize="words"
            maxLength={FILAMENT_MEMORY_MAX}
            editable={!busy}
          />
          <Field
            label="Profile"
            value={profile}
            onChangeText={setProfile}
            placeholder="ASA"
            autoCapitalize="characters"
            maxLength={FILAMENT_MEMORY_MAX}
            editable={!busy}
            hint="Cleared automatically when this slot's material type changes."
          />
        </View>

        <View style={{ gap: space.sm }}>
          <Button
            label="Save"
            onPress={save}
            disabled={allEmpty || tooLong || busy}
            loading={saving}
            fullWidth
          />
          {/* Forget only makes sense when a label exists today. */}
          {existing && (
            <Button
              label="Clear / Forget"
              variant="danger"
              onPress={confirmClear}
              loading={clearing}
              disabled={busy}
              fullWidth
            />
          )}
          <Button
            label="Cancel"
            variant="secondary"
            onPress={() => router.back()}
            disabled={busy}
            fullWidth
          />
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
