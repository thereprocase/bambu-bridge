/**
 * A finished print the bridge could not fully charge, and the one action
 * that closes it. Shown at the top of the Filament tab for every print in
 * delivery_state "review": the part's name, when it finished, each shortfall
 * in one line, and per shortfall a spool to charge. Charge sends one resolve
 * call; Skip closes it with nothing charged; a spool picked for an
 * unassigned slot can stay assigned to that slot.
 */

import { useState } from "react";
import { Pressable, Switch, Text, View } from "react-native";

import {
  inventoryAssign,
  inventorySpools,
  resolveHeldPrint,
  type AccountingRow,
  type InventorySource,
  type InventorySpool,
  type InventoryStatus,
  type Shortfall,
} from "../api/inventory";
import { Button } from "./Button";
import { Field } from "./Field";
import { PartPicture } from "./PartPicture";
import { Surface } from "./Surface";
import { useToastStore } from "./Toast";
import {
  chargeable,
  chargesFor,
  countedText,
  gramsText,
  outcomeWord,
  parseGrams,
  shortfallLine,
  shortfallQuestion,
  slotName,
  whenText,
  type Decision,
} from "../lib/heldPrints";
import { gramsLeft, inventoryErrorText, spoolLabel } from "../lib/slotInventory";
import { useTheme } from "../theme/ThemeProvider";

interface Props {
  row: AccountingRow;
  status: InventoryStatus | null;
  sources: InventorySource[];
  /** Called after the print is closed, so the tab can drop the card. */
  onResolved: (attemptId: string) => void;
}

export function NeedsYouCard({ row, status, sources, onResolved }: Props) {
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const shortfalls = row.shortfalls ?? [];
  const [decisions, setDecisions] = useState<Decision[]>(
    shortfalls.map((shortfall) => ({
      shortfall,
      spoolId: shortfall.kind === "delivery_error" ? shortfall.spool_id : null,
      gramsText: countedText(shortfall),
    })),
  );
  const [picking, setPicking] = useState<number | null>(null);
  const [choices, setChoices] = useState<InventorySpool[]>([]);
  const [known, setKnown] = useState<Map<number, InventorySpool>>(new Map());
  const [keep, setKeep] = useState<Record<number, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const body = [type.body, { color: c.text }];
  const small = [type.small, { color: c.muted }];

  async function openPicker(index: number) {
    const shortfall = shortfalls[index];
    setPicking(index);
    setError("");
    try {
      const material = shortfall.material?.trim() || undefined;
      const spools = (await inventorySpools("", 0, material)).filter((s) => !s.archived);
      // Older bridges ignore the material filter; same material first either way.
      const same = (s: InventorySpool) => !!material && (s.filament.material ?? "").toUpperCase() === material.toUpperCase();
      spools.sort((a, b) => Number(same(b)) - Number(same(a)));
      setChoices(spools);
      setKnown((prev) => {
        const next = new Map(prev);
        spools.forEach((s) => next.set(s.id, s));
        return next;
      });
    } catch (e) {
      setError(inventoryErrorText(e));
      setPicking(null);
    }
  }

  function choose(index: number, spool: InventorySpool) {
    setDecisions((prev) => prev.map((d, i) => (i === index ? { ...d, spoolId: spool.id } : d)));
    if (shortfalls[index].kind === "unassigned_slot") setKeep((prev) => ({ ...prev, [index]: prev[index] ?? true }));
    setPicking(null);
  }

  async function finish(action: "charge" | "nothing" | "charged_elsewhere") {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const charges = action === "charge" ? chargesFor(decisions) : [];
      await resolveHeldPrint(row.attempt_id, { action, charges });
      if (action === "charge") await keepAssignments();
      onResolved(row.attempt_id);
    } catch (e) {
      setError(inventoryErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  /** Assign each picked spool to its slot when the switch is on; a failure here is shown but the print stays closed. */
  async function keepAssignments() {
    if (!status?.configured) return;
    for (const [i, d] of decisions.entries()) {
      const wire = d.shortfall.wire_slot;
      if (!keep[i] || d.spoolId == null || wire == null) continue;
      const source = sources.find((s) => s.wire_slot === wire);
      if (!source || source.issues.includes("source_empty")) continue;
      try {
        await inventoryAssign(row.printer_id, {
          source: source.source,
          spool_id: d.spoolId,
          observed_identity: source.observed_identity,
        });
      } catch (e) {
        // The card is about to close; a toast outlives it.
        showToast(`Charged, but ${slotName(wire)} kept no spool: ${inventoryErrorText(e)}`, { severity: "warn" });
      }
    }
  }

  const ready = chargeable(decisions);
  const chargeLabel = (() => {
    const charges = chargesFor(decisions);
    if (!ready || charges.length !== 1) return charges.length > 1 ? `Charge ${charges.length} spools` : "Charge";
    const only = charges[0];
    const grams = "grams" in only ? only.grams : parseGrams(decisions.find((d) => d.spoolId === only.spool_id)!.gramsText) ?? 0;
    return `Charge ${grams} g to #${only.spool_id}`;
  })();

  const pickRow = (index: number, spool: InventorySpool) => (
    <Pressable
      key={spool.id}
      accessibilityRole="button"
      accessibilityLabel={`${spoolLabel(spool)}, ${gramsLeft(spool)}`}
      onPress={() => choose(index, spool)}
      style={{ paddingVertical: space.sm, borderTopWidth: 1, borderTopColor: c.borderSoft }}
    >
      <Text style={body} numberOfLines={1}>{spoolLabel(spool)}</Text>
      <Text style={small}>{gramsLeft(spool)}</Text>
    </Pressable>
  );

  return (
    <Surface padded style={{ gap: space.md, borderLeftWidth: 3, borderLeftColor: c.warn }}>
      <View style={{ gap: 2 }}>
        <Text style={[type.caption, { color: c.muted, textTransform: "uppercase" }]}>Needs you</Text>
        <Text style={[type.h2, { color: c.text }]} numberOfLines={2}>{row.title || "Print"}</Text>
        <Text style={small}>{[outcomeWord(row), whenText(row)].filter(Boolean).join(" ")}</Text>
      </View>
      <PartPicture captureId={row.capture_id} />
      {shortfalls.map((shortfall: Shortfall, index) => {
        const decision = decisions[index];
        const chosen = decision.spoolId != null ? known.get(decision.spoolId) : undefined;
        return (
          <View key={`${shortfall.kind}-${shortfall.wire_slot ?? "x"}-${shortfall.spool_id ?? "x"}-${index}`} style={{ gap: space.sm }}>
            <Text style={body}>{shortfallLine(shortfall)}</Text>
            <Text style={small}>{shortfallQuestion(shortfall)}</Text>
            {decision.spoolId != null ? (
              <Text style={body}>→ {chosen ? spoolLabel(chosen) : `#${decision.spoolId}`}</Text>
            ) : null}
            {picking !== index && (
              <Button
                label={decision.spoolId == null ? "Pick a spool" : "Pick a different spool"}
                variant="secondary"
                disabled={busy}
                onPress={() => void openPicker(index)}
              />
            )}
            {picking === index && (
              <View>
                {choices.map((spool) => pickRow(index, spool))}
                {!choices.length && <Text style={small}>No spools in Spoolman yet.</Text>}
                <Button label="Close" variant="secondary" onPress={() => setPicking(null)} />
              </View>
            )}
            {decision.spoolId != null && (
              <Field
                label="Grams"
                value={decision.gramsText}
                keyboardType="number-pad"
                onChangeText={(text) =>
                  setDecisions((prev) => prev.map((d, i) => (i === index ? { ...d, gramsText: gramsText(text) } : d)))
                }
                error={decision.gramsText === "" || parseGrams(decision.gramsText) == null ? "Whole grams, 1 to 10000. Use Skip to charge nothing." : null}
                hint={shortfall.kind === "unweighed_filament" ? "The file gave no weight; type what the scale says." : undefined}
              />
            )}
            {decision.spoolId != null && shortfall.kind === "unassigned_slot" && shortfall.wire_slot != null && (
              <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
                <Switch
                  value={!!keep[index]}
                  onValueChange={(value) => setKeep((prev) => ({ ...prev, [index]: value }))}
                  accessibilityLabel={`Keep #${decision.spoolId} in ${slotName(shortfall.wire_slot)}`}
                />
                <Text style={[...body, { flex: 1 }]}>Keep #{decision.spoolId} in {slotName(shortfall.wire_slot)}</Text>
              </View>
            )}
          </View>
        );
      })}
      {!!error && (
        <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>
      )}
      <Button label={chargeLabel} disabled={busy || !ready} loading={busy} onPress={() => void finish("charge")} />
      <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
        <Button label="Skip · charge nothing" variant="secondary" disabled={busy} onPress={() => void finish("nothing")} />
        <Button label="Charged by hand in Spoolman" variant="secondary" disabled={busy} onPress={() => void finish("charged_elsewhere")} />
      </View>
    </Surface>
  );
}
