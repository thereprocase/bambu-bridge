/**
 * Settings — bridge URLs (remote + optional LAN) + bearer key + theme toggle
 * + health probe.
 *
 * The bridge config is what every other screen depends on, so this is the
 * only place the user goes if they haven't onboarded yet (root `index.tsx`
 * bounces here when baseUrl/bearer are unset).
 *
 * Two URLs:
 *   - REMOTE (Tailscale) — always-available fallback, used anywhere.
 *   - LAN (optional) — faster, used automatically when the phone is on a
 *     network the user has saved. "Saved networks" are matched by /24 subnet
 *     fingerprint (no SSID, no location permission).
 *
 * Health probe is a non-blocking `GET /printers` (any 2xx counts) plus an
 * auth check on the bearer (401 → key invalid). The LAN field gets its own
 * probe line so the user can tell the LAN URL works *before* they save the
 * network it's on.
 */

import { Link } from "expo-router";
import * as Network from "expo-network";
import { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import { request, requestExact } from "../src/api/client";
import { prefixOf, resetEndpointCache, resolveEndpoint } from "../src/api/endpoint";
import { BridgeError, BridgeNetworkError } from "../src/api/errors";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { StatusDot } from "../src/components/StatusDot";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import { useBridgeStore } from "../src/store/bridge";
import { useTheme } from "../src/theme/ThemeProvider";

type ProbeState =
  | { state: "idle" }
  | { state: "ok" }
  | { state: "auth_invalid" }
  | { state: "unreachable"; detail: string };

export default function SettingsScreen() {
  const { c, space, type, radius } = useTheme();
  const showToast = useToastStore((s) => s.show);

  const baseUrl = useBridgeStore((s) => s.baseUrl);
  const baseUrlLan = useBridgeStore((s) => s.baseUrlLan);
  const savedNetworks = useBridgeStore((s) => s.savedNetworks);
  const bearer = useBridgeStore((s) => s.bearer);
  const health = useBridgeStore((s) => s.health);
  const setBaseUrl = useBridgeStore((s) => s.setBaseUrl);
  const setBaseUrlLan = useBridgeStore((s) => s.setBaseUrlLan);
  const addSavedNetwork = useBridgeStore((s) => s.addSavedNetwork);
  const removeSavedNetwork = useBridgeStore((s) => s.removeSavedNetwork);
  const setBearer = useBridgeStore((s) => s.setBearer);
  const setHealth = useBridgeStore((s) => s.setHealth);

  const { mode, override, setOverride } = useTheme();

  const [urlInput, setUrlInput] = useState(baseUrl ?? "");
  const [lanInput, setLanInput] = useState(baseUrlLan ?? "");
  const [keyInput, setKeyInput] = useState(bearer ?? "");
  const [probing, setProbing] = useState(false);
  const [lanProbe, setLanProbe] = useState<ProbeState>({ state: "idle" });

  // Which path the *remote/LAN resolver* would use right now ("via LAN" /
  // "via Tailscale"), shown next to the Reachable line. Re-resolved after
  // each successful probe.
  const [activePath, setActivePath] = useState<"lan" | "remote" | null>(null);

  // The /24 prefix of the network the phone is currently on (Wi-Fi only),
  // and whether we're on Wi-Fi at all. Drives the "Save this network" button.
  const [currentPrefix, setCurrentPrefix] = useState<string | null>(null);
  const [onWifi, setOnWifi] = useState(false);

  useEffect(() => {
    setUrlInput(baseUrl || urlInput);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl]);

  useEffect(() => {
    setLanInput(baseUrlLan ?? "");
  }, [baseUrlLan]);

  // Read the current network fingerprint on mount + whenever the LAN URL or
  // saved set changes (a save/remove should re-evaluate the button).
  const refreshNetwork = useCallback(async () => {
    try {
      const state = await Network.getNetworkStateAsync();
      const wifi = state.type === Network.NetworkStateType.WIFI;
      setOnWifi(wifi);
      if (wifi) {
        const ip = await Network.getIpAddressAsync();
        setCurrentPrefix(prefixOf(ip));
      } else {
        setCurrentPrefix(null);
      }
    } catch {
      setOnWifi(false);
      setCurrentPrefix(null);
    }
  }, []);

  useEffect(() => {
    void refreshNetwork();
  }, [refreshNetwork]);

  async function save() {
    try {
      await setBearer(keyInput);
      setBaseUrl(urlInput);
      setBaseUrlLan(lanInput); // "" clears it
      resetEndpointCache();
      await probe();
      await refreshNetwork();
    } catch {
      showToast("Couldn't save the API key. Unlock the phone and try again.", { severity: "danger" });
    }
  }

  async function probe() {
    setProbing(true);
    try {
      // /printers is auth-required, so this exercises both the URL and key.
      // Goes through the resolver, so it tests whatever path is live now.
      await request<unknown>("/printers");
      setHealth({ state: "ok", at: Date.now() });
      try {
        const { path } = await resolveEndpoint();
        setActivePath(path);
      } catch {
        setActivePath(null);
      }
      showToast("Bridge reachable", { severity: "success" });
    } catch (e) {
      setActivePath(null);
      if (e instanceof BridgeError && (e.code === "auth_invalid" || e.code === "auth_missing")) {
        setHealth({ state: "auth_invalid", at: Date.now() });
      } else if (e instanceof BridgeNetworkError) {
        setHealth({ state: "unreachable", at: Date.now(), detail: e.message });
      } else if (e instanceof BridgeError) {
        setHealth({ state: "unreachable", at: Date.now(), detail: `${e.code}: ${e.message}` });
      } else {
        setHealth({ state: "unreachable", at: Date.now(), detail: String(e) });
      }
    } finally {
      setProbing(false);
    }
  }

  // Probe the LAN URL *specifically*, bypassing the resolver — so the user
  // can confirm it works on this network before saving. Uses the typed-in
  // value (not the persisted one) so they can test before Save.
  async function probeLan() {
    const target = lanInput.trim();
    if (!target) {
      setLanProbe({ state: "unreachable", detail: "Enter a LAN URL first." });
      return;
    }
    setLanProbe({ state: "idle" });
    setProbing(true);
    try {
      await requestExact<unknown>(target, "/printers");
      setLanProbe({ state: "ok" });
      showToast("LAN bridge reachable", { severity: "success" });
    } catch (e) {
      if (e instanceof BridgeError && (e.code === "auth_invalid" || e.code === "auth_missing")) {
        setLanProbe({ state: "auth_invalid" });
      } else if (e instanceof BridgeNetworkError) {
        setLanProbe({ state: "unreachable", detail: e.message });
      } else if (e instanceof BridgeError) {
        setLanProbe({ state: "unreachable", detail: `${e.code}: ${e.message}` });
      } else {
        setLanProbe({ state: "unreachable", detail: String(e) });
      }
    } finally {
      setProbing(false);
    }
  }

  function saveCurrentNetwork() {
    if (!currentPrefix) return;
    addSavedNetwork(currentPrefix);
    resetEndpointCache();
    showToast(`Saved network ${currentPrefix}.x`, { severity: "success" });
  }

  const healthDot =
    health.state === "ok" ? "ok"
    : health.state === "auth_invalid" ? "warn"
    : health.state === "unreachable" ? "danger"
    : "neutral";

  const healthLabel =
    health.state === "ok" ? "Reachable"
    : health.state === "auth_invalid" ? "API key rejected"
    : health.state === "unreachable" ? "Unreachable"
    : "Not yet probed";

  const pathSuffix =
    health.state === "ok" && activePath
      ? activePath === "lan" ? " · via LAN" : " · via Tailscale"
      : "";

  const lanDot =
    lanProbe.state === "ok" ? "ok"
    : lanProbe.state === "auth_invalid" ? "warn"
    : lanProbe.state === "unreachable" ? "danger"
    : "neutral";

  const lanLabel =
    lanProbe.state === "ok" ? "LAN reachable"
    : lanProbe.state === "auth_invalid" ? "API key rejected"
    : lanProbe.state === "unreachable" ? "LAN unreachable"
    : "Not yet probed";

  // "Save this network" appears only when it can actually do something:
  //   on Wi-Fi + a LAN URL is set + the LAN probe just succeeded + the
  //   current /24 isn't already saved.
  const alreadySaved =
    !!currentPrefix && savedNetworks.some((n) => n.prefix === currentPrefix);
  const canSaveNetwork =
    onWifi &&
    !!lanInput.trim() &&
    lanProbe.state === "ok" &&
    !!currentPrefix &&
    !alreadySaved;

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
    >
      {/* Bridge connection ------------------------------------------------ */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Bridge</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Where your Bambu Bridge server is reachable. The remote (Tailscale)
          URL works anywhere. Add a LAN URL too and the app uses it
          automatically — and falls back to Tailscale — when you&apos;re on a
          saved network. Include the `/api/v1` path; the default port is 8080.
        </Text>

        <Field
          label="Remote base URL (Tailscale)"
          value={urlInput}
          onChangeText={setUrlInput}
          placeholder="http://your-bridge-host:8080/api/v1"
          keyboardType="url"
          autoCorrect={false}
        />

        <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
          <StatusDot state={healthDot} />
          <Text style={[type.small, { color: c.muted, flex: 1 }]}>
            {healthLabel}
            {pathSuffix}
            {health.state === "unreachable" && health.detail ? ` — ${health.detail}` : ""}
          </Text>
        </View>

        <Field
          label="LAN base URL (optional)"
          value={lanInput}
          onChangeText={setLanInput}
          placeholder="http://192.168.1.50:8080/api/v1"
          keyboardType="url"
          autoCorrect={false}
          hint="Used first when you're on a saved network; falls back to Tailscale automatically."
        />

        {!!lanInput.trim() && (
          <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
            <StatusDot state={lanDot} />
            <Text style={[type.small, { color: c.muted, flex: 1 }]}>
              {lanLabel}
              {lanProbe.state === "unreachable" && lanProbe.detail ? ` — ${lanProbe.detail}` : ""}
            </Text>
          </View>
        )}

        {!!lanInput.trim() && (
          <Button label="Probe LAN URL" variant="secondary" onPress={probeLan} loading={probing} fullWidth />
        )}

        <Field
          label="API key (bearer)"
          value={keyInput}
          onChangeText={setKeyInput}
          placeholder="paste the bridge.env BRIDGE_API_KEY"
          secureTextEntry
        />

        <View style={{ flexDirection: "row", gap: space.sm }}>
          <Button label="Save & probe" onPress={save} loading={probing} fullWidth />
        </View>
        {baseUrl && bearer && (
          <Button label="Probe again" onPress={probe} variant="secondary" loading={probing} fullWidth />
        )}
      </Surface>

      {/* Saved networks --------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Saved networks</Text>
        <Text style={[type.small, { color: c.muted }]}>
          When you&apos;re on a saved network, the app talks to the bridge over
          your LAN first and falls back to Tailscale automatically. Networks are
          matched by subnet (the first three numbers of your Wi-Fi IP) — no
          location or Wi-Fi-name access needed.
        </Text>

        {savedNetworks.length === 0 ? (
          <Text style={[type.small, { color: c.muted }]}>
            None saved yet. Connect to your home Wi-Fi, probe the LAN URL above,
            then tap “Save this network”.
          </Text>
        ) : (
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: space.sm }}>
            {savedNetworks.map((n) => (
              <Pressable
                key={n.prefix}
                onPress={() => {
                  removeSavedNetwork(n.prefix);
                  resetEndpointCache();
                  showToast(`Removed ${n.prefix}.x`, { severity: "info" });
                }}
                style={{
                  flexDirection: "row",
                  alignItems: "center",
                  gap: space.xs,
                  paddingHorizontal: space.md,
                  paddingVertical: space.sm,
                  backgroundColor: c.surface2,
                  borderColor: c.border,
                  borderWidth: 1,
                  borderRadius: radius.md,
                }}
              >
                <Text style={[type.small, { color: c.text }]}>
                  {n.prefix}.x
                </Text>
                <Text style={[type.small, { color: c.muted }]}>
                  · added {formatDate(n.addedAt)} · tap to remove
                </Text>
              </Pressable>
            ))}
          </View>
        )}

        {canSaveNetwork && (
          <Button
            label={`Save this network (${currentPrefix}.x)`}
            onPress={saveCurrentNetwork}
            fullWidth
          />
        )}
        {onWifi && !!lanInput.trim() && lanProbe.state !== "ok" && !alreadySaved && (
          <Text style={[type.small, { color: c.muted }]}>
            Probe the LAN URL successfully to save this network.
          </Text>
        )}
        {onWifi && alreadySaved && (
          <Text style={[type.small, { color: c.muted }]}>
            This network ({currentPrefix}.x) is already saved.
          </Text>
        )}
        {!onWifi && (
          <Text style={[type.small, { color: c.muted }]}>
            Connect to Wi-Fi to save the current network.
          </Text>
        )}
      </Surface>

      {/* Printers --------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Printers</Text>
        <Text style={[type.small, { color: c.muted }]}>
          Each printer is added separately. The bridge checks the connection
          and access code before saving — a wrong IP or access code is caught
          here, not later.
        </Text>
        <Link href="/add-printer" asChild>
          <Button label="Add a printer" onPress={() => {}} fullWidth />
        </Link>
        {baseUrl && bearer && (
          <Link href="/(tabs)/status" asChild>
            <Button label="Open dashboard" variant="secondary" onPress={() => {}} fullWidth />
          </Link>
        )}
      </Surface>

      {/* Appearance ------------------------------------------------------- */}
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Appearance</Text>
        <View style={{ flexDirection: "row", gap: space.sm }}>
          <Button
            label="System"
            variant={override === null ? "primary" : "secondary"}
            onPress={() => setOverride(null)}
          />
          <Button
            label="Dark"
            variant={override === "dark" ? "primary" : "secondary"}
            onPress={() => setOverride("dark")}
          />
          <Button
            label="Light"
            variant={override === "light" ? "primary" : "secondary"}
            onPress={() => setOverride("light")}
          />
        </View>
        <Text style={[type.small, { color: c.muted }]}>
          Currently rendering: {mode}
        </Text>
      </Surface>
    </ScrollView>
  );
}

function formatDate(ms: number): string {
  try {
    return new Date(ms).toLocaleDateString();
  } catch {
    return "—";
  }
}
