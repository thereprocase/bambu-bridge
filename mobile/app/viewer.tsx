/**
 * 3D viewer (full-screen) — hosts the bridge's self-contained WebGL viewer in a
 * native WebView so the operator never leaves the app to see the printing part
 * in 3D (live layer progress, scrubbing, color modes).
 *
 * Opened from the Status screen:
 *   router.push({ pathname: "/viewer", params: { printer: <serial> } })
 *
 * The viewer URL (from `buildViewerUrl`) carries the bearer in its query — the
 * documented auth path for this route, because an embedded view can't set
 * Authorization headers. SECURITY: that URL is fed straight to the WebView and
 * is NEVER rendered, logged, or surfaced in any error copy.
 *
 * Registered full-screen (no `presentation: "modal"`) with the stack header, so
 * the back affordance matches the rest of the app (settings/add-printer rely on
 * the same stack header). Dark background #111113 matches the viewer canvas so
 * there's no light flash before the WebGL scene paints.
 */

import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, BackHandler, Text, View } from "react-native";
import WebView from "react-native-webview";

import { notifyRequestFailed } from "../src/api/endpoint";
import { buildViewerUrl, isViewerNavigationAllowed, safeVizTimings } from "../src/api/viewer";
import { useBridgeStore } from "../src/store/bridge";
import { Button } from "../src/components/Button";
import { qaLog } from "../src/lib/qalog";
import { useTheme } from "../src/theme/ThemeProvider";

// Matches the viewer canvas so there's no white flash before WebGL paints.
const VIEWER_BG = "#111113";

function firstParam(v: string | string[] | undefined): string | undefined {
  if (v == null) return undefined;
  const s = Array.isArray(v) ? v[0] : v;
  return s && s.length ? s : undefined;
}

export default function ViewerScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const params = useLocalSearchParams();
  const printerId = firstParam(params.printer);

  const networkFailed = useRef(false);
  const baseUrl = useBridgeStore((s) => s.baseUrl);
  const baseUrlLan = useBridgeStore((s) => s.baseUrlLan);
  const bearer = useBridgeStore((s) => s.bearer);

  // URL is resolved async (the base URL comes from the LAN/Tailscale resolver),
  // so we hold three states: resolving → ready(uri) → unconfigured(null).
  const [uri, setUri] = useState<string | null>(null);
  const [resolving, setResolving] = useState(true);
  // WebView load failure (network or HTTP). Holds a friendly line; the raw URL
  // is never put here.
  const [loadError, setLoadError] = useState<string | null>(null);
  // Reload nonce — bumping it remounts the WebView for a clean retry even if
  // the ref reload path is unavailable mid-error.
  const [reloadKey, setReloadKey] = useState(0);
  // Viewer page may drive loading/ready/error via viz.state postMessage.
  // null = no viz.state received yet (fall back to onLoad / onError behavior).
  const [vizState, setVizState] = useState<"loading" | "ready" | "error" | null>(null);

  // Android hardware back: when viewer is focused, intercept the hardware back
  // button and pop the stack via router.back() rather than letting the default
  // framework path run. Without this, the back event escapes the stack on some
  // Android versions (observed on Pixel 9 Pro) and moves the app to the
  // background instead of returning to the Status tab.
  //
  // useFocusEffect-scoped so the handler is only active while this screen owns
  // focus; the cleanup removes it the moment viewer loses focus (navigating
  // away, push on top), leaving the Status tab's normal app-exit behavior
  // completely untouched.
  useFocusEffect(
    useCallback(() => {
      const sub = BackHandler.addEventListener("hardwareBackPress", () => {
        router.back();
        return true;
      });
      return () => sub.remove();
    }, [router]),
  );

  useEffect(() => {
    let cancelled = false;
    if (!printerId) {
      setResolving(false);
      // No printer param — treat as a configuration error state.
      qaLog("viewer.state", { state: "error" });
      return;
    }
    setResolving(true);
    setUri(null);
    setLoadError(null);
    setVizState(null);
    networkFailed.current = false;
    qaLog("viewer.state", { state: "loading" });
    buildViewerUrl(printerId)
      .then((u) => {
        if (!cancelled) {
          setUri(u);
          setResolving(false);
          // URL resolved: viewer transitions to ready (WebView will load).
          // SECURITY: URL carries the bearer token — never logged here.
          qaLog("viewer.state", { state: "ready" });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setUri(null);
          setResolving(false);
          qaLog("viewer.state", { state: "error" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [printerId, reloadKey, baseUrl, baseUrlLan, bearer]);

  function retry() {
    setLoadError(null);
    setVizState(null);
    if (networkFailed.current && uri) notifyRequestFailed(uri);
    setResolving(true);
    // Resolve a fresh endpoint and key; retrying the old URI defeats fallback.
    setReloadKey((k) => k + 1);
  }

  /**
   * Handle postMessage from the viewer page.
   * Accepted types (whitelist — all others are silently ignored):
   *   {type:"viz.timings", ...}  — performance telemetry; forwarded to qaLog
   *   {type:"viz.state", state}  — drives loading/ready/error UI
   *
   * SECURITY: never log URLs, tokens, or free-form strings from the page.
   * JSON.parse is wrapped in try/catch; unknown types are dropped silently.
   */
  function handleWebViewMessage(event: { nativeEvent: { data: string } }) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(event.nativeEvent.data);
    } catch {
      return; // ignore non-JSON
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
    const msg = parsed as Record<string, unknown>;
    const msgType = msg.type;
    if (msgType === "viz.timings") {
      qaLog("viz.timings", safeVizTimings(msg));
    } else if (msgType === "viz.state") {
      const state = msg.state;
      if (state === "loading" || state === "ready" || state === "error") {
        setVizState(state);
        qaLog("viz.state.web", { state });
        // If the page signals error, surface it like an onError.
        if (state === "error") {
          setLoadError("The 3D viewer reported an error. Retry to reload.");
        }
      }
      // Unknown state values are ignored.
    }
    // All other type values are silently ignored.
  }

  const header = (
    <Stack.Screen
      options={{
        title: "3D · Live",
        headerStyle: { backgroundColor: VIEWER_BG },
        headerTintColor: c.text,
      }}
    />
  );

  // Bad/lost param, or no base/bearer configured → explain, don't show a blank
  // WebView. Never claim "unknown".
  if (!printerId) {
    return (
      <View style={{ flex: 1, backgroundColor: VIEWER_BG, padding: space.lg, gap: space.md }}>
        {header}
        <Text style={[type.h1, { color: c.text }]}>3D view</Text>
        <Text style={[type.body, { color: c.muted }]}>
          Couldn&apos;t tell which printer to show. Go back and tap View in 3D again.
        </Text>
        <Button label="Back" variant="secondary" onPress={() => router.back()} fullWidth />
      </View>
    );
  }

  if (resolving) {
    return (
      <View style={{ flex: 1, backgroundColor: VIEWER_BG, alignItems: "center", justifyContent: "center" }}>
        {header}
        <ActivityIndicator color={c.accent} />
      </View>
    );
  }

  if (!uri) {
    return (
      <View style={{ flex: 1, backgroundColor: VIEWER_BG, padding: space.lg, gap: space.md }}>
        {header}
        <Text style={[type.h1, { color: c.text }]}>3D view</Text>
        <Text style={[type.body, { color: c.muted }]}>
          Set up the bridge connection in Settings first, then try again.
        </Text>
        <Button label="Back" variant="secondary" onPress={() => router.back()} fullWidth />
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: VIEWER_BG }}>
      {header}
      <WebView
        key={reloadKey}
        source={{ uri }}
        style={{ flex: 1, backgroundColor: VIEWER_BG }}
        // Only ever load http(s); a viewer page that tried to redirect to a
        // custom scheme (mailto:, intent:) is not something we host.
        originWhitelist={["*"]}
        onShouldStartLoadWithRequest={(req) => isViewerNavigationAllowed(uri, req.url)}
        setSupportMultipleWindows={false}
        javaScriptEnabled
        domStorageEnabled
        // The viewer may animate/play media inline; don't force fullscreen.
        allowsInlineMediaPlayback
        mediaPlaybackRequiresUserAction={false}
        // 3D canvas owns its own gestures — kill the Android pinch-zoom chrome
        // and the rubber-band overscroll so they don't fight the WebGL camera.
        setBuiltInZoomControls={false}
        setDisplayZoomControls={false}
        overScrollMode="never"
        onError={() => {
          networkFailed.current = true;
          setLoadError("Couldn't load the 3D viewer. Check the printer connection and retry.");
          qaLog("viewer.state", { state: "error" });
        }}
        onHttpError={() => {
          setLoadError("The bridge couldn't serve the 3D viewer. Check the API key in Settings, then retry.");
          qaLog("viewer.state", { state: "error" });
        }}
        // Clear a stale error once a fresh load starts so Retry feels live.
        onLoadStart={() => {
          if (loadError) setLoadError(null);
          setVizState(null);
        }}
        // When viz.state messages are absent, fall back to the native onLoad
        // signal as "ready" so the loading indicator dismisses correctly.
        onLoad={() => {
          if (!vizState) setVizState("ready");
        }}
        // postMessage handler — viz.timings / viz.state (§ task 4).
        onMessage={handleWebViewMessage}
        renderLoading={() => (
          <View style={{ flex: 1, backgroundColor: VIEWER_BG, alignItems: "center", justifyContent: "center" }}>
            <ActivityIndicator color={c.accent} />
          </View>
        )}
        startInLoadingState
      />

      {/* Viz-state loading overlay: shown while the page has signaled "loading"
          (viz.state postMessage) but not yet "ready". Dismissed as soon as the
          page signals ready OR the native onLoad fires. Falls back gracefully
          when the viewer page doesn't send viz.state at all. */}
      {vizState === "loading" && !loadError && (
        <View
          style={{
            position: "absolute",
            top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: VIEWER_BG,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <ActivityIndicator color={c.accent} />
        </View>
      )}

      {/* Friendly error overlay with Retry — covers the WebView so the operator
          isn't staring at a blank/half-rendered canvas. */}
      {loadError && (
        <View
          style={{
            position: "absolute",
            top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: VIEWER_BG,
            alignItems: "center",
            justifyContent: "center",
            padding: space.lg,
            gap: space.md,
          }}
        >
          <Text style={[type.h2, { color: c.text, textAlign: "center" }]}>3D viewer</Text>
          <Text style={[type.body, { color: c.muted, textAlign: "center" }]}>
            {loadError}
          </Text>
          <View style={{ flexDirection: "row", gap: space.sm }}>
            <Button label="Retry" onPress={retry} />
            <Button label="Back" variant="secondary" onPress={() => router.back()} />
          </View>
        </View>
      )}
    </View>
  );
}
