/**
 * Activity — recent events.
 *
 * Two sources:
 *   1. The WS live event log (in-memory, current session — drives the
 *      "since you opened the app" feed and toasts).
 *   2. /printers/{id}/events — bridge-persisted, for "since the bridge
 *      started" history. After PR B lands this returns a JSON list of
 *      stored events with severity + dismissed_at.
 *
 * If /events 404s (pre-PR-B bridge), we just render the WS log — no
 * crash. Same screen post-PR-B will pull both.
 */

import { useEffect, useState } from "react";
import { Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import { dismissEvent, listEvents } from "../../src/api/events";
import { BridgeError, BridgeNetworkError } from "../../src/api/errors";
import { EventRow } from "../../src/api/types";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function ActivityScreen() {
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));
  const wsLog = live?.eventLog ?? [];

  const [persisted, setPersisted] = useState<EventRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [endpointMissing, setEndpointMissing] = useState(false);

  async function refresh() {
    if (!selectedId) return;
    setLoading(true);
    try {
      const got = await listEvents(selectedId, { limit: 100 });
      setPersisted(got);
      setEndpointMissing(false);
    } catch (e) {
      if (e instanceof BridgeError && (e.code === "not_found" || e.status === 404)) {
        setEndpointMissing(true);
      } else if (!(e instanceof BridgeNetworkError)) {
        showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" });
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  async function dismiss(ev: EventRow) {
    if (!selectedId) return;
    try {
      await dismissEvent(selectedId, ev.id);
      await refresh();
    } catch (e) {
      showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" });
    }
  }

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={<RefreshControl refreshing={loading} tintColor={c.accent} onRefresh={refresh} />}
    >
      {/* This session (WS) -------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>This session</Text>
        {wsLog.length === 0 && (
          <Text style={[type.small, { color: c.muted }]}>
            Nothing yet — events fire on prints starting, completing, errors,
            etc.
          </Text>
        )}
        {wsLog.map((ev, i) => (
          <View
            key={`${ev.at}-${i}`}
            style={{
              borderTopWidth: i > 0 ? 1 : 0,
              borderTopColor: c.borderSoft,
              paddingTop: i > 0 ? space.md : 0,
              gap: 2,
            }}
          >
            <Text style={[type.body, { color: severityColor(c, ev.name) }]}>
              {humanize(ev.name)}
            </Text>
            <Text style={[type.caption, { color: c.muted }]}>
              {new Date(ev.at).toLocaleTimeString()}
            </Text>
            {Object.keys(ev.data).length > 0 && (
              <Text style={[type.small, { color: c.muted }]}>
                {summarize(ev.data)}
              </Text>
            )}
          </View>
        ))}
      </Surface>

      {/* Persisted (bridge) ------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>History</Text>
        {endpointMissing && (
          <Text style={[type.small, { color: c.muted }]}>
            Saved history isn&apos;t available on this bridge version yet. Update
            the bridge server to see past events here.
          </Text>
        )}
        {!endpointMissing && persisted.length === 0 && !loading && (
          <Text style={[type.small, { color: c.muted }]}>No saved events yet.</Text>
        )}
        {persisted.map((ev) => (
          <Pressable
            key={ev.id}
            onLongPress={() => dismiss(ev)}
            style={{
              borderTopWidth: 1,
              borderTopColor: c.borderSoft,
              paddingTop: space.md,
              gap: 2,
              opacity: ev.dismissed ? 0.4 : 1,
            }}
          >
            {/* §8.4: title/detail/context are pre-rendered by the bridge —
                show them verbatim rather than re-deriving from a payload. */}
            <Text style={[type.body, { color: severityColor(c, ev.kind, ev.severity) }]}>
              {ev.title || humanize(ev.kind)}
            </Text>
            <Text style={[type.caption, { color: c.muted }]}>
              {new Date(ev.ts).toLocaleString()}
              {ev.severity ? ` · ${ev.severity}` : ""}
            </Text>
            {ev.detail && ev.detail !== "—" && (
              <Text style={[type.small, { color: c.muted }]}>{ev.detail}</Text>
            )}
            {ev.context && ev.context !== "—" && (
              <Text style={[type.small, { color: c.muted }]}>{ev.context}</Text>
            )}
          </Pressable>
        ))}
        {persisted.length > 0 && (
          <Text style={[type.caption, { color: c.muted }]}>
            Long-press to dismiss.
          </Text>
        )}
      </Surface>
    </ScrollView>
  );
}

function humanize(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/^./, (s) => s.toUpperCase());
}

function severityColor(c: ReturnType<typeof useTheme>["c"], name: string, sev?: string): string {
  // §8.4 persisted severity is info|warn|error; WS-log rows pass none and we
  // fall through to name-based inference. ("warning"/"critical" kept for any
  // legacy in-memory event that still carries them.)
  if (sev === "critical" || sev === "error") return c.danger;
  if (sev === "warn" || sev === "warning") return c.warn;
  if (sev === "info") return c.text;
  if (name.includes("error") || name.includes("failed") || name === "cert_changed") return c.danger;
  if (name.includes("warning") || name.includes("runout") || name === "feed_warning") return c.warn;
  if (name === "print_completed" || name === "cert_trusted") return c.accent;
  return c.text;
}

function summarize(data: Record<string, unknown>): string {
  return Object.entries(data)
    .slice(0, 4)
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" · ");
}
