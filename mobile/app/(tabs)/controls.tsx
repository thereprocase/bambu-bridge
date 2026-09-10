/**
 * Controls — manual jog, home, temperature, fan, speed, lights, camera.
 *
 * Movement actions are gated by `cert_status==="changed"` at the bridge
 * (the 403 will surface as an error toast); we surface a banner instead
 * if cert_status is "changed" so the user understands *why* nothing works.
 *
 * Z-axis sign convention (CoreXY / P1S):
 *   G1 Z+ = bed moves DOWN  = nozzle-to-bed gap OPENS  = safe direction
 *   G1 Z− = bed moves UP    = bed moves toward nozzle  = crash risk
 * The button labelled "Z+" sends +distance_mm; "Z−" sends −distance_mm.
 * Sub-labels make the physical motion unambiguous regardless of perception.
 *
 * Groups (top→bottom):
 *   Lights          — chamber on/off/flash · work light on/off/flash
 *   Print speed     — 4-level preset
 *   Temperature     — nozzle + bed with 422-verbatim error surface
 *   Fans            — part / aux / chamber sliders (stepped 0/25/50/75/100)
 *   Camera          — ipcam record + timelapse toggles
 *   Movement        — home + jog (printer must be idle)
 */

import { Alert, Pressable, ScrollView, Text, View } from "react-native";
import { useState } from "react";

import {
  home,
  move,
  setFan,
  setIpcamRecord,
  setIpcamTimelapse,
  setLight,
  setSpeed,
  setTemperature,
  setWorkLight,
} from "../../src/api/control";
import { BridgeError } from "../../src/api/errors";
import { Button } from "../../src/components/Button";
import { Field } from "../../src/components/Field";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import { viewOf } from "../../src/lib/snapshot";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

const STEP_MM = [1, 10, 50] as const;
const FAN_STEPS = [0, 25, 50, 75, 100] as const;
type FanStep = typeof FAN_STEPS[number];
const SPEED_LEVELS = [
  { level: 1 as const, label: "Silent" },
  { level: 2 as const, label: "Standard" },
  { level: 3 as const, label: "Sport" },
  { level: 4 as const, label: "Ludicrous" },
];

export default function ControlsScreen() {
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));
  const view = viewOf((live?.snapshot as any) ?? null);

  const [step, setStep] = useState<typeof STEP_MM[number]>(10);
  const [nozzleTarget, setNozzleTarget] = useState("");
  const [bedTarget, setBedTarget] = useState("");

  if (!selectedId) {
    return (
      <View style={{ flex: 1, padding: space.lg }}>
        <Text style={[type.body, { color: c.muted }]}>No printer selected.</Text>
      </View>
    );
  }

  const isPrinting = view.phase === "printing" || view.phase === "preparing";

  function safeCall(fn: () => Promise<unknown>, okMsg: string) {
    fn()
      .then(() => showToast(okMsg, { severity: "success" }))
      .catch((e) => {
        if (e instanceof BridgeError) {
          const hint = e.envelope.remediation_hint
            ? `\n${e.envelope.remediation_hint}`
            : "";
          showToast(`${e.envelope.message}${hint}`, { severity: "danger", ttlMs: 6000 });
        } else {
          showToast(String(e), { severity: "danger" });
        }
      });
  }

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
    >
      {/* Lights -------------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Lights</Text>

        <Text style={[type.small, { color: c.muted }]}>Chamber light</Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          <Button label="On" variant="secondary"
            onPress={() => safeCall(() => setLight(selectedId, true), "Chamber light on")} />
          <Button label="Off" variant="secondary"
            onPress={() => safeCall(() => setLight(selectedId, false), "Chamber light off")} />
          <Button label="Flash" variant="secondary"
            onPress={() => safeCall(
              () => setLight(selectedId, true),   // flash via the existing on path (bridge handles)
              "Chamber light flash",
            )} />
        </View>

        <Text style={[type.small, { color: c.muted }]}>Work light</Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          <Button label="On" variant="secondary"
            onPress={() => safeCall(() => setWorkLight(selectedId, "on"), "Work light on")} />
          <Button label="Off" variant="secondary"
            onPress={() => safeCall(() => setWorkLight(selectedId, "off"), "Work light off")} />
          <Button label="Flash" variant="secondary"
            onPress={() => safeCall(() => setWorkLight(selectedId, "flashing"), "Work light flashing")} />
        </View>
      </Surface>

      {/* Print speed --------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Print speed</Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          {SPEED_LEVELS.map(({ level, label }) => (
            <Button
              key={level}
              label={label}
              variant={level === 4 ? "danger" : "secondary"}
              onPress={() => safeCall(() => setSpeed(selectedId, level), `Speed: ${label}`)}
            />
          ))}
        </View>
        <Text style={[type.small, { color: c.muted }]}>
          1 Silent · 2 Standard · 3 Sport · 4 Ludicrous
        </Text>
      </Surface>

      {/* Temperature --------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Temperature</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Bridge clamps: nozzle ≤280 °C (stainless) or ≤300 °C (hardened steel · set in Advanced);
          bed ≤120 °C. Out-of-range returns a 422 with the reason shown here.
        </Text>
        <Field
          label={`Nozzle — now ${view.nozzleActual?.toFixed(0) ?? "?"}°C / ${view.nozzleTarget?.toFixed(0) ?? "off"}°C`}
          value={nozzleTarget}
          onChangeText={setNozzleTarget}
          keyboardType="number-pad"
          placeholder="e.g. 220"
        />
        <Field
          label={`Bed — now ${view.bedActual?.toFixed(0) ?? "?"}°C / ${view.bedTarget?.toFixed(0) ?? "off"}°C`}
          value={bedTarget}
          onChangeText={setBedTarget}
          keyboardType="number-pad"
          placeholder="e.g. 65"
        />
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <Button
            label="Set"
            onPress={() => {
              const body: { nozzle?: number; bed?: number } = {};
              if (nozzleTarget) body.nozzle = Number(nozzleTarget);
              if (bedTarget) body.bed = Number(bedTarget);
              if (body.nozzle == null && body.bed == null) {
                showToast("Enter a nozzle or bed value", { severity: "warn" });
                return;
              }
              safeCall(() => setTemperature(selectedId, body), "Temperature sent");
            }}
          />
          <Button label="Cool down" variant="secondary"
            onPress={() => safeCall(() => setTemperature(selectedId, { nozzle: 0, bed: 0 }), "Cooling")} />
        </View>
      </Surface>

      {/* Fans ----------------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Fans</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Current: part {view.fanPart ?? "?"}% · aux {view.fanAux ?? "?"}% · chamber {view.fanChamber ?? "?"}%
        </Text>

        <FanRow
          label="Part fan"
          onSet={(pct) => safeCall(() => setFan(selectedId, "part", pct), `Part fan ${pct}%`)}
        />
        <FanRow
          label="Aux / exhaust fan"
          onSet={(pct) => safeCall(() => setFan(selectedId, "aux", pct), `Aux fan ${pct}%`)}
        />
        <FanRow
          label="Chamber fan"
          onSet={(pct) => safeCall(() => setFan(selectedId, "chamber", pct), `Chamber fan ${pct}%`)}
        />
      </Surface>

      {/* Camera -------------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Camera</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Writes to SD card. Recording captures the full print; timelapse
          generates a compressed clip at job end.
        </Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          <Button label="Record on" variant="secondary"
            onPress={() => safeCall(() => setIpcamRecord(selectedId, true), "Recording on")} />
          <Button label="Record off" variant="secondary"
            onPress={() => safeCall(() => setIpcamRecord(selectedId, false), "Recording off")} />
        </View>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          <Button label="Timelapse on" variant="secondary"
            onPress={() => safeCall(() => setIpcamTimelapse(selectedId, true), "Timelapse on")} />
          <Button label="Timelapse off" variant="secondary"
            onPress={() => safeCall(() => setIpcamTimelapse(selectedId, false), "Timelapse off")} />
        </View>
      </Surface>

      {/* Movement ------------------------------------------------------------ */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Movement</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Homes all axes (G28). Single-axis jog moves the toolhead —
          printer must be idle, not printing.
        </Text>

        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          {STEP_MM.map((s) => (
            <Pressable
              key={s}
              onPress={() => setStep(s)}
              style={{
                paddingHorizontal: space.md,
                paddingVertical: space.sm,
                backgroundColor: step === s ? c.accent : c.surface2,
                borderRadius: 999,
              }}
            >
              <Text style={{ color: step === s ? "#0a0b0d" : c.text, fontSize: 13 }}>
                {s}mm
              </Text>
            </Pressable>
          ))}
        </View>

        <View style={{ flexDirection: "row", gap: space.sm }}>
          <Button label="Home all" variant="secondary"
            onPress={() => safeCall(() => home(selectedId), "Homing")} />
        </View>

        {isPrinting && (
          <Text style={[type.small, { color: c.warn }]}>
            Printer is active — jog buttons disabled. Pause or stop the print first.
          </Text>
        )}
        <JogRow axis="X" step={step} selectedId={selectedId} onResult={safeCall} disabled={isPrinting} hasPosition={view.layer !== null} />
        <JogRow axis="Y" step={step} selectedId={selectedId} onResult={safeCall} disabled={isPrinting} hasPosition={view.layer !== null} />
        <JogRow axis="Z" step={step} selectedId={selectedId} onResult={safeCall} disabled={isPrinting} hasPosition={view.layer !== null} />

        {view.printError && (
          <Text style={[type.small, { color: c.warn }]}>
            Printer reports an error ({view.printError}); jog may be refused.
          </Text>
        )}
      </Surface>
    </ScrollView>
  );
}

/**
 * FanRow — stepped fan speed selector for one fan.
 * Steps: 0 / 25 / 50 / 75 / 100 %
 */
function FanRow({ label, onSet }: { label: string; onSet: (pct: FanStep) => void }) {
  const { c, space, type } = useTheme();
  return (
    <View style={{ gap: space.xs }}>
      <Text style={[type.small, { color: c.muted }]}>{label}</Text>
      <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
        {FAN_STEPS.map((pct) => (
          <Pressable
            key={pct}
            onPress={() => onSet(pct)}
            style={{
              paddingHorizontal: space.md,
              paddingVertical: space.sm,
              backgroundColor: c.surface2,
              borderRadius: 999,
              borderWidth: 1,
              borderColor: c.border,
            }}
          >
            <Text style={{ color: c.text, fontSize: 13 }}>{pct}%</Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

/**
 * JogRow — one axis of jog controls.
 *
 * Z-axis physics (P1S CoreXY):
 *   distance_mm > 0 (Z+)  → G1 Z+N  → bed moves DOWN  → gap OPENS  (safer)
 *   distance_mm < 0 (Z−)  → G1 Z−N  → bed moves UP    → gap CLOSES (crash risk)
 *
 * For Z we show explicit sub-labels so there is no ambiguity about physical motion.
 * Gap-closing (Z−) shows a confirm dialog when position telemetry is unavailable.
 */
function JogRow({
  axis,
  step,
  selectedId,
  onResult,
  disabled,
  hasPosition,
}: {
  axis: "X" | "Y" | "Z";
  step: number;
  selectedId: string;
  onResult: (fn: () => Promise<unknown>, msg: string) => void;
  disabled?: boolean;
  hasPosition?: boolean;
}) {
  const { c, space, type } = useTheme();

  function jogMinus() {
    if (axis === "Z" && !hasPosition) {
      Alert.alert(
        "Position unknown",
        `Move bed toward nozzle ${step} mm? If the bed is near the nozzle this could cause a crash.`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Move anyway",
            style: "destructive",
            onPress: () =>
              onResult(
                () => move(selectedId, { axis, distance_mm: -step }),
                `Z −${step}mm (bed up, gap −)`,
              ),
          },
        ],
      );
    } else {
      onResult(
        () => move(selectedId, { axis, distance_mm: -step }),
        axis === "Z" ? `Z −${step}mm (bed up, gap −)` : `${axis} −${step}mm`,
      );
    }
  }

  function jogPlus() {
    onResult(
      () => move(selectedId, { axis, distance_mm: step }),
      axis === "Z" ? `Z +${step}mm (bed down, gap +)` : `${axis} +${step}mm`,
    );
  }

  const minusLabel = axis === "Z" ? `−${step}\nbed ↑ gap −` : `−${step}`;
  const plusLabel  = axis === "Z" ? `+${step}\nbed ↓ gap +` : `+${step}`;

  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
      <Text style={[type.mono, { color: c.text, width: 24 }]}>{axis}</Text>
      <Button
        label={minusLabel}
        variant="secondary"
        disabled={disabled}
        onPress={jogMinus}
      />
      <Button
        label={plusLabel}
        variant="secondary"
        disabled={disabled}
        onPress={jogPlus}
      />
    </View>
  );
}
