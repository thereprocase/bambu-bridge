/** Maintenance command targets are never physical-position telemetry. */
import { useEffect, useRef, useState } from "react";
import { Alert, AppState, Pressable, Text, View, type ViewStyle } from "react-native";
import { useIsFocused } from "@react-navigation/native";
import { moveBedAbsolute, moveXYAbsolute, maintenanceZStep, maintenanceXYStep, qualifyHeldJog, startHeldJog, renewHeldJog, stopHeldJog, type MaintenanceResponse, type MaintenanceState } from "../api/control";
import { BridgeError, BridgeNetworkError } from "../api/errors";
import { useLiveStore } from "../store/live";
import { useTheme } from "../theme/ThemeProvider";
import { useToastStore } from "./Toast";
import { createHoldLease } from "../lib/holdLease";
import { Z_PRESETS, XY_PRESETS, XY_DIRECTIONS, maintenanceReady, latestMaintenance, maintenanceStepAllowed } from "../lib/maintenance";

type Step = 1 | 10 | 50;
type HeldDirection = {axes:"Z"|"XY";direction_x?:-1|0|1;direction_y?:-1|0|1;direction_z?:-1|1};
export function MaintenanceControls({ printerId }: { printerId: string }) {
  const { c, type, radius } = useTheme();
  const snapshot = useLiveStore(s => s.printers[printerId]?.snapshot);
  const [response, setResponse] = useState<MaintenanceState | null>(null);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const lease = useRef<ReturnType<typeof createHoldLease> | null>(null);
  const longPresses = useRef<Record<string, boolean>>({});
  const [zStep, setZStep] = useState<Step>(10);
  const [xyStep, setXYStep] = useState<Step>(10);
  const [active, setActive] = useState(AppState.currentState === "active");
  const [now, setNow] = useState(Date.now());
  const focused = useIsFocused();
  const showToast = useToastStore(s => s.show);
  const state = latestMaintenance(snapshot?.maintenance as MaintenanceState | undefined, response);
  const supported = !!snapshot?.maintenance;
  const readyZ = supported && !pending && active && focused && maintenanceReady(snapshot, "Z", now);
  const readyXY = supported && !pending && active && focused && maintenanceReady(snapshot, "XY", now);
  const generation = useRef(0);
  const current = useRef({active,focused,readyZ,readyXY,state});
  current.current = {active,focused,readyZ,readyXY,state};
  useEffect(() => {
    void lease.current?.stop();
    generation.current += 1; setResponse(null); setPending(false); inFlight.current = false;
  }, [printerId]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    const listener = AppState.addEventListener("change", value => {
      current.current.active = value === "active";
      if (value !== "active") void lease.current?.stop();
      setActive(value === "active");
    });
    return () => { current.current.active=false; clearInterval(timer); listener.remove(); generation.current += 1; void lease.current?.stop(); };
  }, []);
  useEffect(() => { if (!active || !focused) void lease.current?.stop(); }, [active, focused]);
  async function send(fn: () => Promise<MaintenanceResponse>, message = "Movement command sent") {
    if (inFlight.current || lease.current?.active || !active || !focused) return;
    inFlight.current = true; setPending(true);
    const currentGeneration = generation.current;
    try {
      const result = await fn();
      if (currentGeneration === generation.current) {
        setResponse(result.maintenance); showToast(message, { severity: "success" });
      }
    } catch (error) {
      if (currentGeneration !== generation.current) return;
      if (error instanceof BridgeError) showToast(error.envelope.message, { severity: "danger" });
      else if (error instanceof BridgeNetworkError) showToast(error.message, { severity: "danger", ttlMs: 10000 });
      else showToast(String(error), { severity: "danger" });
    } finally {
      if (currentGeneration === generation.current) { inFlight.current = false; setPending(false); }
    }
  }
  function beginHold(direction: HeldDirection) {
    if (lease.current?.active || inFlight.current || !current.current.active || !current.current.focused
      || !(direction.axes === "Z" ? current.current.readyZ : current.current.readyXY)) return;
    if (!current.current.state?.hold?.qualified) { showToast("Enable held jogging first. Click steps remain available.", {severity:"danger"}); return; }
    lease.current = createHoldLease({
      start: () => startHeldJog(printerId, direction),
      renew: token => renewHeldJog(printerId, token),
      stop: token => stopHeldJog(printerId, token),
    }, error => showToast(error instanceof BridgeError ? error.envelope.message : String(error), {severity:"danger"}));
    void lease.current.start();
  }
  function button(label: string, name: string, disabled: boolean, onPress: () => void, style?: ViewStyle, hold?: HeldDirection) {
    return <Pressable key={name} accessibilityRole="button" accessibilityLabel={name}
      accessibilityState={{ disabled }} disabled={disabled}
      onPressIn={() => { longPresses.current[name] = false; }}
      onLongPress={hold ? () => { longPresses.current[name] = true; beginHold(hold); } : undefined}
      delayLongPress={300}
      pressRetentionOffset={{top:0,bottom:0,left:0,right:0}}
      onPressOut={hold ? () => { void lease.current?.stop(); } : undefined}
      onTouchCancel={hold ? () => { void lease.current?.stop(); } : undefined}
      onPress={() => { if (!longPresses.current[name]) onPress(); }} unstable_pressDelay={100}
      style={({ pressed }) => [{ minHeight: 44, minWidth: 0, padding: 4, borderWidth: 1,
        borderColor: c.border, borderRadius: radius.md, backgroundColor: c.surface2,
        alignItems: "center", justifyContent: "center", opacity: disabled ? 0.4 : pressed ? 0.75 : 1 }, style]}>
      <Text style={[type.small, { color: c.text, textAlign: "center" }]}>{label}</Text>
    </Pressable>;
  }
  function selector(label: string, value: Step, set: (step: Step) => void) {
    return button(`${value} mm ▾`, label, pending, () => Alert.alert(label, undefined,
      [1, 10, 50].map(n => ({ text: `${n} mm`, onPress: () => set(n as Step) }))), { width: "100%" });
  }
  const requested = state?.last_requested;
  const zAllowed = (direction: number) => maintenanceStepAllowed(state, "Z", direction, zStep);
  const xyAllowed = (dx: number, dy: number) => maintenanceStepAllowed(state, "X", dx, xyStep) && maintenanceStepAllowed(state, "Y", dy, xyStep);
  return <View style={{ gap: 8 }}>
    <Text style={[type.h2, { color: c.text }]}>Maintenance Z positions</Text>
    <Text style={[type.small, { color: c.muted }]}>Top raises the bed; Bottom lowers it. Keep hands and tools clear while moving.</Text>
    {!supported && <Text style={[type.small, { color: c.warn }]}>Update the bridge to enable maintenance positioning.</Text>}
    <View style={{ flexDirection: "row", gap: 12 }}>
      <View style={{ flex: 1, gap: 8 }}>{[0, 1].map(row => <View key={row} style={{ flexDirection: "row", gap: 8 }}>
        {Z_PRESETS.slice(row * 2, row * 2 + 2).map(([label, target]) => button(label,
          `${label}: move bed to Z${target} mm`, !readyZ, () => send(() => moveBedAbsolute(printerId, target)), { flex: 1, minHeight: 64 }))}
      </View>)}</View>
      <View style={{ width: 96, gap: 6 }}>
        <Text style={[type.small, { color: c.muted }]}>Step</Text>{selector("Maintenance Z step", zStep, setZStep)}
        {button("↑ Raise", "Raise bed by selected Z step", !readyZ || !zAllowed(-1), () => send(() => maintenanceZStep(printerId, -1, zStep)), undefined, {axes:"Z",direction_z:-1})}
        {button("↓ Lower", "Lower bed by selected Z step", !readyZ || !zAllowed(1), () => send(() => maintenanceZStep(printerId, 1, zStep)), undefined, {axes:"Z",direction_z:1})}
      </View>
    </View>
    <Text accessibilityLiveRegion="polite" style={[type.small, { color: c.muted }]}>{typeof requested?.Z === "number"
      ? `Last Z command: ${requested.Z} mm. Physical position is unverified.` : "Select an absolute Z preset before using Raise or Lower."}</Text>
    <Text style={[type.h2, { color: c.text }]}>Maintenance XY positions</Text>
    <Text style={[type.small, { color: c.muted }]}>Back (+Y) ↑ · Front (−Y) ↓ · Left (−X) · Right (+X)</Text>
    <View accessibilityLabel="Absolute XY maintenance targets" style={{ gap: 6 }}>
      {[0, 1, 2, 3, 4].map(row => <View key={row} style={{ flexDirection: "row", gap: 6 }}>
        {[0, 1, 2, 3, 4].map(col => {
          const point = XY_PRESETS.find(p => p.row === row && p.col === col);
          return <View key={col} style={{ flex: 1, minWidth: 0, minHeight: 44 }}>{point && button(point.label,
            `${point.name}: X${point.x} Y${point.y} mm`, !readyXY, () => send(() => moveXYAbsolute(printerId, point.x, point.y)), { flex: 1, width: "100%" })}</View>;
        })}
      </View>)}
    </View>
    <View style={{ flexDirection: "row", gap: 12 }}>
      <View accessibilityLabel="XY maintenance direction pad" style={{ flex: 1, gap: 6 }}>
        {[0, 1, 2].map(row => <View key={row} style={{ flexDirection: "row", gap: 6 }}>
          {[0, 1, 2].map(col => {
            const direction = XY_DIRECTIONS.find(d => d[4] === row && d[5] === col);
            return <View key={col} style={{ flex: 1, minWidth: 0, minHeight: 44 }}>{direction && button(direction[0],
              `Step ${direction[1]} by selected XY step`, !readyXY || !xyAllowed(direction[2], direction[3]),
              () => send(() => maintenanceXYStep(printerId, direction[2], direction[3], xyStep)), { flex: 1, width: "100%" }, {axes:"XY",direction_x:direction[2],direction_y:direction[3]})}</View>;
          })}
        </View>)}
      </View>
      <View style={{ width: 96, gap: 6 }}><Text style={[type.small, { color: c.muted }]}>Step</Text>{selector("Maintenance XY step", xyStep, setXYStep)}</View>
    </View>
    <Text accessibilityLiveRegion="polite" style={[type.small, { color: c.muted }]}>{typeof requested?.X === "number" && typeof requested?.Y === "number"
      ? `Last XY command: X${requested.X}, Y${requested.Y} mm. Physical position is unverified.` : "Select an absolute XY target before using the direction pad."}</Text>
    {button(state?.hold?.qualified ? "Verify held jogging again" : "Enable held jogging", "Verify held jogging without axis movement", !readyZ || !readyXY,
      () => send(async () => { const result=await qualifyHeldJog(printerId); return {sent:null,confirmation:"pending",maintenance:result.maintenance}; }, "Held-jog verification finished"))}
    <Text accessibilityLiveRegion="polite" style={[type.small,{color:c.muted}]}>{state?.hold?.reason ?? "Held jogging needs idle firmware verification. Click steps remain available."}</Text>
    <Text style={[type.small,{color:c.muted}]}>Hold a direction to jog; release to stop future segments. Held segments are small and independent of the click-step selector.</Text>
    <Text style={[type.small, { color: c.muted }]}>Requires an idle printer, fresh status and homed axes. Select an absolute preset again after using other printer controls.</Text>
  </View>;
}
