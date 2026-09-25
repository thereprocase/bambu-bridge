import { useIsFocused } from "@react-navigation/native";
import { useEffect, useRef, useState } from "react";
import { AppState, NativeSyntheticEvent, requireNativeComponent, Text, View, ViewProps } from "react-native";
import { notifyRequestFailed, notifyRequestSucceeded, sameOrigin } from "../api/endpoint";
import { useBridgeStore } from "../store/bridge";
import { useNetStore } from "../store/net";
import { useViewingStore } from "./state";
import { viewingConfig } from "./native";
import { CAMERA_HUD_INTERVAL_MS, shouldPublishCameraHud } from "./hud";

interface CameraEvent { state: string; fps: number; width: number; height: number; transport?: string }
const NativeCamera = requireNativeComponent<ViewProps & {
  source: string | null; zoomEnabled: boolean;
  onState(event: NativeSyntheticEvent<CameraEvent>): void;
}>("BridgeCameraView");

export function Camera({ printer, fullscreen = false }: { printer: string; fullscreen?: boolean }) {
  const focused = useIsFocused();
  const [foreground, setForeground] = useState(AppState.currentState === "active");
  const revision = useViewingStore(s => s.networkRevision);
  const bearer = useBridgeStore(s => s.bearer);
  const [attempt, setAttempt] = useState(0);
  const [source, setSource] = useState<string | null>(null);
  const [state, setState] = useState("connecting");
  const [fps, setFps] = useState(0);
  const [quality, setQuality] = useState("");
  const [now, setNow] = useState(Date.now());
  const base = useRef("");
  const failures = useRef(0);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hud = useRef({ state: "connecting", fps: 0, lastFrame: null as number | null, publishedAt: 0 });
  const routeHealthy = useRef(false);
  const routeNotifiedAt = useRef(0);
  const active = focused && foreground;
  useEffect(() => {
    const subscription = AppState.addEventListener("change", value => setForeground(value === "active"));
    return () => subscription.remove();
  }, []);
  useEffect(() => {
    hud.current = { state: "connecting", fps: 0, lastFrame: null, publishedAt: 0 };
    routeHealthy.current = false;
    routeNotifiedAt.current = 0;
    setFps(0);
    failures.current = 0;
  }, [printer]);
  useEffect(() => {
    let cancelled = false;
    hud.current = { ...hud.current, state: "connecting" };
    routeHealthy.current = false;
    routeNotifiedAt.current = 0;
    setSource(null);
    if (!active) return;
    setState("connecting");
    void viewingConfig(printer).then(config => {
      if (!cancelled) { base.current = config.base; setSource(JSON.stringify(config)); }
    }).catch(() => {
      if (!cancelled) { hud.current = { ...hud.current, state: "auth" }; setState("auth"); }
    });
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => { cancelled = true; clearInterval(clock); if (retry.current) clearTimeout(retry.current); retry.current = null; };
  }, [printer, active, revision, bearer, attempt]);

  function onState({ nativeEvent: event }: NativeSyntheticEvent<CameraEvent>) {
    if (!active) return;
    const at = Date.now();
    const previous = hud.current;
    if (event.state === "frame") {
      hud.current = { ...previous, state: event.state, fps: event.fps, lastFrame: at };
      failures.current = 0;
      if (!routeHealthy.current || at - routeNotifiedAt.current >= CAMERA_HUD_INTERVAL_MS) {
        routeHealthy.current = true;
        routeNotifiedAt.current = at;
        notifyRequestSucceeded(base.current);
        const lan = useBridgeStore.getState().baseUrlLan;
        useNetStore.getState().setReach(lan && sameOrigin(base.current, lan) ? "lan" : "remote");
      }
    } else if (["network", "unavailable"].includes(event.state) && !retry.current) {
      hud.current = { ...previous, state: event.state };
      routeHealthy.current = false;
      if (event.state === "network") notifyRequestFailed(base.current);
      const delay = Math.min(15_000, 1000 * 2 ** Math.min(failures.current++, 4));
      retry.current = setTimeout(() => { retry.current = null; setAttempt(v => v + 1); }, delay);
    } else {
      hud.current = { ...previous, state: event.state };
    }
    if (shouldPublishCameraHud({ previousState: previous.state, nextState: event.state,
      publishedAt: previous.publishedAt, now: at, hadFrame: previous.lastFrame !== null })) {
      hud.current.publishedAt = at;
      setState(event.state);
      setFps(hud.current.fps);
      if (event.state === "frame") setQuality(event.transport === "hls" ? `Auto · ${event.height}p` : "JPEG fallback");
      setNow(at);
    }
  }
  const age = hud.current.lastFrame === null ? null : Math.max(0, Math.floor((now - hud.current.lastFrame)/1000));
  const stale = age !== null && age >= 5;
  const label = state === "identity" ? "Bridge identity could not be verified. Check pairing in Settings."
    : state === "auth" ? "Camera access rejected. Check your pairing or API key."
    : !active ? "Camera paused"
    : stale ? `Last frame ${age}s ago · reconnecting`
    : state === "frame" ? `Live · ${quality} · ${fps > 0 ? fps.toFixed(1) + " FPS" : "receiving frames"}`
    : state === "connecting" ? "Connecting camera…" : "Reconnecting camera…";
  return <View style={{ flex: fullscreen ? 1 : undefined, aspectRatio: fullscreen ? undefined : 4/3,
    backgroundColor: "#111113", overflow: "hidden", minHeight: 120 }}>
    <NativeCamera key={printer} source={source} zoomEnabled={fullscreen} onState={onState}
      style={{ flex: 1, opacity: stale ? 0.5 : 1 }} />
    <View pointerEvents="none" style={{ position: "absolute", left: 10, right: 10, top: 10 }}>
      <Text style={{ color: "#fff", backgroundColor: "rgba(0,0,0,0.75)", alignSelf: "flex-start", padding: 8,
        fontSize: 13, borderRadius: 4 }}>{label}</Text>
    </View>
  </View>;
}
