/**
 * "Wrong spool?" — move a finished print's grams from the spool it was
 * charged to onto the spool that fed it. Opened from a Recent prints row:
 *   router.push({ pathname: "/wrong-spool", params: { attempt, from, grams, title } })
 * One screen: pick the right spool (same material first), confirm the
 * grams (whole, defaulting to all of it), done.
 */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import { inventorySpool, inventorySpools, reassignCharge, type InventorySpool } from "../src/api/inventory";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import { gramsText, parseGrams } from "../src/lib/heldPrints";
import { gramsLeft, inventoryErrorText, spoolLabel } from "../src/lib/slotInventory";
import { useTheme } from "../src/theme/ThemeProvider";

function firstParam(v: string | string[] | undefined): string | undefined {
  const s = Array.isArray(v) ? v[0] : v;
  return s && s.length ? s : undefined;
}

export default function WrongSpoolScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const params = useLocalSearchParams();
  const attemptId = firstParam(params.attempt);
  const fromId = Number(firstParam(params.from));
  const title = firstParam(params.title) ?? "Print";
  // The exact charged figure (the bridge writes hundredths); shown and typed as whole grams.
  const exact = Number(firstParam(params.grams) ?? 0);
  const all = Math.round(exact);

  const [from, setFrom] = useState<InventorySpool | null>(null);
  const [choices, setChoices] = useState<InventorySpool[]>([]);
  const [to, setTo] = useState<InventorySpool | null>(null);
  const [grams, setGrams] = useState(String(all));
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);

  const load = useCallback(async () => {
    if (!attemptId || !Number.isInteger(fromId)) return;
    const current = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const origin = await inventorySpool(fromId);
      const material = origin.filament.material?.trim() || undefined;
      const spools = (await inventorySpools("", 0, material)).filter((s) => !s.archived && s.id !== fromId);
      // Older bridges ignore the material filter; same material first either way.
      const same = (s: InventorySpool) => !!material && (s.filament.material ?? "").toUpperCase() === material.toUpperCase();
      spools.sort((a, b) => Number(same(b)) - Number(same(a)));
      if (current !== generation.current) return;
      setFrom(origin);
      setChoices(spools);
    } catch (e) {
      if (current === generation.current) setError(inventoryErrorText(e));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [attemptId, fromId]);
  useEffect(() => {
    void load();
    return () => {
      generation.current++;
    };
  }, [load]);

  const amount = parseGrams(grams);
  const ready = !!to && amount != null && amount <= Math.max(all, 1);
  // "All of it" is sent without a figure: the bridge moves everything this print put on the
  // spool, to the hundredth. A rounded 84 for 83.7 g would be refused as more than the print has.
  const everything = amount != null && amount >= Math.floor(exact);

  async function move() {
    if (!attemptId || !to || amount == null || busy) return;
    setBusy(true);
    setError("");
    try {
      await reassignCharge(attemptId, { from_spool_id: fromId, to_spool_id: to.id, ...(everything ? {} : { grams: amount }) });
      showToast(`Moved ${amount} g from #${fromId} to #${to.id}`, { severity: "success" });
      router.back();
    } catch (e) {
      setError(inventoryErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  const body = [type.body, { color: c.text }];
  const small = [type.small, { color: c.muted }];
  const term = query.trim().toLowerCase();
  const visible = term ? choices.filter((s) => spoolLabel(s).toLowerCase().includes(term)) : choices;

  if (!attemptId || !Number.isInteger(fromId)) {
    return (
      <View style={{ flex: 1, backgroundColor: c.bg, padding: space.lg, gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Wrong spool?</Text>
        <Text style={body}>Couldn't tell which print this is for. Go back and tap it again.</Text>
        <Button label="Back" variant="secondary" onPress={() => router.back()} fullWidth />
      </View>
    );
  }

  return (
    <ScrollView keyboardShouldPersistTaps="handled" style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
      <View style={{ gap: space.xs }}>
        <Text style={[type.h1, { color: c.text }]} numberOfLines={2}>{title}</Text>
        <Text style={small}>{all} g charged to {from ? spoolLabel(from) : `#${fromId}`}</Text>
      </View>
      {!!error && <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>}

      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Which spool fed it?</Text>
        {to ? (
          <>
            <Text style={body}>→ {spoolLabel(to)}</Text>
            <Button label="Pick a different spool" variant="secondary" disabled={busy} onPress={() => setTo(null)} />
          </>
        ) : (
          <>
            <Field label="Search spools" value={query} onChangeText={setQuery} placeholder="Number, vendor or name" />
            {visible.map((spool) => (
              <Pressable
                key={spool.id}
                accessibilityRole="button"
                accessibilityLabel={`${spoolLabel(spool)}, ${gramsLeft(spool)}`}
                disabled={busy}
                onPress={() => setTo(spool)}
                style={{ paddingVertical: space.sm, borderTopWidth: 1, borderTopColor: c.borderSoft }}
              >
                <Text style={body} numberOfLines={1}>{spoolLabel(spool)}</Text>
                <Text style={small}>{gramsLeft(spool)}{spool.filament.material ? ` · ${spool.filament.material}` : ""}</Text>
              </Pressable>
            ))}
            {!loading && !visible.length && <Text style={small}>{term ? "No spools match." : "No other spools in Spoolman."}</Text>}
          </>
        )}
      </Surface>

      {to && (
        <Surface padded style={{ gap: space.md }}>
          <Field
            label="Grams to move"
            value={grams}
            keyboardType="number-pad"
            onChangeText={(text) => setGrams(gramsText(text))}
            error={amount == null ? "Whole grams, 1 or more." : amount > all ? `This print put ${all} g on #${fromId}.` : null}
            hint={`All of it is ${all} g.`}
          />
          <Button
            label={amount != null ? `Move ${amount} g from #${fromId} to #${to.id}` : "Move"}
            disabled={busy || !ready}
            loading={busy}
            onPress={() => void move()}
          />
        </Surface>
      )}
      <Button label="Cancel" variant="secondary" disabled={busy} onPress={() => router.back()} fullWidth />
    </ScrollView>
  );
}
