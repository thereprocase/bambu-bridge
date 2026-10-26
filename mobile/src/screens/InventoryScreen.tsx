/**
 * Inventory — every Spoolman spool in one place. Spoolman is the only record;
 * the bridge's assignment table says which spool sits in which slot. The tab
 * reads both and keeps nothing.
 *
 * Top to bottom: a summary (count, grams by material, spools running low),
 * the find bar (search, material and vendor chips, where it is, sort), the
 * list (one SpoolRow per active spool; a stored spool's drawer offers the
 * slots it can go into, every drawer opens the spool's sheet), and the
 * archived spools folded away at the bottom.
 */

import { Ionicons } from "@expo/vector-icons";
import { useFocusEffect, useRouter } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import {
  inventoryAssign,
  inventorySources,
  inventorySpools,
  inventoryStatus,
  type InventorySource,
  type InventorySpool,
  type InventoryStatus,
} from "../api/inventory";
import { Button } from "../components/Button";
import { Field } from "../components/Field";
import { FilterChips } from "../components/FilterChips";
import { SpoolRow } from "../components/SpoolRow";
import { Surface } from "../components/Surface";
import { useToastStore } from "../components/Toast";
import {
  dateText,
  fillText,
  filterSpools,
  inventorySummary,
  materialOf,
  materialOptions,
  NO_FILTER,
  placeOf,
  sectionsFor,
  sortSpools,
  vendorOptions,
  weightText,
  type InventoryFilter,
  type SortKey,
  type Where,
} from "../lib/inventoryView";
import { everySpool, slotsFor, type SlotOffer } from "../lib/shelf";
import { inventoryErrorText, slotTitle, slotViews, spoolLabel, swatchFor, sourceColor } from "../lib/slotInventory";
import { usePrintersStore } from "../store/printers";
import { useTheme } from "../theme/ThemeProvider";

const SORTS: { id: SortKey; label: string }[] = [
  { id: "material", label: "Material" },
  { id: "colour", label: "Colour" },
  { id: "left", label: "Left" },
  { id: "recent", label: "Recent" },
];
const WHERE: { id: Where; label: string }[] = [
  { id: "printer", label: "In the printer" },
  { id: "shelf", label: "Off the AMS" },
];

/** "SLOT 2", "EXT", "ON P2" — the row's badge for where the spool is. */
export function badgeText(place: string | null): string | null {
  if (!place) return null;
  if (place === "External spool") return "EXT";
  return place.toUpperCase();
}

export default function InventoryScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const selectedId = usePrintersStore((s) => s.selectedId);

  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<InventoryStatus | null>(null);
  const [sources, setSources] = useState<InventorySource[]>([]);
  const [spools, setSpools] = useState<InventorySpool[]>([]);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<InventoryFilter>(NO_FILTER);
  const [sort, setSort] = useState<SortKey>("material");
  const [archivedOpen, setArchivedOpen] = useState(false);
  // The one spool whose drawer is open; null when all are collapsed.
  const [openId, setOpenId] = useState<number | null>(null);
  // The spool whose "Into slot" is in flight, so a second tap waits.
  const [assigningId, setAssigningId] = useState<number | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const inventoryOn = status?.configured === true && status.api_version === 2;

  const refreshFor = useRef(0);
  const refresh = useCallback(async () => {
    const asked = ++refreshFor.current;
    const current = () => mounted.current && asked === refreshFor.current;
    setLoading(true);
    try {
      const state = await inventoryStatus();
      if (!current()) return;
      setStatus(state);
      if (!(state.configured && state.api_version === 2)) {
        setSources([]);
        setSpools([]);
        setError("");
        return;
      }
      const [physical, records] = await Promise.all([
        // Slots are for the badges and the into-slot offers; a printer that doesn't answer leaves them out.
        selectedId ? inventorySources(selectedId).catch(() => []) : Promise.resolve([]),
        // Archived spools included: the bridge lists them after the active ones.
        everySpool((offset, limit) => inventorySpools("", offset, undefined, limit, true)),
      ]);
      if (!current()) return;
      setSources(physical);
      setSpools(records);
      setError("");
    } catch (e) {
      if (current()) setError(inventoryErrorText(e));
    } finally {
      if (current()) setLoading(false);
    }
  }, [selectedId]);
  useFocusEffect(useCallback(() => { void refresh(); }, [refresh]));

  const views = useMemo(() => (selectedId ? slotViews(selectedId, sources, status, spools) : []), [selectedId, sources, status, spools]);
  const place = useCallback((spool: InventorySpool) => placeOf(spool, status, views, selectedId), [status, views, selectedId]);

  const active = useMemo(() => spools.filter((s) => !s.archived), [spools]);
  const archived = useMemo(() => spools.filter((s) => s.archived), [spools]);
  const summary = useMemo(() => inventorySummary(active), [active]);
  const shown = useMemo(() => sortSpools(filterSpools(active, filter, place), sort), [active, filter, place, sort]);
  const sections = useMemo(() => sectionsFor(shown, sort), [shown, sort]);
  const archivedShown = useMemo(() => sortSpools(filterSpools(archived, { ...filter, where: "all" }, place), sort), [archived, filter, place, sort]);
  const narrowed = filter.query.trim() !== "" || filter.material != null || filter.vendor != null || filter.where !== "all";

  async function putInSlot(spool: InventorySpool, offer: SlotOffer) {
    if (!selectedId || assigningId != null) return;
    const source = offer.view.source;
    setAssigningId(spool.id);
    try {
      await inventoryAssign(selectedId, {
        source: source.source,
        spool_id: spool.id,
        observed_identity: source.observed_identity,
        ...(offer.setSlot ? { set_slot: true } : {}),
      });
      if (!mounted.current) return;
      setOpenId(null);
      showToast(`${spoolLabel(spool)} in ${slotTitle(source).toLowerCase()}`, { severity: "success" });
      void refresh();
    } catch (e) {
      showToast(inventoryErrorText(e), { severity: "danger" });
      void refresh();
    } finally {
      if (mounted.current) setAssigningId(null);
    }
  }

  function offerLabel(offer: SlotOffer): string {
    const where = `Into ${slotTitle(offer.view.source).toLowerCase()}`;
    if (offer.view.assignment) return `${where} · replaces #${offer.view.assignment.spool_id}`;
    const material = offer.view.source.material.trim();
    return offer.setSlot && material ? `${where} · sets it from ${material.toUpperCase()}` : `${where} · no spool there yet`;
  }

  function openSheet(spool: InventorySpool) {
    const seat = views.find((v) => v.assignment?.spool_id === spool.id);
    router.push({
      pathname: "/spool",
      params: seat && selectedId ? { id: String(spool.id), printer: selectedId, source: seat.source.source } : { id: String(spool.id) },
    });
  }

  /** The second line of a row: filament left of full, the material unless the list is already by material, the lot. */
  function detailLine(spool: InventorySpool): string {
    return [fillText(spool), sort !== "material" && spool.filament.material ? materialOf(spool) : null, spool.lot_nr ? `lot ${spool.lot_nr}` : null]
      .filter(Boolean)
      .join(" · ");
  }

  function renderRow(spool: InventorySpool, index: number) {
    const here = place(spool);
    const seat = views.find((v) => v.assignment?.spool_id === spool.id) ?? null;
    const ring = seat ? swatchFor(seat, sourceColor(seat.source)).ring : null;
    const open = openId === spool.id;
    const offers = open && !here ? slotsFor(spool, views) : [];
    return (
      <SpoolRow
        key={spool.id}
        spool={spool}
        detail={spool.archived ? `archived${spool.last_used ? ` · last used ${dateText(spool.last_used)}` : ""}` : detailLine(spool)}
        ring={ring}
        badge={badgeText(here)}
        swatchSize={40}
        expandable
        open={open}
        onPress={() => setOpenId(open ? null : spool.id)}
        style={{ borderTopWidth: index ? 1 : 0, borderTopColor: c.borderSoft, paddingTop: index ? space.md : 0 }}
      >
        {offers.map((offer) => (
          <Button
            key={offer.view.source.source}
            label={offerLabel(offer)}
            variant="secondary"
            size="small"
            disabled={assigningId != null}
            loading={assigningId === spool.id}
            onPress={() => void putInSlot(spool, offer)}
          />
        ))}
        {!here && !spool.archived && offers.length === 0 && (
          <Text style={[type.small, { color: c.muted }]}>
            {selectedId ? "No slot can take it right now. Put it in a slot; that row then offers to assign it." : "Pick a printer to put it in a slot."}
          </Text>
        )}
        <Button
          label={spool.archived ? "Details" : "Details · weigh · archive"}
          variant="secondary"
          size="small"
          disabled={assigningId != null}
          onPress={() => openSheet(spool)}
        />
      </SpoolRow>
    );
  }

  const caption = [type.caption, { color: c.muted, textTransform: "uppercase" as const }];
  // The summary bar's segments, largest material first; past five they share the last tone.
  const tones = [c.accent, c.warn, c.text, c.accentDim, c.muted];
  const tone = (i: number) => ({ backgroundColor: tones[Math.min(i, tones.length - 1)], opacity: i === 2 ? 0.7 : 1 });

  // Spoolman off, or the bridge couldn't be asked: one card, never a blank tab.
  if (status && !inventoryOn) {
    return (
      <ScrollView style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }} refreshControl={<RefreshControl refreshing={loading} tintColor={c.accent} onRefresh={refresh} />}>
        <Surface padded style={{ gap: space.md }}>
          <Text style={[type.h2, { color: c.text }]}>Inventory</Text>
          <Text style={[type.body, { color: c.text }]}>
            {status.configured ? "This bridge speaks an older inventory API than the app. Update the bridge." : "Spoolman isn't set up on this bridge. Point the bridge at Spoolman in its settings; spools then show here."}
          </Text>
          <Button label="Settings" variant="secondary" onPress={() => router.push("/settings")} />
        </Surface>
      </ScrollView>
    );
  }

  return (
    <ScrollView
      keyboardShouldPersistTaps="handled"
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={<RefreshControl refreshing={loading} tintColor={c.accent} onRefresh={refresh} />}
    >
      {!!error && (
        <Surface padded style={{ gap: space.md, borderLeftWidth: 3, borderLeftColor: c.danger }}>
          <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>
          <View style={{ flexDirection: "row", gap: space.sm }}>
            <Button label="Try again" variant="secondary" size="small" onPress={() => void refresh()} />
            <Button label="Settings" variant="secondary" size="small" onPress={() => router.push("/settings")} />
          </View>
        </Surface>
      )}

      {/* Summary */}
      {status && (
        <Surface padded style={{ gap: space.md }}>
          <View style={{ flexDirection: "row", alignItems: "baseline", gap: space.sm }}>
            <Text style={[type.display, { color: c.text }]}>{summary.count}</Text>
            <Text style={[type.h2, { color: c.text }]}>{summary.count === 1 ? "spool" : "spools"}</Text>
            <Text style={[type.body, { color: c.muted, flex: 1, textAlign: "right" }]}>
              {summary.count === 0 ? "" : `${weightText(summary.grams)} left${summary.unweighed ? ` + ${summary.unweighed} unweighed` : ""}`}
            </Text>
          </View>
          {summary.byMaterial.length > 0 && (
            <>
              <View accessibilityLabel={`Filament by material: ${summary.byMaterial.map((m) => `${m.material} ${weightText(m.grams)}`).join(", ")}`} style={{ flexDirection: "row", height: 10, gap: 2 }}>
                {summary.byMaterial.map((m, i) => (
                  <View key={m.material} style={{ flex: Math.max(m.share, 0.02), ...tone(i) }} />
                ))}
              </View>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: space.sm }}>
                {summary.byMaterial.map((m, i) => {
                  const on = filter.material === m.material;
                  return (
                    <Pressable
                      key={m.material}
                      accessibilityRole="button"
                      accessibilityLabel={`${m.material}, ${weightText(m.grams)}, ${on ? "show all materials" : "show only this material"}`}
                      onPress={() => setFilter((f) => ({ ...f, material: on ? null : m.material }))}
                      style={{ flexDirection: "row", alignItems: "center", gap: 6, paddingVertical: 2 }}
                    >
                      <View style={{ width: 10, height: 10, ...tone(i) }} />
                      <Text style={[type.small, { color: c.text, textDecorationLine: on ? "underline" : "none" }]}>{m.material}</Text>
                      <Text style={[type.small, { color: c.muted }]}>{weightText(m.grams)}</Text>
                    </Pressable>
                  );
                })}
              </View>
            </>
          )}
          {summary.low.length > 0 && (
            <Text style={[type.small, { color: c.warn }]}>
              {`Running low: ${summary.low.map((s) => `#${s.id} ${s.filament.name ?? materialOf(s)} ${Math.round(s.remaining_weight ?? 0)} g`).join(", ")}`}
            </Text>
          )}
          {status && summary.count === 0 && !error && !loading && (
            <Text style={[type.small, { color: c.muted }]}>No spools in Spoolman yet. Add one from a slot on the Filament tab.</Text>
          )}
        </Surface>
      )}

      {/* Find bar */}
      {active.length > 0 && (
        <Surface padded style={{ gap: space.md }}>
          <Field label="Find" value={filter.query} onChangeText={(query) => setFilter((f) => ({ ...f, query }))} placeholder="Number, name, vendor, colour, lot" />
          <FilterChips label="Material" chips={materialOptions(active)} value={filter.material} onSelect={(material) => setFilter((f) => ({ ...f, material }))} />
          <FilterChips label="Vendor" chips={vendorOptions(active)} value={filter.vendor} onSelect={(vendor) => setFilter((f) => ({ ...f, vendor }))} />
          <FilterChips label="Where" chips={WHERE} value={filter.where === "all" ? null : filter.where} onSelect={(where) => setFilter((f) => ({ ...f, where: (where as Where | null) ?? "all" }))} />
          <View style={{ flexDirection: "row", alignItems: "center", gap: space.md }}>
            <Text style={caption}>Sort</Text>
            <FilterChips label="Sort" chips={SORTS} value={sort} onSelect={(id) => setSort((id as SortKey | null) ?? "material")} allLabel={null} />
          </View>
        </Surface>
      )}

      {/* The list */}
      {active.length > 0 && (
        <Surface padded style={{ gap: space.md }}>
          <View style={{ flexDirection: "row", alignItems: "baseline", justifyContent: "space-between" }}>
            <Text style={[type.h2, { color: c.text }]}>{narrowed ? `${shown.length} of ${active.length} spools` : "All spools"}</Text>
            {narrowed && (
              <Pressable accessibilityRole="button" onPress={() => setFilter(NO_FILTER)}>
                <Text style={[type.small, { color: c.accent }]}>Clear</Text>
              </Pressable>
            )}
          </View>
          {shown.length === 0 && <Text style={[type.small, { color: c.muted }]}>No spool matches. Clear the filters or widen the search.</Text>}
          {sections.map((section) => (
            <View key={section.title ?? "all"} style={{ gap: space.md }}>
              {section.title && <Text style={caption}>{`${section.title} · ${section.spools.length}`}</Text>}
              {section.spools.map((spool, index) => renderRow(spool, index))}
            </View>
          ))}
        </Surface>
      )}

      {/* Archived */}
      {archived.length > 0 && (
        <Surface padded style={{ gap: space.md }}>
          <Pressable
            accessibilityRole="button"
            accessibilityState={{ expanded: archivedOpen }}
            accessibilityLabel={`Archived, ${archived.length}, ${archivedOpen ? "hide" : "show"}`}
            onPress={() => setArchivedOpen((o) => !o)}
            style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}
          >
            <Text style={[type.h2, { color: c.text, flex: 1 }]}>{`Archived · ${archived.length}`}</Text>
            <Ionicons name={archivedOpen ? "chevron-up" : "chevron-down"} size={16} color={c.muted} />
          </Pressable>
          {archivedOpen && (
            <>
              <Text style={[type.small, { color: c.muted }]}>Finished spools keep their history in Spoolman. Unarchive one there to use it again.</Text>
              {archivedShown.length === 0 && <Text style={[type.small, { color: c.muted }]}>No archived spool matches.</Text>}
              {archivedShown.map((spool, index) => renderRow(spool, index))}
            </>
          )}
        </Surface>
      )}
    </ScrollView>
  );
}
