/**
 * Filament — every slot, what spool is in it and how much is left.
 *
 * Slots come live from the printer snapshot (type, colour, fill). When the
 * bridge has Spoolman, each row also carries the assigned spool from the
 * bridge's assignment table and the spool's record, and tapping a row opens
 * the assign sheet (`/assign-spool`). Load/unload stays here as before.
 */

import { amsEnvironmentLabel } from "../../src/lib/amsEnvironment";
import { CapabilityCard, CapabilityList } from "../../src/components/CapabilityCard";
import { printerCapabilities } from "../../src/lib/capabilities";

import { useFocusEffect, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import { Ionicons } from "@expo/vector-icons";

import { amsChange, amsRfidRead, unloadFilament } from "../../src/api/control";
import type { PrinterSnapshot } from "../../src/api/types";
import { printerStateLabels } from "../../src/lib/printerState";
import { READ_POLL_MS, READ_TIMEOUT_MS, READING_TEXT, canRead, readOutcome, readableWires, sameReport, type Reading } from "../../src/lib/readTag";
import { BridgeError } from "../../src/api/errors";
import {
  inventoryAccounting,
  inventorySources,
  inventorySpools,
  inventoryStatus,
  refillStatus,
  type AccountingRow,
  type RefillStatus,
  type InventorySource,
  type InventorySpool,
  type InventoryStatus,
} from "../../src/api/inventory";
import { Button } from "../../src/components/Button";
import { NeedsYouCard } from "../../src/components/NeedsYouCard";
import { ProgressBar } from "../../src/components/ProgressBar";
import { RecentPrints } from "../../src/components/RecentPrints";
import { Surface } from "../../src/components/Surface";
import { useToastStore } from "../../src/components/Toast";
import {
  EXTERNAL_WIRE,
  gramsLeft,
  inventoryErrorText,
  materialLine,
  percentFor,
  slotViews,
  swatchFor,
  sourceColor,
  spoolLabel,
  staleLine,
  tagOriginNote,
  type SlotView,
} from "../../src/lib/slotInventory";
import { AmsSlot, viewOf } from "../../src/lib/snapshot";
import { useLiveStore } from "../../src/store/live";
import { usePrintersStore } from "../../src/store/printers";
import { useReviewStore } from "../../src/store/review";
import { BACKUP_POLL_MS, backupLine, backupSettling, canOfferBackup, currentBackup, slotWord } from "../../src/lib/backup";
import { useTheme } from "../../src/theme/ThemeProvider";

/**
 * Every slot row shares one grid: swatch · text column · chevron slot. A row is a
 * drawer: collapsed it shows the spool, its grams and bar and any status lines;
 * tapping it opens that slot's actions, stacked small underneath, one slot at a
 * time. The chevron slot keeps its width on rows without one, so swatches, text
 * and bars sit at the same x on every row of both cards.
 */
const SWATCH = 32;
const CHEVRON_W = 16;

export default function FilamentScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const setReviewCount = useReviewStore((s) => s.setCount);
  const selectedId = usePrintersStore((s) => s.selectedId);
  const live = useLiveStore((s) => (selectedId ? s.printers[selectedId] : undefined));
  const registeredModel = usePrintersStore((s) => s.list.find((p) => p.serial === selectedId)?.model);
  const capabilities = printerCapabilities(live?.snapshot, registeredModel);
  const view = viewOf((live?.snapshot as any) ?? null);

  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<InventoryStatus | null>(null);
  const [sources, setSources] = useState<InventorySource[]>([]);
  const [spools, setSpools] = useState<InventorySpool[]>([]);
  const [inventoryError, setInventoryError] = useState("");
  const [needsYou, setNeedsYou] = useState<AccountingRow[]>([]);
  const [recent, setRecent] = useState<AccountingRow[]>([]);
  const [refill, setRefill] = useState<RefillStatus | null>(null);
  // The one slot whose drawer is open (by wire slot); null when all are collapsed.
  const [openWire, setOpenWire] = useState<number | null>(null);
  // Manual RFID reads, by wire slot. Nothing starts on its own.
  const [readings, setReadings] = useState<Record<number, Reading | undefined>>({});
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const facts = printerStateLabels((live?.snapshot as unknown as PrinterSnapshot) ?? null, live?.status ?? "closed");
  const printerIdle = facts.live && facts.printerReady;
  // Why tags can't be read right now: the printer is busy, or it hasn't reported at all.
  const readTagHint = facts.live ? "Read tags when the printer is idle." : "Tags can be read once the printer reports again.";

  // Hold the last non-empty AMS slot view so an RFID re-scan (which transiently
  // empties `ams.slots` while the hardware stays attached, §6.1.1) renders the
  // previous slots dimmed instead of flashing empty.
  const [lastSlots, setLastSlots] = useState<typeof view.ams>([]);
  const [slotsPrinter, setSlotsPrinter] = useState<string | null>(null);
  // viewOf() builds a new ams array every render; key the effect on its content,
  // or each setLastSlots re-renders into another run ("Maximum update depth exceeded").
  const amsKey = JSON.stringify(view.ams);
  useEffect(() => {
    if (view.ams.length > 0) { setLastSlots(view.ams); setSlotsPrinter(selectedId); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, amsKey]);

  const rescanning = view.amsPresent && view.ams.length === 0;
  const slotsToRender = view.ams.length > 0 ? view.ams : rescanning && slotsPrinter === selectedId ? lastSlots : [];

  const inventoryOn = status?.configured === true && status.api_version === 2;

  // Which printer the latest refresh is for; a slow answer for the printer
  // before a switch must not land on the one now shown.
  const refreshFor = useRef(0);
  const refresh = useCallback(async () => {
    const asked = ++refreshFor.current;
    const current = () => mounted.current && asked === refreshFor.current;
    setLoading(true);
    try {
      const state = await inventoryStatus();
      if (!current()) return;
      setStatus(state);
      setReviewCount(state.review_count ?? 0);
      if (!(state.configured && state.api_version === 2) || !selectedId) {
        setSources([]);
        setSpools([]);
        return;
      }
      const [physical, records, waiting, latest, refillNow] = await Promise.all([
        inventorySources(selectedId),
        inventorySpools(),
        // Older bridges have no accounting page; the slots still show.
        inventoryAccounting(0, "review").catch(() => ({ attempts: [] })),
        inventoryAccounting(0, undefined, 10).catch(() => ({ attempts: [] })),
        // A bridge without the backup route answers without `backups`; the row then says nothing.
        refillStatus(selectedId).catch(() => null),
      ]);
      if (!current()) return;
      setRefill(refillNow);
      setSources(physical);
      setSpools(records);
      setNeedsYou((waiting.attempts ?? []).filter((r) => r.printer_id === selectedId));
      setRecent((latest.attempts ?? []).filter((r) => r.printer_id === selectedId));
      setInventoryError("");
    } catch (e) {
      if (current()) setInventoryError(inventoryErrorText(e));
    } finally {
      if (current()) setLoading(false);
    }
  }, [selectedId, setReviewCount]);

  useFocusEffect(useCallback(() => { void refresh(); }, [refresh]));

  // The printer confirms a new backup some seconds after the write that the
  // sheet waited for, so the focus refresh above usually still sees `pending`.
  // Ask again while the tab is open until the chain settles; both rows read
  // this one record. A failed poll keeps the last answer. One request at a
  // time, so a bridge slower than the interval is not stacked up, and none
  // while the app is in the background.
  const settling = backupSettling(currentBackup(refill));
  const refillAsked = useRef(0);
  const refillInFlight = useRef(false);
  useFocusEffect(useCallback(() => {
    if (!settling || !selectedId || !inventoryOn) return;
    const timer = setInterval(() => {
      // "unknown" (cold start on some devices) still asks; only a known background state skips.
      if (refillInFlight.current || AppState.currentState === "background" || AppState.currentState === "inactive") return;
      const asked = ++refillAsked.current;
      refillInFlight.current = true;
      refillStatus(selectedId)
        .then((next) => { if (mounted.current && asked === refillAsked.current) setRefill(next); })
        .catch(() => {})
        .finally(() => { if (asked === refillAsked.current) refillInFlight.current = false; });
    }, BACKUP_POLL_MS);
    return () => { clearInterval(timer); refillAsked.current++; refillInFlight.current = false; };
  }, [settling, selectedId, inventoryOn]));

  function safeCall(fn: () => Promise<unknown>) {
    fn()
      .then(() => showToast("Command sent", { severity: "success" }))
      .catch((e) => showToast(e instanceof BridgeError ? e.envelope.message : String(e), { severity: "danger" }));
  }

  const views = selectedId ? slotViews(selectedId, sources, status, spools) : [];
  const byWire = new Map(views.map((v) => [v.source.wire_slot, v]));
  const external = byWire.get(EXTERNAL_WIRE) ?? null;
  const backup = currentBackup(refill);

  function openBackup(slot: SlotView) {
    if (!selectedId) return;
    router.push({ pathname: "/backup", params: { printer: selectedId, from: String(slot.source.wire_slot) } });
  }

  /** Send one slot's read, then watch its report until it moves or the wait runs out. */
  async function readTag(wire: number) {
    if (!selectedId || !canRead(printerIdle, readings, wire)) return;
    const before = byWire.get(wire)?.source ?? null;
    const startedAt = Date.now();
    setReadings((r) => ({ ...r, [wire]: { wire, startedAt, before, state: "reading", text: READING_TEXT } }));
    const finish = (text: string, failed = false) => {
      if (mounted.current) setReadings((r) => ({ ...r, [wire]: { wire, startedAt, before, state: "done", text, failed } }));
    };
    try {
      await amsRfidRead(selectedId, Math.floor(wire / 4), wire % 4);
    } catch (e) {
      // The bridge's own words (e.g. "Fresh printer status required"), with Try again on the row.
      finish(e instanceof BridgeError ? e.envelope.message : String(e), true);
      return;
    }
    while (mounted.current) {
      await new Promise((resolve) => setTimeout(resolve, READ_POLL_MS));
      let after: InventorySource | null = null;
      try {
        after = (await inventorySources(selectedId)).find((s) => s.wire_slot === wire) ?? null;
      } catch {
        continue; // a missed poll is retried on the next tick
      }
      const liveName = viewOf((useLiveStore.getState().printers[selectedId]?.snapshot as any) ?? null).ams.find((s) => s.physicalSlot === wire + 1)?.name ?? null;
      const timedOut = Date.now() - startedAt >= READ_TIMEOUT_MS;
      if (!sameReport(before, after) || timedOut) {
        finish(readOutcome(wire, after, liveName, timedOut));
        void refresh();
        return;
      }
    }
  }

  /** One slot after another, never in parallel. */
  async function readAllTags() {
    for (const wire of readableWires(sources)) await readTag(wire);
  }
  const anyReading = Object.values(readings).some((r) => r?.state === "reading");

  function openAssign(slot: SlotView) {
    if (!selectedId) return;
    router.push({ pathname: "/assign-spool", params: { printer: selectedId, source: slot.source.source } });
  }

  /** Two text lines for a row: the spool when assigned, else the live material. */
  function lines(slot: SlotView | null, liveType: string | null, empty: boolean): { primary: string; secondary: string | null; chain: string | null } {
    const chain = slot ? backupLine(slot.source.wire_slot, backup, refill) : null;
    if (slot?.assignment) {
      const name = slot.spool ? spoolLabel(slot.spool) : `#${slot.assignment.spool_id}`;
      // The backup chain gets its own line; appended to the grams it was cut off.
      // The tag-origin note gets its own line; appended to the grams it was cut off.
      const fromTag = tagOriginNote(slot.assignment);
      return {
        primary: name,
        secondary: `${gramsLeft(slot.spool)} · ${materialLine(slot.source)}`,
        chain: [chain, fromTag ? `#${slot.assignment.spool_id} ${fromTag}` : null].filter(Boolean).join("\n") || null,
      };
    }
    if (empty) return { primary: "Empty", secondary: null, chain };
    const material = liveType ?? (slot ? materialLine(slot.source) : "Filament loaded");
    // A backup slot may run without a spool assigned; it still says what it backs up.
    return { primary: material, secondary: inventoryOn && slot ? "No spool · tap to assign" : null, chain };
  }

  function renderRow(opts: {
    key: string;
    title: string;
    color: string | null;
    /** The printer's tray colour when it differs from the spool's; drawn as a thin ring. */
    ring?: string | null;
    empty: boolean;
    liveType: string | null;
    slot: SlotView | null;
    remainingPercent: number | null | undefined;
    first: boolean;
    /** The slot's actions, shown stacked when the row's drawer is open. */
    actions?: React.ReactNode;
    /** The manual tag read's state for this row, when one ran. */
    note?: string | null;
    /** The read's send failed; offered when the printer is idle. */
    retry?: (() => void) | null;
  }) {
    const { primary, secondary, chain } = lines(opts.slot, opts.liveType, opts.empty);
    const stale = opts.slot ? staleLine(opts.slot.source) : null;
    // The AMS reads a new tag for a few seconds; the bridge seats the spool after that.
    const readingTag = !!opts.slot?.source.tag_reading && readings[opts.slot.source.wire_slot]?.state !== "reading";
    const tappable = inventoryOn && !!opts.slot && !rescanning && !opts.empty && !!opts.actions;
    const wire = opts.slot?.source.wire_slot ?? null;
    const open = tappable && wire != null && openWire === wire;
    return (
      <View
        key={opts.key}
        style={{
          borderTopWidth: opts.first ? 0 : 1,
          borderTopColor: c.borderSoft,
          paddingTop: opts.first ? 0 : space.md,
        }}
      >
      <Pressable
        accessibilityRole={tappable ? "button" : undefined}
        accessibilityLabel={tappable ? `${opts.title}, ${primary}, ${open ? "close" : "open"} actions` : undefined}
        accessibilityState={tappable ? { expanded: open } : undefined}
        onPress={tappable ? () => setOpenWire(open ? null : wire) : undefined}
        disabled={!tappable}
        style={{
          flexDirection: "row",
          alignItems: "flex-start",
          gap: space.md,
          opacity: rescanning || stale ? 0.4 : 1,
        }}
      >
        <View
          accessibilityLabel={opts.ring ? "spool colour from Spoolman; the ring is what the printer reports" : undefined}
          style={{
            width: SWATCH, height: SWATCH, borderRadius: SWATCH / 2,
            // Centred on the caption + primary pair, whatever lines follow them.
            marginTop: 2,
            backgroundColor: opts.color ?? c.surface3,
            borderWidth: opts.ring ? 3 : 1, borderColor: opts.ring ?? c.border,
          }}
        />
        <View style={{ flex: 1, gap: 2 }}>
          <Text style={[type.caption, { color: c.muted, textTransform: "uppercase" }]}>{opts.title}</Text>
          <Text style={[type.body, { color: c.text }]} numberOfLines={1}>{primary}</Text>
          {secondary && (
            <Text style={[type.small, { color: c.muted }]} numberOfLines={1}>{secondary}</Text>
          )}
          {chain && (
            <Text style={[type.small, { color: c.text }]} numberOfLines={2}>{chain}</Text>
          )}
          {opts.note && (
            <Text style={[type.small, { color: c.text }]} numberOfLines={2}>{opts.note}</Text>
          )}
          {opts.retry && (
            <Button label="Try again" variant="secondary" size="small" disabled={!printerIdle} onPress={opts.retry} style={{ marginTop: space.xs }} />
          )}
          {stale && <Text style={[type.small, { color: c.muted }]}>{stale}</Text>}
          {readingTag && <Text style={[type.small, { color: c.muted }]}>reading tag…</Text>}
          {opts.remainingPercent != null && (
            <ProgressBar value={opts.remainingPercent} tone={opts.remainingPercent < 15 ? "warn" : "accent"} />
          )}
        </View>
        <View style={{ width: CHEVRON_W, alignItems: "center", paddingTop: space.sm }}>
          {tappable && <Ionicons name={open ? "chevron-up" : "chevron-down"} size={CHEVRON_W} color={c.muted} />}
        </View>
      </Pressable>
      {open && (
        <View style={{ marginLeft: SWATCH + space.md, marginTop: space.sm, gap: space.xs, alignItems: "flex-start" }}>
          {opts.actions}
        </View>
      )}
      </View>
    );
  }

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={<RefreshControl refreshing={loading} tintColor={c.accent} onRefresh={refresh} />}
    >
      {selectedId && inventoryOn && needsYou.map((row) => (
        <NeedsYouCard
          key={row.attempt_id}
          row={row}
          status={status}
          sources={sources}
          onResolved={(id) => {
            setNeedsYou((rows) => {
              const left = rows.filter((r) => r.attempt_id !== id);
              setReviewCount(left.length);
              return left;
            });
            void refresh();
          }}
        />
      ))}
      {selectedId && inventoryOn && backup?.state === "needs_attention" && (
        <Surface padded style={{ gap: space.sm, borderLeftWidth: 3, borderLeftColor: c.warn }}>
          <Text style={[type.caption, { color: c.muted, textTransform: "uppercase" }]}>Needs you</Text>
          <Text style={[type.h2, { color: c.text }]}>Backup for {slotWord(backup.from_wire)} didn&apos;t take</Text>
          <Text style={[type.body, { color: c.text }]}>{backup.message ?? "The printer reports a different setting than the backup asked for."}</Text>
          <Button label="Open backup" variant="secondary" onPress={() => { const v = byWire.get(backup.from_wire); if (v) openBackup(v); }} />
        </Surface>
      )}
      <CapabilityList>
      {selectedId && (
        <CapabilityCard title="AMS" availability={capabilities.get("ams")}>
        <Surface padded style={{ gap: space.md }}>
          <Text style={[type.h2, { color: c.text }]}>AMS</Text>
          {view.amsUnits.map((unit, i) => (
            <Text key={unit.id} style={[type.small, { color: c.muted }]}>
              {amsEnvironmentLabel(unit, i, live?.status !== "open")}
            </Text>
          ))}
          {!!inventoryError && (
            <Text accessibilityRole="alert" style={[type.small, { color: c.danger }]}>
              {inventoryError}
            </Text>
          )}
          {inventoryOn && view.amsPresent && !rescanning && (
            <View style={{ flexDirection: "row", alignItems: "center", gap: space.md, flexWrap: "wrap" }}>
              <Button
                label={anyReading ? "Reading tags…" : "Read all tags"}
                variant="secondary"
                size="small"
                disabled={!printerIdle || anyReading || !readableWires(sources).length}
                onPress={() => void readAllTags()}
              />
              {!printerIdle && (
                <Text style={[type.small, { color: c.muted, flex: 1 }]}>{readTagHint}</Text>
              )}
            </View>
          )}
          {!view.amsPresent ? (
            <Text style={[type.small, { color: c.muted }]}>
              No AMS detected on this printer.
            </Text>
          ) : (
            <>
              {rescanning && (
                <Text style={[type.small, { color: c.muted }]}>
                  AMS re-scanning — slot data returns in a few seconds.
                </Text>
              )}
              {slotsToRender.map((slot: AmsSlot, index) =>
                renderRow({
                  key: `ams-${slot.physicalSlot}`,
                  title: `Slot ${slot.physicalSlot}`,
                  ...(() => {
                    const view = byWire.get(slot.physicalSlot - 1) ?? null;
                    const swatch = swatchFor(view, slot.color);
                    return { color: swatch.fill, ring: swatch.ring, remainingPercent: percentFor(view, slot.remainingPercent) };
                  })(),
                  empty: slot.empty,
                  liveType: !slot.empty ? (slot.name ?? null) : null,
                  slot: byWire.get(slot.physicalSlot - 1) ?? null,
                  first: index === 0,
                  note: readings[slot.physicalSlot - 1]?.text ?? null,
                  retry: readings[slot.physicalSlot - 1]?.failed ? () => void readTag(slot.physicalSlot - 1) : null,
                  actions: (() => {
                    const slotView = byWire.get(slot.physicalSlot - 1) ?? null;
                    const wire = slot.physicalSlot - 1;
                    if (!inventoryOn || rescanning || !slotView || slot.empty) return undefined;
                    const offerBackup = canOfferBackup(slotView, views) || backup?.from_wire === slotView.source.wire_slot;
                    const load = capabilities.get("filamentMotion").available;
                    const readable = canRead(printerIdle, readings, wire);
                    return (
                      <>
                        <Button
                          label={readings[wire]?.state === "reading" ? "Reading…" : "Read tag"}
                          variant="secondary"
                          size="small"
                          disabled={!readable}
                          onPress={() => void readTag(wire)}
                        />
                        {!printerIdle && <Text style={[type.small, { color: c.muted }]}>{readTagHint}</Text>}
                        <Button
                          label={slotView.assignment ? "Change spool" : "Assign a spool"}
                          variant="secondary"
                          size="small"
                          onPress={() => openAssign(slotView)}
                        />
                        {slotView.assignment && (
                          <Button
                            label={`Weigh or archive #${slotView.assignment.spool_id}`}
                            variant="secondary"
                            size="small"
                            onPress={() => router.push({ pathname: "/spool", params: { id: String(slotView.assignment!.spool_id), printer: selectedId, source: slotView.source.source } })}
                          />
                        )}
                        {load && (
                          <Button
                            label="Load"
                            variant="secondary"
                            size="small"
                            onPress={() => safeCall(() => amsChange(selectedId, { target_tray: slot.physicalSlot - 1 }))}
                          />
                        )}
                        {offerBackup && (
                          <Button
                            label={backup?.from_wire === slotView.source.wire_slot ? "Backup" : "Backup ›"}
                            variant="secondary"
                            size="small"
                            onPress={() => openBackup(slotView)}
                          />
                        )}
                      </>
                    );
                  })(),
                }),
              )}
            </>
          )}
          {capabilities.get("filamentMotion").available && !rescanning && slotsToRender.some((s) => !s.empty) && (
            <Button
              label="Unload current filament"
              variant="secondary"
              onPress={() => safeCall(() => unloadFilament(selectedId))}
            />
          )}
        </Surface>
        </CapabilityCard>
      )}

      {selectedId && inventoryOn && external && (
        <Surface padded style={{ gap: space.md }}>
          <Text style={[type.h2, { color: c.text }]}>External spool</Text>
          {renderRow({
            key: "external",
            title: "Holder",
            color: swatchFor(external, sourceColor(external.source)).fill,
            ring: swatchFor(external, sourceColor(external.source)).ring,
            empty: external.source.issues.includes("source_empty"),
            liveType: null,
            slot: external,
            remainingPercent: percentFor(external, null),
            first: true,
            actions: external.source.issues.includes("source_empty") ? undefined : (
              <>
                <Button
                  label={external.assignment ? "Change spool" : "Assign a spool"}
                  variant="secondary"
                  size="small"
                  onPress={() => openAssign(external)}
                />
                {external.assignment && (
                  <Button
                    label={`Weigh or archive #${external.assignment.spool_id}`}
                    variant="secondary"
                    size="small"
                    onPress={() => router.push({ pathname: "/spool", params: { id: String(external.assignment!.spool_id), printer: selectedId, source: external.source.source } })}
                  />
                )}
              </>
            ),
          })}
        </Surface>
      )}

      <CapabilityCard title="Load / unload filament" availability={capabilities.get("filamentMotion")}>{null}</CapabilityCard>
      </CapabilityList>
      {selectedId && inventoryOn && (
        <RecentPrints
          rows={recent}
          onWrongSpool={(row, charge) =>
            router.push({
              pathname: "/wrong-spool",
              params: { attempt: row.attempt_id, from: String(charge.spool_id), grams: String(charge.grams), title: row.title || "Print" },
            })
          }
        />
      )}
    </ScrollView>
  );
}
