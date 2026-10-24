/**
 * Assign a Spoolman spool to one physical source (an AMS tray or the external
 * holder). Opened from a slot row on the Filament tab:
 *   router.push({ pathname: "/assign-spool", params: { printer, source } })
 *
 * Lists spools of the slot's material first; an untagged slot also offers
 * other materials, which the bridge writes to the slot before assigning.
 */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import {
  inventoryAssign,
  inventorySources,
  inventorySpools,
  inventoryStatus,
  type InventorySource,
  type InventorySpool,
  type InventoryStatus,
} from "../src/api/inventory";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import {
  candidateSpools,
  gramsLeft,
  inventoryErrorText,
  materialLine,
  slotTitle,
  staleLine,
  slotViews,
  spoolLabel,
  type SlotView,
} from "../src/lib/slotInventory";
import { useTheme } from "../src/theme/ThemeProvider";

function firstParam(v: string | string[] | undefined): string | undefined {
  const s = Array.isArray(v) ? v[0] : v;
  return s && s.length ? s : undefined;
}

export default function AssignSpoolScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const params = useLocalSearchParams();
  const printerId = firstParam(params.printer);
  const sourceId = firstParam(params.source);

  const [status, setStatus] = useState<InventoryStatus | null>(null);
  const [slot, setSlot] = useState<SlotView | null>(null);
  const [spools, setSpools] = useState<InventorySpool[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);

  const load = useCallback(async () => {
    if (!printerId || !sourceId) return;
    const current = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const [state, sources, records] = await Promise.all([
        inventoryStatus(),
        inventorySources(printerId),
        inventorySpools(),
      ]);
      if (current !== generation.current) return;
      setStatus(state);
      setSpools(records);
      setSlot(slotViews(printerId, sources, state, records).find((v) => v.source.source === sourceId) ?? null);
    } catch (e) {
      if (current === generation.current) setError(inventoryErrorText(e));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [printerId, sourceId]);

  useEffect(() => {
    void load();
    return () => {
      generation.current++;
    };
  }, [load]);

  async function assign(source: InventorySource, spool: InventorySpool | null, setSlotMaterial = false) {
    if (!printerId || busy) return;
    setBusy(true);
    setError("");
    try {
      await inventoryAssign(printerId, {
        source: source.source,
        spool_id: spool?.id ?? null,
        observed_identity: source.observed_identity,
        ...(setSlotMaterial ? { set_slot: true } : {}),
      });
      showToast(
        spool ? `${spoolLabel(spool)} in ${slotTitle(source).toLowerCase()}` : `${slotTitle(source)} has no spool now`,
        { severity: "success" },
      );
      router.back();
    } catch (e) {
      // The bridge may have refused against a list that changed: reload it,
      // then show the reason (load clears the error line as it starts).
      const reason = inventoryErrorText(e);
      await load();
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  const body = [type.body, { color: c.text }];
  const small = [type.small, { color: c.muted }];

  if (!printerId || !sourceId) {
    return (
      <View style={{ flex: 1, backgroundColor: c.bg, padding: space.lg, gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Assign spool</Text>
        <Text style={body}>Couldn't tell which slot to assign. Go back and tap the slot again.</Text>
        <Button label="Back" variant="secondary" onPress={() => router.back()} fullWidth />
      </View>
    );
  }

  const source = slot?.source ?? null;
  const { matching, others } = source
    ? candidateSpools(source, spools, slot?.assignment ?? null, query)
    : { matching: [], others: [] };
  const empty = !!source?.issues.includes("source_empty");
  const stale = source ? staleLine(source) : null;

  const row = (spool: InventorySpool, onPress: () => void, note?: string) => (
    <Pressable
      key={spool.id}
      accessibilityRole="button"
      accessibilityLabel={`${spoolLabel(spool)}, ${gramsLeft(spool)}`}
      disabled={busy}
      onPress={onPress}
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: space.md,
        paddingVertical: space.sm,
        borderTopWidth: 1,
        borderTopColor: c.borderSoft,
        opacity: busy ? 0.5 : 1,
      }}
    >
      <View
        style={{
          width: 24,
          height: 24,
          borderRadius: 12,
          backgroundColor: spool.filament.color_hex ? `#${spool.filament.color_hex.replace(/^#/, "").slice(0, 6)}` : c.surface3,
          borderWidth: 1,
          borderColor: c.border,
        }}
      />
      <View style={{ flex: 1, gap: 2 }}>
        <Text style={body} numberOfLines={1}>{spoolLabel(spool)}</Text>
        <Text style={small}>{note ? `${gramsLeft(spool)} · ${note}` : gramsLeft(spool)}</Text>
      </View>
    </Pressable>
  );

  return (
    <ScrollView
      keyboardShouldPersistTaps="handled"
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
    >
      <View style={{ gap: space.xs }}>
        <Text style={[type.h1, { color: c.text }]}>{source ? slotTitle(source) : "Assign spool"}</Text>
        {source && <Text style={small}>{materialLine(source)}</Text>}
      </View>

      {!!error && (
        <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>
      )}
      {loading && !slot && <Text style={small}>Loading spools…</Text>}
      {!loading && !slot && !error && (
        <Text style={body}>This slot is no longer reported by the printer. Go back and refresh.</Text>
      )}

      {slot?.assignment && source && (
        <Surface padded style={{ gap: space.sm }}>
          <Text style={small}>NOW IN THIS SLOT</Text>
          <Text style={body}>
            {slot.spool ? spoolLabel(slot.spool) : `#${slot.assignment.spool_id}`} · {gramsLeft(slot.spool)}
          </Text>
          <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
            <Button
              label={`Weigh or archive #${slot.assignment.spool_id}`}
              variant="secondary"
              disabled={busy}
              onPress={() => router.push({ pathname: "/spool", params: { id: String(slot.assignment!.spool_id), printer: printerId, source: source.source } })}
            />
            <Button
              label="Remove from slot"
              variant="secondary"
              disabled={busy}
              onPress={() => void assign(source, null)}
            />
          </View>
        </Surface>
      )}

      {source && empty && (
        <Text style={body}>Put a spool in first. The printer reports this slot as empty.</Text>
      )}
      {source && !empty && stale && (
        <Text style={body}>The printer hasn't reported this slot lately ({stale}). Try again in a moment.</Text>
      )}

      {source && !empty && !stale && (
        <Surface padded style={{ gap: space.md }}>
          <Field label="Search spools" value={query} onChangeText={setQuery} placeholder="Number, vendor or name" />
          <Text style={[type.h2, { color: c.text }]}>
            {source.material.trim() ? `${source.material.trim()} spools` : "Spools"}
          </Text>
          {matching.map((spool) => row(spool, () => void assign(source, spool)))}
          {!loading && matching.length === 0 && (
            <Text style={small}>
              {query.trim()
                ? "No spools match."
                : slot?.assignment
                  ? `No other ${source.material.trim() || "matching"} spools in Spoolman.`
                  : `No ${source.material.trim() || "matching"} spools in Spoolman yet.`}
            </Text>
          )}
          {others.length > 0 && (
            <>
              <Text style={[type.h2, { color: c.text, marginTop: space.sm }]}>Other materials</Text>
              <Text style={small}>Picking one sets the slot to that material first.</Text>
              {others.map((spool) =>
                row(spool, () => void assign(source, spool, true), `sets slot to ${(spool.filament.material ?? "").toUpperCase()}`),
              )}
            </>
          )}
        </Surface>
      )}

      {source && !empty && !stale && (
        <Button
          label="Add a new spool to Spoolman"
          variant="secondary"
          disabled={busy}
          onPress={() => router.replace({ pathname: "/spool", params: { new: "1", printer: printerId, source: source.source } })}
        />
      )}
      <Button label="Cancel" variant="secondary" disabled={busy} onPress={() => router.back()} fullWidth />
    </ScrollView>
  );
}
