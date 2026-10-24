/**
 * Settings · Spoolman — where the bridge keeps spools, and one switch for
 * showing those spools inside Orca. The switch stands for the bridge's
 * display policy (global flag plus the printers it applies to); for the
 * owner's single printer that is one decision, so it reads as one switch.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Linking, Switch, Text, View } from "react-native";

import {
  inventoryDisplay,
  inventoryStatus,
  saveInventoryDisplay,
  type InventoryDisplayStatus,
  type InventoryStatus,
} from "../api/inventory";
import { inventoryErrorText } from "../lib/slotInventory";
import { usePrintersStore } from "../store/printers";
import { useTheme } from "../theme/ThemeProvider";
import { Button } from "./Button";
import { Surface } from "./Surface";

/** Why Orca is or isn't showing the spools right now, in plain words. */
export function displayReasonText(state: InventoryDisplayStatus): string {
  if (state.effective_enabled) return "Orca shows them now.";
  const reasons: Record<string, string> = {
    disabled: "Off.",
    not_qualified: "This bridge build can't show them yet.",
    native_gateway_unavailable: "Orca's connection to the bridge is down.",
    inventory_unavailable: "Spoolman is unreachable.",
    policy_unavailable: "The bridge couldn't read this setting.",
    refresh_unavailable: "Waiting for the bridge to read the spools.",
    stopped: "Orca's connection to the bridge is stopped.",
    candidate: "Ready; Orca will show them on its next refresh.",
  };
  return reasons[state.reason] ?? "Orca isn't showing them right now.";
}

export function SpoolmanSettings() {
  const { c, space, type } = useTheme();
  const list = usePrintersStore((s) => s.list);
  // A selector must return a stable value; derive the serials outside it.
  const printers = useMemo(() => list.map((p) => p.serial), [list]);
  const [status, setStatus] = useState<InventoryStatus | null>(null);
  const [display, setDisplay] = useState<InventoryDisplayStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const state = await inventoryStatus();
      setStatus(state);
      if (state.configured && state.api_version === 2) setDisplay(await inventoryDisplay());
      setError("");
    } catch (e) {
      setError(inventoryErrorText(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function toggle(on: boolean) {
    if (!display || busy) return;
    setBusy(true);
    setError("");
    try {
      await saveInventoryDisplay({
        provider_id: display.provider_id,
        expected_revision: display.revision,
        global_enabled: on,
        printers: on ? printers : [],
      });
      await load();
    } catch (e) {
      setError(inventoryErrorText(e));
      await load().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  const body = [type.body, { color: c.text }];
  const small = [type.small, { color: c.muted }];
  const url = (status as (InventoryStatus & { spoolman_web_url?: string | null }) | null)?.spoolman_web_url ?? null;
  const on = !!display?.global_enabled && (printers.length === 0 || printers.some((p) => display!.printers.includes(p)));

  return (
    <Surface padded style={{ gap: space.md }}>
      <Text style={[type.h2, { color: c.text }]}>Spoolman</Text>
      {status && !status.configured && (
        <Text style={body}>This bridge has no Spoolman address. Set BRIDGE_SPOOLMAN_URL on the bridge to track spools.</Text>
      )}
      {status?.configured && (
        <Text style={small}>Spools, products and vendors live in Spoolman. The bridge assigns them to slots and charges what each print uses.</Text>
      )}
      {!!url && (
        <Button label="Open Spoolman" variant="secondary" onPress={() => void Linking.openURL(url)} />
      )}
      {!!error && (
        <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>
      )}
      {display && (
        <View style={{ gap: space.xs }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
            <Switch
              value={on}
              onValueChange={(value) => void toggle(value)}
              disabled={busy || (!on && !display.runtime_qualified)}
              accessibilityLabel="Show Spoolman spools in Orca"
            />
            <Text style={[...body, { flex: 1 }]}>Show Spoolman spools in Orca</Text>
          </View>
          <Text style={small}>{displayReasonText(display)}</Text>
        </View>
      )}
    </Surface>
  );
}
