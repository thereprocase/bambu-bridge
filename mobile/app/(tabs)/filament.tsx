/**
 * Filament — AMS slots (live, from snapshot) and off-AMS spool inventory
 * (CRUD against /spools).
 *
 * AMS rows are read-only here — load/unload from slot is via the
 * `/printers/{id}/ams/change` and `/filament/unload` endpoints, surfaced
 * as buttons next to each occupied slot.
 */

import { useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { Alert, Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import { Ionicons } from "@expo/vector-icons";

import { amsChange, unloadFilament } from "../../src/api/control";
import { BridgeError } from "../../src/api/errors";
import { createSpool, deleteSpool, listSpools } from "../../src/api/jobs";
import { Spool } from "../../src/api/types";
import { Button } from "../../src/components/Button";
import { Field } from "../../src/components/Field";
import { ProgressBar } from "../../src/components/ProgressBar";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import { AmsSlot, SlotMemory, viewOf } from "../../src/lib/snapshot";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

/** Compose the operator label into a single human line, skipping empty fields:
 * {make:"Polymaker",model:"PolyLite",profile:"ASA"} → "Polymaker PolyLite ASA".
 * Returns null only if there's nothing to show (caller falls back to type). */
function memoryLabel(m: SlotMemory | null | undefined): string | null {
  if (!m) return null;
  const parts = [m.make, m.model, m.profile].map((s) => s.trim()).filter(Boolean);
  return parts.length ? parts.join(" ") : null;
}

/** The dim second line under a labelled slot: the profile (if any) plus the
 * raw live filament type, so the operator can still see what the printer
 * actually reports. Never renders "unknown"/"null" — empties are dropped. */
function slotSubLabel(slot: AmsSlot): string | null {
  const parts: string[] = [];
  const profile = slot.memory?.profile?.trim();
  if (profile) parts.push(profile);
  if (!slot.empty && slot.name) parts.push(slot.name);
  return parts.length ? parts.join(" · ") : null;
}

export default function FilamentScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));
  const view = viewOf((live?.snapshot as any) ?? null);

  const [spools, setSpools] = useState<Spool[]>([]);
  const [loading, setLoading] = useState(false);

  // Hold the last non-empty AMS slot view so an RFID re-scan (which transiently
  // empties `ams.slots` while the hardware stays attached, §6.1.1) renders the
  // previous slots dimmed instead of flashing empty / "gibberish".
  const [lastSlots, setLastSlots] = useState<typeof view.ams>([]);
  useEffect(() => {
    if (view.ams.length > 0) setLastSlots(view.ams);
  }, [view.ams]);

  // During a re-scan (`amsPresent && no slots`) fall back to the held view.
  const rescanning = view.amsPresent && view.ams.length === 0;
  const slotsToRender = view.ams.length > 0 ? view.ams : rescanning ? lastSlots : [];

  // New spool form
  const [newName, setNewName] = useState("");
  const [newMat, setNewMat] = useState("");
  const [newColor, setNewColor] = useState("");
  const [newRemaining, setNewRemaining] = useState("");

  async function refresh() {
    setLoading(true);
    try {
      setSpools(await listSpools());
    } catch (e) {
      showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function safeCall(fn: () => Promise<unknown>, okMsg: string) {
    fn()
      .then(() => showToast(okMsg, { severity: "success" }))
      .catch((e) => showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" }));
  }

  async function add() {
    if (!newName.trim() || !newMat.trim()) {
      showToast("Name and material required", { severity: "warn" });
      return;
    }
    try {
      await createSpool({
        name: newName.trim(),
        material: newMat.trim(),
        color_hex: newColor.trim() || undefined,
        remaining_g: newRemaining ? Number(newRemaining) : undefined,
      });
      setNewName(""); setNewMat(""); setNewColor(""); setNewRemaining("");
      await refresh();
      showToast("Spool added", { severity: "success" });
    } catch (e) {
      showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" });
    }
  }

  function remove(s: Spool) {
    Alert.alert("Delete spool?", s.name, [
      { text: "Cancel", style: "cancel" },
      {
        text: "Delete", style: "destructive",
        onPress: async () => {
          try {
            await deleteSpool(s.id);
            await refresh();
          } catch (e) {
            showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" });
          }
        }
      },
    ]);
  }

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={<RefreshControl refreshing={loading} tintColor={c.accent} onRefresh={refresh} />}
    >
      {/* AMS ---------------------------------------------------------------- */}
      {selectedId && (
        <Surface padded style={{ gap: space.md }}>
          <Text style={[type.h2, { color: c.text }]}>AMS</Text>
          {view.amsUnits.map((unit, i) => (
            <Text key={unit.id} style={[type.small, { color: c.muted }]}>
              {live?.status !== "open" ? "Last known · " : ""}AMS {Number(unit.id) + 1 || i + 1} · {unit.humidityPct == null ? "Humidity unavailable" : `${unit.humidityPct}% RH`} · {unit.temperatureC == null ? "Temperature unavailable" : `${unit.temperatureC.toFixed(1)} °C`}
            </Text>
          ))}
          {/* §6.1.1 state machine:
              !amsPresent             → no AMS hardware
              amsPresent + slots      → render slots
              amsPresent + no slots   → RFID re-scan; hold previous slots dimmed */}
          {!view.amsPresent ? (
            <Text style={[type.small, { color: c.muted }]}>
              No AMS detected on this printer.
            </Text>
          ) : (
            <>
              {rescanning && (
                <Text style={[type.small, { color: c.muted }]}>
                  AMS re-scanning — slot data returns in a few seconds.
                </Text>
              )}
              {slotsToRender.map((slot) => {
                const label = memoryLabel(slot.memory);
                const sub = slotSubLabel(slot);
                // Primary line: the operator label when set (fixes tagless
                // spools reading as gibberish), else the live type, else a
                // plain slot-state word — never "unknown"/"null".
                const primary = label
                  ? label
                  : slot.empty
                    ? "Empty"
                    : slot.name ?? "Filament loaded";
                return (
                  <Pressable
                    key={slot.physicalSlot}
                    // Tap the slot to set/forget its label. Disabled mid-rescan
                    // (slot identity is in flux) to avoid editing a stale slot.
                    onPress={
                      rescanning
                        ? undefined
                        : () =>
                            router.push({
                              pathname: "/filament-memory",
                              params: { printer: selectedId, slot: String(slot.physicalSlot) },
                            })
                    }
                    style={{
                      flexDirection: "row",
                      alignItems: "center",
                      gap: space.md,
                      opacity: rescanning ? 0.4 : 1,
                      borderTopWidth: slot.physicalSlot > 1 ? 1 : 0,
                      borderTopColor: c.borderSoft,
                      paddingTop: slot.physicalSlot > 1 ? space.md : 0,
                    }}
                  >
                    <View
                      style={{
                        width: 32, height: 32, borderRadius: 16,
                        backgroundColor: slot.color ?? c.surface3,
                        borderWidth: 1, borderColor: c.border,
                      }}
                    />
                    <View style={{ flex: 1, gap: 2 }}>
                      <Text style={[type.caption, { color: c.muted, textTransform: "uppercase" }]}>
                        Slot {slot.physicalSlot}
                      </Text>
                      <Text style={[type.body, { color: c.text }]} numberOfLines={1}>
                        {primary}
                      </Text>
                      {/* Secondary: profile + raw live type, dim. Only when it
                          adds info beyond the primary line. */}
                      {sub && sub !== primary && (
                        <Text style={[type.small, { color: c.muted }]} numberOfLines={1}>
                          {sub}
                        </Text>
                      )}
                      {slot.remainingPercent != null && (
                        <ProgressBar
                          value={slot.remainingPercent}
                          tone={slot.remainingPercent < 15 ? "warn" : "accent"}
                        />
                      )}
                    </View>
                    {!slot.empty && !rescanning && (
                      <Button
                        label="Load"
                        variant="secondary"
                        onPress={() => safeCall(
                          () => amsChange(selectedId, { target_tray: slot.physicalSlot - 1 }),
                          `Loading slot ${slot.physicalSlot}`
                        )}
                      />
                    )}
                    {/* Edit affordance — a pencil so the whole row reads as
                        tappable. Hidden during a rescan (row is non-interactive). */}
                    {!rescanning && (
                      <Ionicons name="pencil" size={16} color={c.muted} />
                    )}
                  </Pressable>
                );
              })}
            </>
          )}
          {!rescanning && slotsToRender.some((s) => !s.empty) && (
            <Button
              label="Unload current filament"
              variant="secondary"
              onPress={() => safeCall(() => unloadFilament(selectedId), "Unloading")}
            />
          )}
        </Surface>
      )}

      {/* Off-AMS spool inventory ------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Spool inventory</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Spools not loaded into the AMS — track what you have on the shelf.
        </Text>
        {spools.length === 0 && (
          <Text style={[type.small, { color: c.muted }]}>None yet.</Text>
        )}
        {spools.map((s) => (
          <View
            key={s.id}
            style={{
              flexDirection: "row", alignItems: "center", gap: space.md,
              borderTopWidth: 1, borderTopColor: c.borderSoft, paddingTop: space.md,
            }}
          >
            <View
              style={{
                width: 28, height: 28, borderRadius: 14,
                backgroundColor: s.color_hex ?? c.surface3,
                borderWidth: 1, borderColor: c.border,
              }}
            />
            <View style={{ flex: 1 }}>
              <Text style={[type.body, { color: c.text }]}>{s.name}</Text>
              <Text style={[type.caption, { color: c.muted }]}>
                {s.material}{s.remaining_g != null ? ` · ${s.remaining_g}g` : ""}
              </Text>
            </View>
            <Button label="Delete" variant="danger" onPress={() => remove(s)} />
          </View>
        ))}
      </Surface>

      {/* Add spool --------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Add spool</Text>
        <Field label="Name" value={newName} onChangeText={setNewName} placeholder="PLA Matte Charcoal" />
        <Field label="Material" value={newMat} onChangeText={setNewMat} placeholder="PLA Matte · 1.75" />
        <Field label="Color (hex)" value={newColor} onChangeText={setNewColor} placeholder="#2a2a2e" />
        <Field
          label="Remaining (g)"
          value={newRemaining}
          onChangeText={setNewRemaining}
          placeholder="1000"
          keyboardType="number-pad"
        />
        <Button label="Add" onPress={add} fullWidth />
      </Surface>
    </ScrollView>
  );
}
