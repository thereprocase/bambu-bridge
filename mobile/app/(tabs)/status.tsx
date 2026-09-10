/**
 * Status — live dashboard. The single screen most users open.
 *
 * Reads from `useLiveStore[selectedId]` (no fetch on focus — WS keeps it
 * fresh). Falls back to the MMKV-cached snapshot on cold launch so the
 * UI doesn't flash.
 *
 * Top: headline + progress (or "Connect a printer" if none registered).
 * Middle: 1fps camera snapshot poll.
 * Bottom: temp + fan readout + quick actions (pause/resume/stop/light).
 */

import { Ionicons } from "@expo/vector-icons";
import { Image as ExpoImage } from "expo-image";
import { useFocusEffect, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  AppState,
  Linking,
  Pressable,
  RefreshControl,
  ScrollView,
  Text,
  View,
} from "react-native";

import { downloadSnapshot } from "../../src/api/files";
import { printAction, setLight } from "../../src/api/control";
import { resolveEndpoint } from "../../src/api/endpoint";
import { BridgeError } from "../../src/api/errors";
import { Button } from "../../src/components/Button";
import { ProgressBar } from "../../src/components/ProgressBar";
import { StatusDot } from "../../src/components/StatusDot";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import { isSafeBambuWikiUrl, viewOf } from "../../src/lib/snapshot";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function StatusScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);

  const list = usePrintersStore((s) => s.list);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const refreshPrinters = usePrintersStore((s) => s.refresh);
  const select = usePrintersStore((s) => s.select);
  const printersLoading = usePrintersStore((s) => s.loading);
  const lastFetched = usePrintersStore((s) => s.lastFetched);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));
  const status = live?.status ?? "closed";
  // `snapshot` is the bridge's §6 translated snapshot, stored at its root by
  // the WS layer. `viewOf` reads the root blocks (`phase`, `headline`,
  // `temps`, `job`, `ams.slots`, …) directly — no `.state` wrapper.
  const view = viewOf((live?.snapshot as any) ?? null);

  // Fail-safe: if the WS is not open we cannot reliably know the actual phase,
  // so all print-control buttons must be disabled (§G2 requirement). This also
  // handles the cold-launch MMKV-cached state where `status` is "connecting".
  const wsConnected = status === "open";

  // The 3D viewer is only meaningful while there's a job loaded on the bed —
  // printing/paused/preparing. Same phase gate idiom as the Quick actions, and
  // (like them) also gated on a live WS so a stale cached phase can't enable it.
  const jobActive =
    wsConnected &&
    (view.phase === "printing" ||
      view.phase === "paused" ||
      view.phase === "preparing");

  // Camera: TRUE double-buffer. Two absolutely-positioned <ExpoImage>s (A and
  // B) fill the same box. The one that is FRONT is fully opaque and visible;
  // its source is NEVER mutated while it's visible. When new frame bytes
  // arrive we load them into the BACK image's source and wait for its `onLoad`
  // (decode complete) before flipping which buffer is front. Because the swap
  // is decode-gated — not timing-gated — the displayed frame is held intact
  // until the next one is fully ready, so there is no placeholder/strobe gap.
  //
  // `snapErr`/`snapFails` are informational — a failed poll does NOT clear
  // either buffer; we keep the last good frame and count consecutive failures
  // to dim + badge the container once the view is clearly no longer live.
  const [uriA, setUriA] = useState<string | null>(null);
  const [uriB, setUriB] = useState<string | null>(null);
  const [front, setFront] = useState<"a" | "b">("a");
  const [snapErr, setSnapErr] = useState<string | null>(null);
  const [snapFails, setSnapFails] = useState(0);
  const [appActive, setAppActive] = useState(AppState.currentState === "active");
  // Which path the bridge resolver is currently using (LAN vs Tailscale),
  // shown as a one-liner in the header so the operator can tell they're on
  // the fast local path. Resolved on focus; best-effort, never blocks.
  const [activePath, setActivePath] = useState<"lan" | "remote" | null>(null);

  // Issues card collapse state — starts expanded so new warnings are seen.
  const [issuesExpanded, setIssuesExpanded] = useState(true);

  // `front` mirrored into a ref so the async poll + the onLoad handlers read
  // the live value (not the value captured when the effect/handler closed).
  const frontRef = useRef<"a" | "b">("a");
  // The buffer a freshly-fetched frame was just written into and is awaiting
  // decode. Its onLoad performs the flip; we ignore onLoad from the (already
  // visible) front buffer so re-layouts don't spuriously flip.
  const pendingRef = useRef<"a" | "b" | null>(null);

  // Threshold of consecutive failed polls before we flag the (still-shown)
  // frame as stale. ~5s of dead feed at 1fps.
  const STALE_AFTER_FAILS = 5;

  // Decode-complete handler: promote the just-loaded back buffer to front.
  // Only acts if this buffer is the pending one, so an onLoad fired by the
  // already-visible buffer (e.g. remount) can't flip us to a blank slot.
  const onBufferLoad = useCallback((buf: "a" | "b") => {
    if (pendingRef.current !== buf) return;
    pendingRef.current = null;
    frontRef.current = buf;
    setFront(buf);
  }, []);

  // Switching printers must drop BOTH buffers immediately — showing one
  // printer's bed under another printer's telemetry is worse than a momentary
  // blank. (Background/tab-switch deliberately do NOT clear.)
  useEffect(() => {
    setUriA(null);
    setUriB(null);
    setFront("a");
    frontRef.current = "a";
    pendingRef.current = null;
    setSnapErr(null);
    setSnapFails(0);
  }, [selectedId]);

  // Track app foreground/background so we don't burn 1fps camera traffic
  // while the screen is off or the user is in another app.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (s) => {
      setAppActive(s === "active");
    });
    return () => sub.remove();
  }, []);

  // Refresh the registered-printers list on focus — non-blocking, mirroring
  // the config deep-link route (commit ace9d72). WS/camera run off the
  // persisted selectedId regardless, but the *list* (used to render this
  // screen and the picker) is only ever populated by an explicit fetch. On a
  // cold start where the very first fetch happened while the network was down,
  // `list` stays empty forever and the user is stranded on "No printers yet"
  // even though the printer is reachable. Refetch when the list is empty OR
  // the last good fetch is stale; never block on it (fire-and-forget) so a
  // slow/dead network can't freeze the screen.
  const STALE_LIST_MS = 30_000;
  useFocusEffect(
    useCallback(() => {
      const { list: l, lastFetched: lf, loading } = usePrintersStore.getState();
      const stale = Date.now() - lf > STALE_LIST_MS;
      if (!loading && (l.length === 0 || stale)) {
        // Fire-and-forget; the store updates the UI when it resolves.
        void refreshPrinters();
      }
    }, [refreshPrinters]),
  );

  // Resolve which bridge path is live (LAN/Tailscale) when the screen gains
  // focus. Fire-and-forget; a failure just leaves the suffix off.
  useFocusEffect(
    useCallback(() => {
      let cancelled = false;
      resolveEndpoint()
        .then(({ path }) => {
          if (!cancelled) setActivePath(path);
        })
        .catch(() => {
          if (!cancelled) setActivePath(null);
        });
      return () => {
        cancelled = true;
      };
    }, []),
  );

  // 1fps camera poll while this tab is focused AND app is foreground.
  // Bambu's snapshot endpoint responds regardless of print phase (the
  // operator wants the live view even when idle), so no phase gate here.
  useFocusEffect(
    useCallback(() => {
      // No printer selected → nothing to show. (Backgrounding the app or
      // leaving the tab just stops the poll via teardown; we keep the last
      // frame so returning is instant rather than a fresh blank+decode.)
      if (!selectedId) {
        setUriA(null);
        setUriB(null);
        setSnapFails(0);
        pendingRef.current = null;
        return;
      }
      if (!appActive) return;
      let cancelled = false;
      let timer: ReturnType<typeof setTimeout> | null = null;

      const tick = async () => {
        try {
          const bytes = await downloadSnapshot(selectedId);
          if (cancelled) return;
          const b64 = arrayBufferToBase64(bytes);
          const uri = `data:image/jpeg;base64,${b64}`;
          // Load into the BACK buffer (opposite of the live front). We do NOT
          // touch the visible front buffer. The back image's `onLoad` flips
          // the front once it has decoded — so the displayed frame is held
          // until the next is fully ready: no placeholder/strobe gap.
          const back = frontRef.current === "a" ? "b" : "a";
          pendingRef.current = back;
          if (back === "a") setUriA(uri);
          else setUriB(uri);
          setSnapErr(null);
          setSnapFails(0);
        } catch (e) {
          if (cancelled) return;
          // Do NOT clear the frame — keep the last good image up and just
          // record the error + bump the consecutive-failure counter so the
          // UI can flag staleness without the feed churning to black.
          setSnapErr(e instanceof BridgeError ? e.envelope.message : String(e));
          setSnapFails((n) => n + 1);
        } finally {
          if (!cancelled) timer = setTimeout(tick, 1000);
        }
      };
      tick();
      return () => {
        cancelled = true;
        if (timer) clearTimeout(timer);
      };
    }, [selectedId, appActive]),
  );

  // Empty list. This is genuinely "none registered" only if we've actually
  // fetched at least once; before that (or if the first fetch failed on a
  // dead network) we must not claim "No printers yet" — the focus effect
  // above is already retrying. Distinguish the two so a cold-start network
  // blip doesn't strand the user on a dead-end empty state.
  if (!list.length) {
    const everFetched = lastFetched > 0;
    return (
      <ScrollView
        style={{ flex: 1, backgroundColor: c.bg }}
        contentContainerStyle={{ padding: space.lg, gap: space.lg }}
        refreshControl={
          <RefreshControl
            refreshing={printersLoading}
            tintColor={c.accent}
            onRefresh={() => refreshPrinters()}
          />
        }
      >
        <Surface padded style={{ gap: space.md }}>
          {!everFetched ? (
            <>
              <Text style={[type.h1, { color: c.text }]}>Loading printers…</Text>
              <Text style={[type.body, { color: c.muted }]}>
                {printersLoading
                  ? "Fetching your registered printers from the bridge."
                  : "Couldn't reach the bridge yet. Pull down to retry, or check the connection in Settings."}
              </Text>
              {!printersLoading && (
                <Button label="Retry" onPress={() => refreshPrinters()} fullWidth />
              )}
            </>
          ) : (
            <>
              <Text style={[type.h1, { color: c.text }]}>No printers yet</Text>
              <Text style={[type.body, { color: c.muted }]}>
                Add your Bambu P1S to start watching prints in real time. You{"'"}ll
                need its LAN IP and the 8-digit access code on its screen.
              </Text>
              <Button label="Add a printer" onPress={() => router.push("/add-printer")} fullWidth />
            </>
          )}
        </Surface>
      </ScrollView>
    );
  }

  const selected = list.find((p) => p.serial === selectedId) ?? list[0];
  const pathSuffix =
    status === "open" && activePath
      ? activePath === "lan" ? " · via LAN" : " · via Tailscale"
      : "";
  const statusLabel =
    status === "open"
      ? `Connected · ${view.phase}${pathSuffix}`
      : status === "connecting"
        ? "Connecting…"
        : status === "error"
          ? `Error: ${live?.statusDetail ?? "unknown"}`
          : "Disconnected";
  const dot =
    view.phase === "failed" || status === "error" ? "danger"
      : view.phase === "paused" ? "warn"
        : status === "open" ? "ok"
          : "neutral";

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={
        <RefreshControl
          refreshing={printersLoading}
          tintColor={c.accent}
          onRefresh={() => refreshPrinters()}
        />
      }
    >
      {/* Printer picker (rendered as a row of pills when >1 registered) ---- */}
      {list.length > 1 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false}>
          <View style={{ flexDirection: "row", gap: space.sm }}>
            {list.map((p) => (
              <Pressable
                key={p.serial}
                onPress={() => select(p.serial)}
                style={{
                  paddingHorizontal: space.md,
                  paddingVertical: space.sm,
                  backgroundColor: p.serial === selectedId ? c.accent : c.surface2,
                  borderRadius: 999,
                }}
              >
                <Text style={{ color: p.serial === selectedId ? "#0a0b0d" : c.text, fontSize: 13 }}>
                  {p.friendly_name}
                </Text>
              </Pressable>
            ))}
          </View>
        </ScrollView>
      )}

      {/* Headline ----------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
          <StatusDot state={dot} />
          <Text style={[type.caption, { color: c.muted, textTransform: "uppercase", flex: 1 }]}>
            {selected.friendly_name} · {statusLabel}
          </Text>
        </View>
        <Text style={[type.h1, { color: c.text }]}>{view.statusLine}</Text>
        {view.subtitle && (
          <Text style={[type.body, { color: c.muted }]}>{view.subtitle}</Text>
        )}
        {view.progress != null && (
          <View style={{ gap: space.sm }}>
            <ProgressBar value={view.progress} />
            <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
              <Text style={[type.caption, { color: c.muted }]}>
                {view.progress.toFixed(1)}%
              </Text>
              {view.layer != null && view.totalLayers != null && (
                <Text style={[type.caption, { color: c.muted }]}>
                  layer {view.layer}/{view.totalLayers}
                </Text>
              )}
              {view.timeLeftMin != null && (
                <Text style={[type.caption, { color: c.muted }]}>
                  {formatMin(view.timeLeftMin)} left
                </Text>
              )}
              {view.etaTimeStr != null && (
                <Text style={[type.caption, { color: c.muted }]}>
                  done ~{view.etaTimeStr}
                </Text>
              )}
            </View>
          </View>
        )}
      </Surface>

      {/* Issues card — HMS warnings + print_error + job_anomaly, shown only
          when at least one of these is present. job_anomaly is always warn
          severity; it has no wiki link. */}
      {(view.hms.length > 0 || view.printErrorFull || view.jobAnomaly) && (
        <Surface padded style={{ gap: space.sm }}>
          <Pressable
            onPress={() => setIssuesExpanded((x) => !x)}
            style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}
          >
            <Ionicons
              name="warning-outline"
              size={18}
              color={
                view.hms.some((e) => e.severity === "error" && !e.stale) || view.printErrorFull
                  ? c.danger
                  : c.warn
              }
            />
            <Text style={[type.h2, { color: c.text, flex: 1 }]}>
              Issues ({view.hms.length + (view.printErrorFull ? 1 : 0) + (view.jobAnomaly ? 1 : 0)})
            </Text>
            <Ionicons
              name={issuesExpanded ? "chevron-up" : "chevron-down"}
              size={16}
              color={c.muted}
            />
          </Pressable>
          {issuesExpanded && (
            <View style={{ gap: space.md }}>
              {view.jobAnomaly && (
                <IssueEntry
                  key="job_anomaly"
                  severity="warn"
                  text={view.jobAnomaly.text}
                  remediation={null}
                  wikiUrl={null}
                />
              )}
              {view.hms.map((entry, i) => (
                <IssueEntry
                  key={`hms-${i}`}
                  severity={entry.severity}
                  text={entry.text}
                  remediation={entry.remediation}
                  wikiUrl={entry.wikiUrl}
                  contextNote={entry.contextNote}
                  stale={entry.stale}
                />
              ))}
              {view.printErrorFull && (
                <IssueEntry
                  key="print_error"
                  severity={view.printErrorFull.severity ?? "error"}
                  text={view.printErrorFull.text}
                  remediation={view.printErrorFull.remediation ?? null}
                  wikiUrl={
                    isSafeBambuWikiUrl(view.printErrorFull.wiki_url)
                      ? view.printErrorFull.wiki_url
                      : null
                  }
                />
              )}
            </View>
          )}
        </Surface>
      )}

      {/* Camera snapshot ----------------------------------------------------
          TRUE double-buffer: two absolutely-positioned <ExpoImage>s fill the
          same box. Exactly one is FRONT (opaque); its source is never mutated
          while visible. New bytes load into the BACK image and only when its
          onLoad fires (decode complete) do we flip front/back — so the feed
          holds the last good frame until the next is fully ready: no strobe.
          A failed poll keeps both buffers; after N misses we dim the whole
          container and show a "Feed stale" badge instead of blanking. */}
      {uriA || uriB ? (
        <Surface style={{ padding: 0, overflow: "hidden" }}>
          <View
            style={{
              width: "100%",
              aspectRatio: 4 / 3,
              // Stale dim applied to the CONTAINER so both buffers fade
              // together — the operator can tell the view may be behind
              // without losing the last known toolhead/bed position.
              opacity: snapFails >= STALE_AFTER_FAILS ? 0.45 : 1,
            }}
          >
            {uriA && (
              <ExpoImage
                source={{ uri: uriA }}
                style={{
                  position: "absolute",
                  width: "100%",
                  height: "100%",
                  opacity: front === "a" ? 1 : 0,
                }}
                contentFit="cover"
                cachePolicy="none"
                onLoad={() => onBufferLoad("a")}
              />
            )}
            {uriB && (
              <ExpoImage
                source={{ uri: uriB }}
                style={{
                  position: "absolute",
                  width: "100%",
                  height: "100%",
                  opacity: front === "b" ? 1 : 0,
                }}
                contentFit="cover"
                cachePolicy="none"
                onLoad={() => onBufferLoad("b")}
              />
            )}
            {snapFails >= STALE_AFTER_FAILS && (
              <View
                style={{
                  position: "absolute",
                  top: space.sm,
                  left: space.sm,
                  flexDirection: "row",
                  alignItems: "center",
                  gap: 4,
                  backgroundColor: "rgba(0,0,0,0.6)",
                  paddingHorizontal: space.sm,
                  paddingVertical: 4,
                  borderRadius: 999,
                }}
              >
                <Ionicons name="alert-circle" size={13} color={c.warn} />
                <Text style={[type.small, { color: "#fff" }]}>Feed stale</Text>
              </View>
            )}
          </View>
        </Surface>
      ) : snapErr ? (
        <Surface padded>
          <Text style={[type.small, { color: c.muted }]}>Camera: {snapErr}</Text>
        </Surface>
      ) : null}

      {/* 3D viewer — sits with the camera as the other "see the print" view.
          Enabled only when a job is loaded (printing/paused/preparing); the
          disabled state matches the Quick-actions dimmed-button idiom. Opens
          the bridge's WebGL viewer in-app (native WebView, app/viewer.tsx). */}
      <Surface padded style={{ gap: space.sm }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
          <Ionicons name="cube-outline" size={18} color={jobActive ? c.text : c.muted} />
          <Text style={[type.h2, { color: c.text, flex: 1 }]}>3D view</Text>
          <Button label="View in 3D" disabled={!jobActive} onPress={open3D} />
        </View>
        {!jobActive && (
          <Text style={[type.small, { color: c.muted }]}>
            Available while a print is loaded.
          </Text>
        )}
      </Surface>

      {/* Temps + fans ------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Temperatures &amp; fans</Text>
        <Row label="Nozzle" actual={view.nozzleActual} target={view.nozzleTarget} unit="°C" />
        <Row label="Bed" actual={view.bedActual} target={view.bedTarget} unit="°C" />
        {view.chamberTemp != null && (
          <Row label="Chamber" actual={view.chamberTemp} target={null} unit="°C" />
        )}
        {view.fanPart != null && (
          <Row label="Part fan" actual={view.fanPart} target={null} unit="%" />
        )}
        {view.fanAux != null && (
          <Row label="Aux fan" actual={view.fanAux} target={null} unit="%" />
        )}
      </Surface>

      {/* Quick actions ------------------------------------------------------ */}
      <Surface padded style={{ gap: space.sm }}>
        <Text style={[type.h2, { color: c.text }]}>Quick actions</Text>
        <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
          {/* Pause — enabled ONLY when actively printing. Disabled (dimmed)
              for every other phase so the button stays anchored in the layout
              rather than popping in and out. Fail-safe: also disabled when the
              WS is not open (phase may be stale cached state). */}
          <Button
            label="Pause"
            variant="secondary"
            disabled={!wsConnected || view.phase !== "printing"}
            onPress={() => safeCall(() => printAction(selected.serial, "pause"), "Paused")}
          />
          {/* Resume — enabled ONLY when paused. */}
          <Button
            label="Resume"
            disabled={!wsConnected || view.phase !== "paused"}
            onPress={() => safeCall(() => printAction(selected.serial, "resume"), "Resumed")}
          />
          {/* Stop — enabled when printing, paused, or preparing. Confirmation
              dialog guards against an accidental tap — the bridge command is
              fire-and-forget so there is no undo. */}
          <Button
            label="Stop"
            variant="danger"
            disabled={
              !wsConnected ||
              (view.phase !== "printing" &&
                view.phase !== "paused" &&
                view.phase !== "preparing")
            }
            onPress={() =>
              Alert.alert(
                "Stop print?",
                "The current job will be cancelled and cannot be resumed.",
                [
                  { text: "Keep printing", style: "cancel" },
                  {
                    text: "Stop print",
                    style: "destructive",
                    onPress: () =>
                      safeCall(() => printAction(selected.serial, "stop"), "Stopped"),
                  },
                ],
              )
            }
          />
          <Button
            label={view.lightOn ? "Light off" : "Light on"}
            variant="secondary"
            onPress={() => safeCall(
              () => setLight(selected.serial, !view.lightOn),
              view.lightOn ? "Light off" : "Light on",
            )}
          />
        </View>
        {/* Hint shown when all three print-control buttons are disabled so the
            user understands why nothing responds. Only shown when idle/unknown
            (not when disconnected — the status bar above already explains that). */}
        {wsConnected &&
          view.phase !== "printing" &&
          view.phase !== "paused" &&
          view.phase !== "preparing" && (
            <Text style={[type.small, { color: c.muted }]}>
              Nothing printing — controls enabled when a print is active.
            </Text>
          )}
      </Surface>
    </ScrollView>
  );

  function safeCall(fn: () => Promise<unknown>, okMsg: string) {
    fn()
      .then(() => showToast(okMsg, { severity: "success" }))
      .catch((e) =>
        showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" })
      );
  }

  // Open the in-app 3D viewer (native WebView, app/viewer.tsx). The viewer
  // screen resolves the tokenized URL itself (via buildViewerUrl) and handles
  // the no-connection / load-error cases, so here we just navigate.
  function open3D() {
    router.push({ pathname: "/viewer", params: { printer: selected.serial } });
  }
}

interface IssueEntryProps {
  severity: string;
  text: string;
  remediation: string | null;
  wikiUrl: string | null;
  /** Phase-tuned advice that replaces `remediation` when present. */
  contextNote?: string | null;
  /** When true, dims the entry and adds a stale-badge line. */
  stale?: boolean;
}

function IssueEntry({ severity, text, remediation, wikiUrl, contextNote, stale }: IssueEntryProps) {
  const { c, type, space, radius } = useTheme();

  // Map severity to an accent color. "error" → danger, "warn" → warn (amber),
  // "info" → blue, anything else → muted. Stale entries always render muted.
  const accentColor = stale
    ? c.muted
    : severity === "error" ? c.danger
      : severity === "warn" ? c.warn
        : severity === "info" ? c.blue
          : c.muted;

  // Stale entries dim all text to reduce visual weight.
  const textColor = stale ? c.muted : c.text;

  // context_note replaces generic remediation when present (more specific).
  const adviceLine = contextNote ?? remediation;

  return (
    <View
      style={{
        borderLeftWidth: 3,
        borderLeftColor: accentColor,
        paddingLeft: space.md,
        gap: space.xs,
        opacity: stale ? 0.65 : 1,
      }}
    >
      <Text style={[type.body, { color: textColor }]}>{text}</Text>
      {adviceLine ? (
        <Text style={[type.small, { color: c.muted }]}>{adviceLine}</Text>
      ) : null}
      {stale ? (
        <Text style={[type.small, { color: c.muted, fontStyle: "italic" }]}>
          Stale — from a previous job. Dismiss on the printer or power-cycle.
        </Text>
      ) : null}
      {wikiUrl && !stale ? (
        <Pressable
          onPress={() => {
            if (isSafeBambuWikiUrl(wikiUrl)) {
              void Linking.openURL(wikiUrl);
            }
          }}
          style={{ alignSelf: "flex-start" }}
        >
          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              gap: 4,
              backgroundColor: c.surface2,
              borderRadius: radius.sm,
              paddingHorizontal: space.sm,
              paddingVertical: 3,
            }}
          >
            <Text style={[type.small, { color: c.blue }]}>Bambu Wiki</Text>
            <Ionicons name="open-outline" size={11} color={c.blue} />
          </View>
        </Pressable>
      ) : null}
    </View>
  );
}

function Row({ label, actual, target, unit }: { label: string; actual: number | null; target: number | null; unit: string }) {
  const { c, type } = useTheme();
  if (actual == null) return null;
  return (
    <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
      <Text style={[type.body, { color: c.muted }]}>{label}</Text>
      <Text style={[type.mono, { color: c.text }]}>
        {actual.toFixed(1)}{unit}
        {target != null && target > 0 ? ` / ${target.toFixed(0)}${unit}` : ""}
      </Text>
    </View>
  );
}

function formatMin(min: number): string {
  if (min < 60) return `${Math.round(min)}m`;
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return `${h}h ${m}m`;
}

// RN Hermes lacks btoa in some configs; spell it out so the snapshot
// data: URI works regardless of runtime.
function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
  if (typeof btoa === "function") return btoa(bin);
  // Fallback — base64 by hand (rare).
  return globalThis.Buffer ? globalThis.Buffer.from(bin, "binary").toString("base64") : bin;
}
