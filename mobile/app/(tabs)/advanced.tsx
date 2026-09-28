/**
 * Advanced — machine-level controls that the everyday operator rarely needs.
 *
 * Risk tiers follow the server's P1S-CONTROL-MATRIX:
 *   GREEN  — XCam toggles, get_version, RFID re-read
 *   YELLOW — print_option flags, AMS filament/drying/user settings,
 *            skip_objects, set_accessories (nozzle type/diameter),
 *            calibration (P1S-confirmed bits only)
 *   RED    — extrude/retract, steppers-off   → always confirm dialog
 *   BLACK  — raw G-code console             → visible but gated (403 path)
 *
 * Sections:
 *   1. XCam AI     — spaghetti/first-layer/airprint + print_halt toggle
 *   2. Print opts  — auto_recovery, air_print_detect, filament_tangle,
 *                    nozzle_blob, sound
 *   3. AMS ops     — filament setting (color/material/temps for untagged),
 *                    RFID re-read, drying, user settings
 *   4. Skip obj    — integer list entry + send
 *   5. Calibration — P1S-confirmed options (1/2/4/7 bitmask)
 *   6. Nozzle      — set_accessories nozzle type + diameter
 *   7. Firmware    — get_version info card (reads from live snapshot)
 *   8. Extrude     — distance presets ±10/±50/±100, feedrate 120/300/600 RED
 *   9. Steppers    — M84 with confirm dialog              RED
 *  10. Raw G-code  — gated console (shows 403 message verbatim if disabled)
 */

import { CapabilityCard, CapabilityList } from "../../src/components/CapabilityCard";
import { printerCapabilities } from "../../src/lib/capabilities";
import { Alert, ScrollView, Text, TextInput, View } from "react-native";
import { useState } from "react";

import {
  amsDrying,
  amsFilamentSetting,
  amsRfidRead,
  amsUserSetting,
  calibrate,
  extrude,
  getVersion,
  setNozzle,
  setPrintOption,
  setXcam,
  skipObjects,
  steppersOff,
  sendRawGcode,
} from "../../src/api/control";
import { BridgeError } from "../../src/api/errors";
import { Button } from "../../src/components/Button";
import { Field } from "../../src/components/Field";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

// ── XCam module list (P1S-confirmed per CONTROL-MATRIX §9) ───────────────────
const XCAM_MODULES = [
  { name: "spaghetti_detector",   label: "Spaghetti detect" },
  { name: "first_layer_inspector", label: "First layer inspect" },
  { name: "airprint_detector",    label: "Air-print detect" },
  { name: "printing_monitor",     label: "Print monitor" },
  { name: "pileup_detector",      label: "Pileup detect" },
  { name: "clump_detector",       label: "Clump detect" },
] as const;

// ── Print-option flag list ────────────────────────────────────────────────────
const PRINT_OPTIONS = [
  { key: "auto_recovery",          label: "Step-loss recovery" },
  { key: "air_print_detect",       label: "Air-print detect" },
  { key: "filament_tangle_detect", label: "Filament tangle detect" },
  { key: "nozzle_blob_detect",     label: "Nozzle blob detect" },
  { key: "sound_enable",           label: "Sound / beep" },
] as const;

const EXTRUDE_DISTANCES = [10, 50, 100] as const;
const FEEDRATES = [120, 300, 600] as const;
type Feedrate = typeof FEEDRATES[number];

export default function AdvancedScreen() {
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));

  const registeredModel = usePrintersStore((s) => s.list.find((p) => p.serial === selectedId)?.model);
  const capabilities = printerCapabilities(live?.snapshot, registeredModel);

  // ── XCam ──────────────────────────────────────────────────────────────────
  const [xcamHalt, setXcamHalt] = useState(true);

  // ── AMS filament setting ──────────────────────────────────────────────────
  const [fsAmsId, setFsAmsId] = useState("0");
  const [fsTrayId, setFsTrayId] = useState("0");
  const [fsColor, setFsColor] = useState("FF0000FF");
  const [fsMaterial, setFsMaterial] = useState("PLA");
  const [fsTempMin, setFsTempMin] = useState("190");
  const [fsTempMax, setFsTempMax] = useState("240");

  // ── AMS RFID ──────────────────────────────────────────────────────────────
  const [rfidAmsId, setRfidAmsId] = useState("0");
  const [rfidSlotId, setRfidSlotId] = useState("0");

  // ── AMS drying ────────────────────────────────────────────────────────────
  const [dryAmsId, setDryAmsId] = useState("0");
  const [dryTemp, setDryTemp] = useState("55");
  const [dryCoolTemp, setDryCoolTemp] = useState("35");
  const [dryDuration, setDryDuration] = useState("240");
  const [dryHumidity, setDryHumidity] = useState("4");

  // ── AMS user setting ──────────────────────────────────────────────────────
  const [usAmsId, setUsAmsId] = useState("0");

  // ── Skip objects ──────────────────────────────────────────────────────────
  const [skipInput, setSkipInput] = useState("");

  // ── Calibration ───────────────────────────────────────────────────────────

  // ── Nozzle accessories ────────────────────────────────────────────────────
  const [nozzleType, setNozzleType] = useState<"stainless_steel" | "hardened_steel">("stainless_steel");
  const [nozzleDiam, setNozzleDiam] = useState<0.2 | 0.4 | 0.6 | 0.8>(0.4);

  // ── Extrude ───────────────────────────────────────────────────────────────
  const [feedrate, setFeedrate] = useState<Feedrate>(300);

  // ── Raw G-code ────────────────────────────────────────────────────────────
  const [rawGcode, setRawGcode] = useState("");
  const [rawGcodeError, setRawGcodeError] = useState<string | null>(null);

  // Version info from live snapshot raw
  const rawSnap = live?.snapshot as any;
  const infoModules: any[] | undefined = rawSnap?._raw?.info?.module ?? rawSnap?.info?.module;

  if (!selectedId) {
    return (
      <View style={{ flex: 1, padding: space.lg }}>
        <Text style={[type.body, { color: c.muted }]}>No printer selected.</Text>
      </View>
    );
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function safeCall(fn: () => Promise<unknown>, okMsg: string, after?: () => void) {
    fn()
      .then(() => {
        showToast("Command sent", { severity: "success" });
        after?.();
      })
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

  /** Confirm dialog for RED-tier actions. */
  function confirmRed(
    title: string,
    body: string,
    actionLabel: string,
    fn: () => Promise<unknown>,
    okMsg: string,
  ) {
    Alert.alert(title, body, [
      { text: "Cancel", style: "cancel" },
      { text: actionLabel, style: "destructive", onPress: () => safeCall(fn, okMsg) },
    ]);
  }

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
    >
      <CapabilityList>
      {/* 1. XCam AI --------------------------------------------------------- */}
      <CapabilityCard title="XCam AI vision" availability={capabilities.get("vision")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>XCam AI vision</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Toggles the P1S camera-based detection modules. &quot;Print halt&quot;
          auto-pauses the print when the module fires.
        </Text>

        {/* print_halt toggle */}
        <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
          <Button
            label={xcamHalt ? "Halt on detect: ON" : "Halt on detect: OFF"}
            variant="secondary"
            onPress={() => setXcamHalt((v) => !v)}
          />
        </View>

        {XCAM_MODULES.map(({ name, label }) => (
          <View key={name} style={{ flexDirection: "row", gap: space.sm }}>
            <Button
              label={`${label}: on`}
              variant="secondary"
              onPress={() => safeCall(
                () => setXcam(selectedId, name, true, xcamHalt),
                `${label} enabled`,
              )}
            />
            <Button
              label={`off`}
              variant="secondary"
              onPress={() => safeCall(
                () => setXcam(selectedId, name, false, false),
                `${label} disabled`,
              )}
            />
          </View>
        ))}
      </Surface>
      </CapabilityCard>

      {PRINT_OPTIONS.map(({ key, label }) => (
        <CapabilityCard key={key} title={label} availability={capabilities.get(
          key === "auto_recovery" ? "autoRecovery" : key === "air_print_detect" ? "airPrint" :
          key === "filament_tangle_detect" ? "tangle" : key === "nozzle_blob_detect" ? "blob" : "sound"
        )}>
          <Surface padded style={{ gap: space.md }}>
            <Text style={[type.h2, { color: c.text }]}>{label}</Text>
            <View style={{ flexDirection: "row", gap: space.sm }}>
              <Button label="Enable" variant="secondary"
                onPress={() => safeCall(() => setPrintOption(selectedId, { [key]: true }), "Request sent")} />
              <Button label="Disable" variant="secondary"
                onPress={() => safeCall(() => setPrintOption(selectedId, { [key]: false }), "Request sent")} />
            </View>
          </Surface>
        </CapabilityCard>
      ))}

      {/* 3. AMS ops --------------------------------------------------------- */}
      <CapabilityCard title="AMS — filament setting" availability={capabilities.get("ams")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>AMS — filament setting</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Override the filament profile for an untagged spool. ams_id and
          tray_id are 0-based. Color is 8-char RRGGBBAA hex.
        </Text>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <View style={{ flex: 1 }}>
            <Field label="AMS id" value={fsAmsId} onChangeText={setFsAmsId} keyboardType="number-pad" />
          </View>
          <View style={{ flex: 1 }}>
            <Field label="Tray id (0-3)" value={fsTrayId} onChangeText={setFsTrayId} keyboardType="number-pad" />
          </View>
        </View>
        <Field label="Color RRGGBBAA" value={fsColor} onChangeText={setFsColor} placeholder="FF0000FF" />
        <Field label="Material type" value={fsMaterial} onChangeText={setFsMaterial} placeholder="PLA" />
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <View style={{ flex: 1 }}>
            <Field label="Nozzle temp min" value={fsTempMin} onChangeText={setFsTempMin} keyboardType="number-pad" />
          </View>
          <View style={{ flex: 1 }}>
            <Field label="Nozzle temp max" value={fsTempMax} onChangeText={setFsTempMax} keyboardType="number-pad" />
          </View>
        </View>
        <Button
          label="Set filament"
          onPress={() => safeCall(
            () => amsFilamentSetting(selectedId, {
              ams_id: Number(fsAmsId),
              tray_id: Number(fsTrayId),
              tray_info_idx: "",
              tray_color: fsColor,
              nozzle_temp_min: Number(fsTempMin),
              nozzle_temp_max: Number(fsTempMax),
              tray_type: fsMaterial,
            }),
            "Filament setting sent",
          )}
        />

      </Surface>
      </CapabilityCard>
      <CapabilityCard title="AMS — RFID re-read" availability={capabilities.get("ams")}>
      <Surface padded style={{ gap: space.md }}>

        <Text style={[type.h2, { color: c.text }]}>AMS — RFID re-read</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Reads the filament tag in the selected AMS slot.
        </Text>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <View style={{ flex: 1 }}>
            <Field label="AMS id" value={rfidAmsId} onChangeText={setRfidAmsId} keyboardType="number-pad" />
          </View>
          <View style={{ flex: 1 }}>
            <Field label="Slot id (0-3)" value={rfidSlotId} onChangeText={setRfidSlotId} keyboardType="number-pad" />
          </View>
        </View>
        <Button
          label="Re-read RFID"
          onPress={() => safeCall(
            () => amsRfidRead(selectedId, Number(rfidAmsId), Number(rfidSlotId)),
            "RFID re-read triggered",
          )}
        />

      </Surface>
      </CapabilityCard>
      <CapabilityCard title="AMS — drying" availability={capabilities.get("drying")}>
      <Surface padded style={{ gap: space.md }}>

        <Text style={[type.h2, { color: c.text }]}>AMS — drying</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Starts a drying cycle. Requires AMS firmware support.
        </Text>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <View style={{ flex: 1 }}>
            <Field label="AMS id" value={dryAmsId} onChangeText={setDryAmsId} keyboardType="number-pad" />
          </View>
          <View style={{ flex: 1 }}>
            <Field label="Temp (°C)" value={dryTemp} onChangeText={setDryTemp} keyboardType="number-pad" />
          </View>
        </View>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <View style={{ flex: 1 }}>
            <Field label="Cool temp (°C)" value={dryCoolTemp} onChangeText={setDryCoolTemp} keyboardType="number-pad" />
          </View>
          <View style={{ flex: 1 }}>
            <Field label="Duration (min)" value={dryDuration} onChangeText={setDryDuration} keyboardType="number-pad" />
          </View>
        </View>
        <Field label="Humidity level (1-5)" value={dryHumidity} onChangeText={setDryHumidity} keyboardType="number-pad" />
        <Button
          label="Start drying"
          onPress={() => safeCall(
            () => amsDrying(selectedId, {
              ams_id: Number(dryAmsId),
              temp: Number(dryTemp),
              cooling_temp: Number(dryCoolTemp),
              duration: Number(dryDuration),
              humidity: Number(dryHumidity),
            }),
            "Drying started",
          )}
        />

      </Surface>
      </CapabilityCard>
      <CapabilityCard title="AMS — user settings" availability={capabilities.get("ams")}>
      <Surface padded style={{ gap: space.md }}>

        <Text style={[type.h2, { color: c.text }]}>AMS — user settings</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Configure RFID auto-read behaviour per AMS unit.
        </Text>
        <Field label="AMS id" value={usAmsId} onChangeText={setUsAmsId} keyboardType="number-pad" />
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          <Button label="Startup and insertion read: on" variant="secondary"
            onPress={() => safeCall(
              () => amsUserSetting(selectedId, Number(usAmsId), true, true),
              "AMS user settings sent",
            )} />
          <Button label="Startup and insertion read: off" variant="secondary"
            onPress={() => safeCall(
              () => amsUserSetting(selectedId, Number(usAmsId), false, false),
              "AMS user settings sent",
            )} />
        </View>
      </Surface>
      </CapabilityCard>

      {/* 4. Skip objects ---------------------------------------------------- */}
      <CapabilityCard title="Skip objects" availability={capabilities.get("skipObjects")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Skip objects</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Cancel specific objects mid-print without stopping the job. Enter
          comma-separated Bambu object IDs from the slice (integers).
        </Text>
        <Field
          label="Object IDs (comma-separated)"
          value={skipInput}
          onChangeText={setSkipInput}
          keyboardType="number-pad"
          placeholder="0,1,3"
        />
        <Button
          label="Skip objects"
          disabled={!skipInput.trim()}
          onPress={() => {
            const tokens = skipInput.split(",").map((s) => s.trim());
            const ids = tokens.map(Number);
            if (!tokens.every((s) => /^\d+$/.test(s)) || !ids.every(Number.isSafeInteger)) {
              showToast("Enter at least one valid integer ID", { severity: "warn" });
              return;
            }
            safeCall(
              () => skipObjects(selectedId, ids),
              `Skipping ${ids.length} object(s)`,
              () => setSkipInput(""),
            );
          }}
        />
      </Surface>
      </CapabilityCard>

      {/* 5. Calibration ----------------------------------------------------- */}
      <CapabilityCard title="Calibration" availability={capabilities.get("calibration")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Calibration</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Bed leveling and vibration calibration require an idle printer.
        </Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          {([2, 4, 6] as const).map((opt) => {
            const labels: Record<number, string> = {
              2: "Bed level",
              4: "Vibration",
              6: "Bed + vibration",
            };
            return (
              <Button
                key={opt}
                label={labels[opt]}
                variant="secondary"
                onPress={() => {
                  safeCall(
                    () => calibrate(selectedId, opt),
                    `Calibration ${labels[opt]} started`,
                  );
                }}
              />
            );
          })}
        </View>
      </Surface>
      </CapabilityCard>

      {/* 6. Nozzle accessories ---------------------------------------------- */}
      <CapabilityCard title="Nozzle type &amp; diameter" availability={capabilities.get("nozzleSetup")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Nozzle type & diameter</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Tell the bridge which nozzle is installed. Hardened steel unlocks
          the 300 °C temperature clamp; stainless is clamped at 280 °C.
        </Text>

        <Text style={[type.small, { color: c.muted }]}>Nozzle type</Text>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <Button
            label="Stainless"
            variant={nozzleType === "stainless_steel" ? "primary" : "secondary"}
            onPress={() => setNozzleType("stainless_steel")}
          />
          <Button
            label="Hardened"
            variant={nozzleType === "hardened_steel" ? "primary" : "secondary"}
            onPress={() => setNozzleType("hardened_steel")}
          />
        </View>

        <Text style={[type.small, { color: c.muted }]}>Nozzle diameter</Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          {([0.2, 0.4, 0.6, 0.8] as const).map((d) => (
            <Button
              key={d}
              label={`${d} mm`}
              variant={nozzleDiam === d ? "primary" : "secondary"}
              onPress={() => setNozzleDiam(d)}
            />
          ))}
        </View>

        <Button
          label={`Set: ${nozzleType === "hardened_steel" ? "Hardened" : "Stainless"} ${nozzleDiam} mm`}
          onPress={() => safeCall(
            () => setNozzle(selectedId, nozzleType, nozzleDiam),
            `Nozzle set: ${nozzleType} ${nozzleDiam} mm`,
          )}
        />
      </Surface>
      </CapabilityCard>

      {/* 7. Firmware / version info card ------------------------------------ */}
      <CapabilityCard title="Firmware info" availability={capabilities.get("identity")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Firmware info</Text>
        <Button
          label="Refresh version"
          variant="secondary"
          onPress={() => safeCall(() => getVersion(selectedId), "Version refresh requested")}
        />
        {Array.isArray(infoModules) && infoModules.length > 0 ? (
          infoModules.map((m: any, i: number) => (
            <View key={i} style={{ gap: 2 }}>
              <Text style={[type.body, { color: c.text }]}>
                {m?.name ?? m?.module_name ?? `module ${i}`}
              </Text>
              <Text style={[type.caption, { color: c.muted }]}>
                {m?.sw_ver ?? m?.version ?? "—"}
                {m?.hw_ver ? ` · hw ${m.hw_ver}` : ""}
              </Text>
            </View>
          ))
        ) : (
          <Text style={[type.small, { color: c.muted }]}>
            Tap &quot;Refresh version&quot; to populate. Module info appears in the
            snapshot after the printer echoes it.
          </Text>
        )}
      </Surface>
      </CapabilityCard>

      {/* 8. Extrude / retract (RED) ----------------------------------------- */}
      <CapabilityCard title="Extrude / Retract" availability={capabilities.get("motion")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.danger }]}>Extrude / Retract</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Manual filament movement. Server enforces: printer must be idle or
          paused, homed, and nozzle ≥ 170 °C. Distance ≤ 100 mm per call.
          Errors returned verbatim from the bridge.
        </Text>

        <Text style={[type.small, { color: c.muted }]}>Feedrate (mm/min)</Text>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          {FEEDRATES.map((f) => (
            <Button
              key={f}
              label={`${f}`}
              variant={feedrate === f ? "primary" : "secondary"}
              onPress={() => setFeedrate(f)}
            />
          ))}
        </View>

        <Text style={[type.small, { color: c.muted }]}>Distance presets</Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          {EXTRUDE_DISTANCES.map((d) => (
            <Button
              key={`ext-${d}`}
              label={`+${d} mm`}
              variant="secondary"
              onPress={() =>
                confirmRed(
                  "Extrude filament?",
                  `Extrude ${d} mm at ${feedrate} mm/min. The nozzle must be hot (≥170 °C) and the printer idle or paused.`,
                  `Extrude ${d} mm`,
                  () => extrude(selectedId, d, feedrate),
                  `Extruded ${d} mm`,
                )
              }
            />
          ))}
          {EXTRUDE_DISTANCES.map((d) => (
            <Button
              key={`ret-${d}`}
              label={`−${d} mm`}
              variant="secondary"
              onPress={() =>
                confirmRed(
                  "Retract filament?",
                  `Retract ${d} mm at ${feedrate} mm/min.`,
                  `Retract ${d} mm`,
                  () => extrude(selectedId, -d, feedrate),
                  `Retracted ${d} mm`,
                )
              }
            />
          ))}
        </View>
      </Surface>
      </CapabilityCard>

      {/* 9. Steppers off (RED) ---------------------------------------------- */}
      <CapabilityCard title="Disable steppers" availability={capabilities.get("motion")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.danger }]}>Disable steppers</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Sends M84. All motors are de-energised; the toolhead and bed can
          move freely. Positional tracking is reset to UNKNOWN — you must
          home before jogging again.
        </Text>
        <Button
          label="Steppers off"
          variant="danger"
          onPress={() =>
            confirmRed(
              "Disable steppers?",
              "All motors will de-energise. The toolhead and bed will move freely until you home again.",
              "Disable",
              () => steppersOff(selectedId),
              "Steppers disabled",
            )
          }
        />
      </Surface>
      </CapabilityCard>

      {/* 10. Raw G-code console (BLACK / gated) ----------------------------- */}
      <CapabilityCard title="Raw G-code console" availability={capabilities.get("core")}>
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Raw G-code console</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Sends G-code straight to the printer with no safety checks. This is
          off by default: set BRIDGE_ENABLE_RAW_GCODE on the bridge server to
          turn it on. If it&apos;s off, the bridge replies with a message
          telling you how to enable it.
        </Text>

        <TextInput
          value={rawGcode}
          onChangeText={(v) => { setRawGcode(v); setRawGcodeError(null); }}
          multiline
          numberOfLines={4}
          style={{
            backgroundColor: c.surface2,
            color: c.text,
            borderColor: rawGcodeError ? c.danger : c.border,
            borderWidth: 1,
            borderRadius: 0,
            padding: 12,
            minHeight: 100,
            fontFamily: "IBMPlexMono_500Medium",
            fontSize: 14,
            textAlignVertical: "top",
          }}
          placeholderTextColor={c.muted}
          placeholder="G28\nM104 S220"
          autoCapitalize="characters"
          autoCorrect={false}
        />

        {rawGcodeError && (
          <Text style={[type.small, { color: c.danger }]}>{rawGcodeError}</Text>
        )}

        <Button
          label="Send"
          variant="danger"
          disabled={!rawGcode.trim()}
          onPress={() => {
            setRawGcodeError(null);
            Alert.alert(
              "Send raw G-code?",
              "This command bypasses all safety checks and is forwarded directly to the printer.",
              [
                { text: "Cancel", style: "cancel" },
                {
                  text: "Send",
                  style: "destructive",
                  onPress: () => {
                    sendRawGcode(selectedId, rawGcode)
                      .then(() => showToast("G-code sent", { severity: "success" }))
                      .catch((e) => {
                        if (e instanceof BridgeError) {
                          // Surface verbatim — the 403 message names BRIDGE_ENABLE_RAW_GCODE.
                          const hint = e.envelope.remediation_hint
                            ? `\n${e.envelope.remediation_hint}`
                            : "";
                          setRawGcodeError(`${e.envelope.message}${hint}`);
                        } else {
                          setRawGcodeError(String(e));
                        }
                      });
                  },
                },
              ],
            );
          }}
        />

        {/* When the server returns 403 raw_gcode_disabled, rawGcodeError above
            renders the bridge's verbatim message with the BRIDGE_ENABLE_RAW_GCODE
            instructions. The error persists until the user edits the input. */}
      </Surface>
      </CapabilityCard>
      </CapabilityList>
    </ScrollView>
  );
}
