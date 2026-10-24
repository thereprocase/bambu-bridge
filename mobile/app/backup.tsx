/**
 * Backup spool for one AMS slot: "When slot 1 runs out, continue from …".
 * Opened from a slot row on the Filament tab:
 *   router.push({ pathname: "/backup", params: { printer, from: "0" } })
 * One chain per printer in this beta; the sheet says so when another exists.
 * Picking a slot shows what the printer will be told, field by field, before
 * anything is written; "Set backup" writes it.
 */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ScrollView, Text, View } from "react-native";

import {
  deleteBackup,
  inventorySources,
  inventorySpools,
  inventoryStatus,
  previewBackup,
  putBackup,
  refillStatus,
  type RefillStatus,
} from "../src/api/inventory";
import { Button } from "../src/components/Button";
import { ChoiceTiles } from "../src/components/ChoiceTiles";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import { backupCandidates, backupSettling, backupStatus, chainInUse, currentBackup, slotWord } from "../src/lib/backup";
import { changeLines, previewSteps, ringAfter, stepHeading, type PreviewStep } from "../src/lib/backupPreview";
import { gramsLeft, inventoryErrorText, slotViews, spoolLabel, swatchFor, type SlotView } from "../src/lib/slotInventory";
import { useTheme } from "../src/theme/ThemeProvider";

function firstParam(v: string | string[] | undefined): string | undefined {
  const s = Array.isArray(v) ? v[0] : v;
  return s && s.length ? s : undefined;
}

export default function BackupScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const params = useLocalSearchParams();
  const printerId = firstParam(params.printer);
  const fromWire = Number(firstParam(params.from));

  const [views, setViews] = useState<SlotView[]>([]);
  const [refill, setRefill] = useState<RefillStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const sending = useRef(false);
  // The slot picked as backup but not yet set, and what setting it would write.
  const [chosen, setChosen] = useState<number | null>(null);
  const [preview, setPreview] = useState<PreviewStep[] | null>(null);
  const [digest, setDigest] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState("");
  const previewAsked = useRef(0);

  const load = useCallback(async () => {
    if (!printerId) return;
    const current = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const [status, sources, spools, refillNow] = await Promise.all([
        inventoryStatus(),
        inventorySources(printerId),
        inventorySpools(),
        refillStatus(printerId),
      ]);
      if (current !== generation.current) return;
      setViews(slotViews(printerId, sources, status, spools));
      setRefill(refillNow);
    } catch (e) {
      if (current === generation.current) setError(inventoryErrorText(e));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [printerId]);
  useEffect(() => {
    void load();
    return () => {
      generation.current++;
    };
  }, [load]);

  const from = views.find((v) => v.source.wire_slot === fromWire) ?? null;
  const backup = currentBackup(refill);
  const mine = backup && backup.from_wire === fromWire ? backup : null;
  const blocked = chainInUse(fromWire, backup);
  const candidates = from ? backupCandidates(from, views) : [];
  const body = [type.body, { color: c.text }];
  const small = [type.small, { color: c.muted }];

  // What the standing backup changed, from the bridge's own record of each slot.
  const applied = mine ? previewSteps({ steps: refill?.steps ?? [] }).filter((st) => st.changes.length) : [];

  async function choose(to: number) {
    if (!printerId || !from) return;
    const asked = ++previewAsked.current;
    setChosen(to);
    setPreview(null);
    setDigest(null);
    setPreviewError("");
    try {
      const answer = await previewBackup(printerId, { from_wire: fromWire, to_wire: to, from_color: from.source.color });
      if (asked === previewAsked.current) {
        setPreview(previewSteps(answer.body));
        setDigest(answer.digest);
      }
    } catch (e) {
      if (asked === previewAsked.current) setPreviewError(inventoryErrorText(e));
    }
  }

  /** The toast after Remove: a busy or offline printer gets it later, and the toast says so. */
  function removeWords(result: RefillStatus): string {
    const left = currentBackup(result);
    if (left && left.from_wire === fromWire && left.state === "needs_attention") {
      return `Removing the backup needs you: ${left.message ?? "the printer did not confirm both slots"}`;
    }
    if (left && left.from_wire === fromWire && left.state !== "active") {
      return `Removing the backup: ${slotWord(left.from_wire)} and ${slotWord(left.to_wire)} go back when the printer is idle`;
    }
    return `${slotWord(fromWire)} has no backup now`;
  }

  /** The toast after a set: the printer may still be confirming, so say which. */
  function setWords(result: RefillStatus, to: number): string {
    const now = currentBackup(result);
    // A chain the printer already runs gets the full sentence; the rest say what they wait on.
    const status = now && backupSettling(now) ? backupStatus(now, result) : null;
    return status
      ? `Backup set: ${slotWord(to)} · ${status}`
      : `Backup: ${slotWord(to)} takes over when ${slotWord(fromWire)} runs out`;
  }

  async function run(action: () => Promise<RefillStatus>, done: string | ((result: RefillStatus) => string)) {
    // A ref, not busy: two taps inside one render both see busy false.
    if (!printerId || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await action();
      setRefill(result);
      const needsYou = currentBackup(result)?.state === "needs_attention";
      showToast(typeof done === "string" ? done : done(result), { severity: needsYou ? "warn" : "success" });
      router.back();
    } catch (e) {
      // load() clears the error line when it starts, so the reason is set
      // after it; before, a refusal flashed and vanished.
      await load();
      // The slots may have changed since the preview (the bridge says so with
      // a 409); show what Set would write now, not what it would have.
      if (chosen != null) await choose(chosen);
      setError(inventoryErrorText(e));
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  if (!printerId || !Number.isInteger(fromWire)) {
    return (
      <View style={{ flex: 1, backgroundColor: c.bg, padding: space.lg, gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Backup spool</Text>
        <Text style={body}>Couldn't tell which slot this is for. Go back and tap the slot again.</Text>
        <Button label="Back" variant="secondary" onPress={() => router.back()} fullWidth />
      </View>
    );
  }

  return (
    <ScrollView style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
      <View style={{ gap: space.xs }}>
        <Text style={[type.h1, { color: c.text }]}>When {slotWord(fromWire)} runs out</Text>
        {from?.spool && <Text style={small}>{spoolLabel(from.spool)} · {gramsLeft(from.spool)}</Text>}
      </View>
      {!!error && <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>}
      {loading && !from && <Text style={small}>Loading slots…</Text>}

      {mine && (
        <Surface padded style={{ gap: space.sm }}>
          <Text style={body}>
            {mine.state === "needs_attention"
              ? `Backup to ${slotWord(mine.to_wire)} needs you`
              : `Continues from ${slotWord(mine.to_wire)}${backupStatus(mine, refill) ? ` · ${backupStatus(mine, refill)}` : ""}`}
          </Text>
          {mine.state === "needs_attention" && !!mine.message && <Text style={small}>{mine.message}</Text>}
          {applied.length > 0 && <Changes steps={applied} views={views} />}
          {applied.length > 0 && <Text style={small}>Removing the backup puts these settings back.</Text>}
          <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
            {mine.state === "needs_attention" && (
              <Button
                label="Try again"
                disabled={busy}
                loading={busy}
                onPress={() => void run(() => putBackup(printerId, { from_wire: fromWire, to_wire: mine.to_wire }), (r) => setWords(r, mine.to_wire))}
              />
            )}
            <Button
              label="Remove backup"
              variant="secondary"
              disabled={busy}
              onPress={() => void run(() => deleteBackup(printerId, fromWire), removeWords)}
            />
          </View>
        </Surface>
      )}

      {blocked && <Text style={body}>{blocked}</Text>}

      {from && !blocked && (
        <Surface padded style={{ gap: space.md }}>
          <Text style={small}>Continue from</Text>
          <ChoiceTiles
            label="Backup slot"
            value={chosen != null ? String(chosen) : mine ? String(mine.to_wire) : "none"}
            onSelect={(id) => {
              if (id === "none") {
                previewAsked.current++;
                setChosen(null);
                setPreview(null);
                setPreviewError("");
                if (mine) void run(() => deleteBackup(printerId, fromWire), removeWords);
                return;
              }
              const to = Number(id);
              if (mine && mine.to_wire === to) {
                previewAsked.current++;
                setChosen(null);
                return;
              }
              void choose(to);
            }}
            options={[
              { id: "none", topLabel: "No", label: "backup", accessibilityLabel: "No backup" },
              ...candidates.map((v) => ({
                id: String(v.source.wire_slot),
                topLabel: "Slot",
                label: String(v.source.wire_slot + 1),
                color: v.source.color ? `#${v.source.color.replace(/^#/, "").slice(0, 6)}` : undefined,
                accessibilityLabel: `Slot ${v.source.wire_slot + 1}`,
              })),
            ]}
          />
          {candidates.map((v) => (
            <Text key={v.source.wire_slot} style={small}>
              Slot {v.source.wire_slot + 1}: {v.spool ? `${spoolLabel(v.spool)} · ${gramsLeft(v.spool)}` : "no spool assigned · assign one so the refill gets charged"}
            </Text>
          ))}
          {chosen != null && mine && (
            <Text style={body}>Remove the {slotWord(mine.to_wire)} backup first, then pick {slotWord(chosen)}.</Text>
          )}
          {chosen != null && !mine && (
            <View style={{ gap: space.sm }}>
              <Text style={[type.h2, { color: c.text }]}>What the printer will be told</Text>
              {!preview && !previewError && <Text style={small}>Checking both slots…</Text>}
              {!!previewError && <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{previewError}</Text>}
              {preview && <Changes steps={preview} views={views} />}
              {preview && (
                <Text style={small}>
                  {"The printer only switches to a slot that declares the same filament. Spoolman keeps each slot's own spool; " +
                    "removing the backup puts every changed setting back."}
                </Text>
              )}
              <Button
                label={`Set ${slotWord(chosen)} as backup`}
                disabled={busy || !preview}
                loading={busy}
                onPress={() => void run(() => putBackup(printerId, { from_wire: fromWire, to_wire: chosen, ...(digest ? { preview_digest: digest } : {}) }), (r) => setWords(r, chosen))}
              />
            </View>
          )}
          {!candidates.length && (
            <Text style={small}>
              {from.source.tagged
                ? `No other ${from.source.material.trim()} slot without an RFID tag is loaded. Two tagged spools can't back each other up.`
                : `No other ${from.source.material.trim()} slot is loaded.`}
            </Text>
          )}
          <Text style={small}>The printer takes the change when it's idle. Both slots keep their own spools in Spoolman; the print is charged to whichever slot fed it.</Text>
        </Surface>
      )}

      <Button label="Done" variant="secondary" disabled={busy} onPress={() => router.back()} fullWidth />
    </ScrollView>
  );
}

/**
 * Each slot's changes as "before → after" lines. A changed colour gets the
 * Filament row's swatch as it will look: the spool's Spoolman colour as the
 * fill, the colour the printer will declare as the ring.
 */
function Changes({ steps, views }: { steps: PreviewStep[]; views: SlotView[] }) {
  const { c, space, type } = useTheme();
  return (
    <View style={{ gap: space.sm }}>
      {steps.map((step) => {
        const after = ringAfter(step);
        const swatch = after ? swatchFor(views.find((v) => v.source.wire_slot === step.wire) ?? null, after) : null;
        const lines = changeLines(step.changes);
        return (
          <View key={step.wire} style={{ gap: 2 }} accessible accessibilityLabel={[stepHeading(step), ...lines].join(". ")}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: space.xs }}>
              {swatch && (
                <View
                  style={{
                    width: 14, height: 14, borderRadius: 7,
                    backgroundColor: swatch.fill ?? c.surface3,
                    borderWidth: swatch.ring ? 3 : 1, borderColor: swatch.ring ?? c.border,
                  }}
                />
              )}
              <Text style={[type.body, { color: c.text }]}>{stepHeading(step)}</Text>
            </View>
            {lines.map((line) => (
              <Text key={line} style={[type.small, { color: c.muted }]}>{line}</Text>
            ))}
          </View>
        );
      })}
    </View>
  );
}
