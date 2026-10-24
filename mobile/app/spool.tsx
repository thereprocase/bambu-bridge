/**
 * One Spoolman spool, through the bridge's thin shim: weigh it, archive it,
 * or add a new one (vendor and product inline). Opened from the assign
 * sheet or a slot row:
 *   /spool?id=5                        an existing spool
 *   /spool?new=1&printer=…&source=…    a new spool, assigned to that source once created
 * Anything else about the record is a job for Spoolman itself: "Open in Spoolman".
 */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Linking, Pressable, ScrollView, Text, View } from "react-native";

import {
  archiveSpool,
  createProduct,
  createSpool,
  createVendor,
  inventoryAssign,
  inventoryCatalog,
  inventoryProducts,
  inventorySources,
  inventorySpool,
  inventoryStatus,
  inventoryVendors,
  setVendorEmptyWeight,
  spoolHistory,
  weighSpool,
  type CatalogEntry,
  type InventoryProduct,
  type SpoolPrint,
  type InventorySpool,
  type InventoryStatus,
  type InventoryVendor,
} from "../src/api/inventory";
import { BridgeError } from "../src/api/errors";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { PartPicture } from "../src/components/PartPicture";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import { historyLine, whenText } from "../src/lib/heldPrints";
import { gramsLeft, inventoryErrorText, spoolLabel } from "../src/lib/slotInventory";
import { useTheme } from "../src/theme/ThemeProvider";

function firstParam(v: string | string[] | undefined): string | undefined {
  const s = Array.isArray(v) ? v[0] : v;
  return s && s.length ? s : undefined;
}

/** Typical densities by chemistry, so the product form starts with a sane figure. */
const DENSITY: Record<string, number> = { PLA: 1.24, PETG: 1.27, ABS: 1.04, ASA: 1.07, TPU: 1.21, PA: 1.14, PC: 1.2, PVA: 1.23 };

/** Whole grams: the owner wants nothing finer. */
function positive(text: string): number | null {
  const value = Number(text.replace(",", "."));
  return text.trim() !== "" && Number.isFinite(value) && value >= 0 ? Math.round(value) : null;
}
/** Density keeps its decimals; it is a material constant, never a reading. */
function decimal(text: string): number | null {
  const value = Number(text.replace(",", "."));
  return text.trim() !== "" && Number.isFinite(value) && value > 0 ? Math.round(value * 1000) / 1000 : null;
}

export default function SpoolScreen() {
  const { c, space, type } = useTheme();
  const router = useRouter();
  const showToast = useToastStore((s) => s.show);
  const params = useLocalSearchParams();
  const spoolId = Number(firstParam(params.id));
  const creating = firstParam(params.new) === "1" || !Number.isInteger(spoolId);
  const printerId = firstParam(params.printer);
  const sourceId = firstParam(params.source);

  const [status, setStatus] = useState<InventoryStatus | null>(null);
  const [spool, setSpool] = useState<InventorySpool | null>(null);
  const [history, setHistory] = useState<SpoolPrint[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);

  // Weigh
  const [weight, setWeight] = useState("");
  const [gross, setGross] = useState(true);
  // Archive: the bare spool on the scale, once it is empty.
  const [archiving, setArchiving] = useState(false);
  const [bareWeight, setBareWeight] = useState("");

  // New spool
  const [vendorQuery, setVendorQuery] = useState("");
  const [vendors, setVendors] = useState<InventoryVendor[]>([]);
  const [vendor, setVendor] = useState<InventoryVendor | null>(null);
  const [productQuery, setProductQuery] = useState("");
  const [products, setProducts] = useState<InventoryProduct[] | null>(null);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [product, setProduct] = useState<InventoryProduct | null>(null);
  const [vendorName, setVendorName] = useState("");
  const [diameter, setDiameter] = useState("1.75");
  const [externalId, setExternalId] = useState<string | null>(null);
  const [newProduct, setNewProduct] = useState(false);
  const [productName, setProductName] = useState("");
  const [material, setMaterial] = useState("");
  const [color, setColor] = useState("");
  const [density, setDensity] = useState("");
  const [netWeight, setNetWeight] = useState("1000");
  const [emptyWeight, setEmptyWeight] = useState("");
  const [remaining, setRemaining] = useState("");

  const load = useCallback(async () => {
    const current = ++generation.current;
    setError("");
    try {
      const [state, record, prints] = await Promise.all([
        inventoryStatus(),
        creating ? Promise.resolve(null) : inventorySpool(spoolId),
        // An older bridge has no history route; the section simply stays away.
        creating ? Promise.resolve(null) : spoolHistory(spoolId).catch(() => null),
      ]);
      if (current !== generation.current) return;
      setStatus(state);
      setSpool(record);
      setHistory(prints);
    } catch (e) {
      if (current === generation.current) setError(inventoryErrorText(e));
    }
  }, [creating, spoolId]);
  useEffect(() => {
    void load();
    return () => {
      generation.current++;
    };
  }, [load]);

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(inventoryErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  /** Spoolman's catalog fills the new-product form; the vendor is matched or created on save. */
  function useCatalogEntry(entry: CatalogEntry) {
    setProduct(null);
    setNewProduct(true);
    setVendor(null);
    setVendorName(entry.manufacturer ?? "");
    setVendorQuery(entry.manufacturer ?? "");
    setProductName((entry.name ?? "").replace(/[™®©]/g, "").trim());
    setMaterial((entry.material ?? "").toUpperCase());
    setColor((entry.color_hex ?? "").replace(/^#/, "").toUpperCase());
    setDensity(entry.density != null ? String(entry.density) : "");
    setNetWeight(entry.weight != null ? String(Math.round(entry.weight)) : "1000");
    setEmptyWeight(entry.spool_weight != null ? String(Math.round(entry.spool_weight)) : "");
    setDiameter(entry.diameter != null ? String(entry.diameter) : "1.75");
    setExternalId(entry.id);
  }

  /** The vendor for a new product: the one picked, else the named one found or created. */
  async function vendorForProduct(): Promise<number> {
    if (vendor) return vendor.id;
    const name = vendorName.trim();
    const found = (await inventoryVendors(name)).find((v) => v.name.trim().toLowerCase() === name.toLowerCase());
    if (found) return found.id;
    return (await createVendor({ name })).id;
  }

  async function assignTo(created: InventorySpool) {
    if (!printerId || !sourceId) return;
    const source = (await inventorySources(printerId)).find((s) => s.source === sourceId);
    if (!source) return;
    await inventoryAssign(printerId, {
      source: source.source,
      spool_id: created.id,
      observed_identity: source.observed_identity,
      ...((source.material.trim().toUpperCase() !== (created.filament.material ?? "").trim().toUpperCase() && !source.tagged)
        ? { set_slot: true }
        : {}),
    });
  }

  const body = [type.body, { color: c.text }];
  const small = [type.small, { color: c.muted }];
  const url = status?.spoolman_web_url ?? null;
  const openInSpoolman = url ? (
    <Button
      label="Open in Spoolman"
      variant="secondary"
      onPress={() => void Linking.openURL(spool ? `${url.replace(/\/+$/, "")}/spool/show/${spool.id}` : url)}
    />
  ) : null;

  const pick = <T extends { id: number }>(items: T[], label: (item: T) => string, onPick: (item: T) => void) =>
    items.map((item) => (
      <Pressable
        key={item.id}
        accessibilityRole="button"
        accessibilityLabel={label(item)}
        onPress={() => onPick(item)}
        style={{ paddingVertical: space.sm, borderTopWidth: 1, borderTopColor: c.borderSoft }}
      >
        <Text style={body}>{label(item)}</Text>
      </Pressable>
    ));

  if (!creating) {
    return (
      <ScrollView keyboardShouldPersistTaps="handled" style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
        <View style={{ gap: space.xs }}>
          <Text style={[type.h1, { color: c.text }]}>{spool ? spoolLabel(spool) : `#${spoolId}`}</Text>
          {spool && (
            <Text style={small}>
              {gramsLeft(spool)}
              {spool.filament.material ? ` · ${spool.filament.material}` : ""}
              {spool.archived ? " · archived" : ""}
            </Text>
          )}
        </View>
        {!!error && <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>}
        {spool && !spool.archived && (
          <Surface padded style={{ gap: space.md }}>
            <Text style={[type.h2, { color: c.text }]}>Weigh</Text>
            <Field
              label={gross ? "Scale reads · g (spool included)" : "Filament left · g"}
              value={weight}
              onChangeText={setWeight}
              keyboardType="decimal-pad"
              hint={
                gross
                  ? (() => {
                      const tare = spool.spool_weight ?? spool.filament.spool_weight;
                      return tare != null
                        ? `Empty spool: ${Math.round(tare)} g. Spoolman takes that off.`
                        : "Empty spool weight unknown in Spoolman; enter the filament only, or set the spool weight in Spoolman first.";
                    })()
                  : "Filament only, without the empty spool."
              }
            />
            <Button
              label={gross ? "I weighed the filament only" : "The scale includes the spool"}
              variant="secondary"
              disabled={busy}
              onPress={() => { setGross(!gross); setWeight(""); }}
            />
            <Button
              label="Save weight"
              disabled={busy || positive(weight) == null}
              loading={busy}
              onPress={() =>
                void run(async () => {
                  const grams = positive(weight)!;
                  const updated = await weighSpool(spool.id, gross ? { gross_weight: grams } : { remaining_weight: grams });
                  setSpool(updated);
                  setWeight("");
                  showToast(`#${spool.id} · ${gramsLeft(updated)}`, { severity: "success" });
                })
              }
            />
          </Surface>
        )}
        {spool && (
          <Surface padded style={{ gap: space.md }}>
            {openInSpoolman}
            {!spool.archived && !archiving && (
              <Button label="Archive in Spoolman" variant="secondary" disabled={busy} onPress={() => setArchiving(true)} />
            )}
            {!spool.archived && archiving && (
              <View style={{ gap: space.md }}>
                <Text style={[type.h2, { color: c.text }]}>Archive #{spool.id}</Text>
                <Text style={small}>Its history stays in Spoolman. Take it out of its slot first.</Text>
                <Field
                  label="Empty spool on the scale · g (optional)"
                  value={bareWeight}
                  onChangeText={setBareWeight}
                  keyboardType="number-pad"
                  hint="Weigh the bare spool once it's empty; Spoolman keeps it as this spool's weight, and the vendor's default can take it."
                />
                <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap" }}>
                  <Button
                    label="Archive"
                    variant="danger"
                    disabled={busy || (bareWeight.trim() !== "" && !(positive(bareWeight) ?? 0))}
                    loading={busy}
                    onPress={() =>
                      void run(async () => {
                        const grams = positive(bareWeight);
                        const body = grams ? { empty_spool_weight: grams } : undefined;
                        let result;
                        let fromSlot = false;
                        try {
                          result = await archiveSpool(spool.id, body);
                        } catch (e) {
                          // An assigned spool can't be archived. Opened from its slot, the sheet takes it
                          // out first (the usual case: the spool in the slot is empty) and archives it.
                          const refused = e instanceof BridgeError && e.status === 409 && /^Remove #/.test(e.envelope.message);
                          if (!refused || !printerId || !sourceId) throw e;
                          await inventoryAssign(printerId, { source: sourceId, spool_id: null });
                          fromSlot = true;
                          result = await archiveSpool(spool.id, body);
                        }
                        showToast(
                          `#${spool.id} archived${fromSlot ? " and taken out of its slot" : ""}${grams ? ` · empty spool ${grams} g` : ""}`,
                          { severity: "success" },
                        );
                        const offer = result.vendor_offer;
                        if (offer) {
                          const current = offer.current_empty_spool_weight;
                          Alert.alert(
                            `${offer.vendor} spools weigh ${Math.round(offer.proposed_empty_spool_weight)} g empty?`,
                            current != null
                              ? `Spoolman has ${Math.round(current)} g for ${offer.vendor}. Use ${Math.round(offer.proposed_empty_spool_weight)} g for every ${offer.vendor} spool from now on?`
                              : `Spoolman has no empty-spool weight for ${offer.vendor} yet. Use ${Math.round(offer.proposed_empty_spool_weight)} g for every ${offer.vendor} spool from now on?`,
                            [
                              { text: "Keep as is", style: "cancel", onPress: () => router.back() },
                              {
                                text: "Use it",
                                onPress: () =>
                                  void run(async () => {
                                    try {
                                      await setVendorEmptyWeight(offer.vendor_id, offer.proposed_empty_spool_weight);
                                      showToast(`${offer.vendor}: empty spool ${Math.round(offer.proposed_empty_spool_weight)} g`, { severity: "success" });
                                    } catch (e) {
                                      showToast(`${offer.vendor} kept its old weight: ${inventoryErrorText(e)}`, { severity: "warn" });
                                    }
                                    router.back();
                                  }),
                              },
                            ],
                          );
                          return;
                        }
                        router.back();
                      })
                    }
                  />
                  <Button label="Cancel" variant="secondary" disabled={busy} onPress={() => { setArchiving(false); setBareWeight(""); }} />
                </View>
              </View>
            )}
          </Surface>
        )}
        {spool && history && history.length > 0 && (
          <Surface padded style={{ gap: space.md }}>
            <Text style={[type.h2, { color: c.text }]}>Prints from #{spool.id}</Text>
            {history.map((print, index) => (
              <View
                key={`${print.attempt_id ?? index}`}
                style={{ gap: 2, borderTopWidth: index ? 1 : 0, borderTopColor: c.borderSoft, paddingTop: index ? space.md : 0 }}
              >
                <PartPicture captureId={print.capture_id} height={96} />
                <Text style={body} numberOfLines={1}>{print.title || "Print"}</Text>
                <Text style={small}>
                  {[whenText({ ended_at: print.ended_at, created_at: null } as never), historyLine(print)].filter(Boolean).join(" · ")}
                </Text>
              </View>
            ))}
          </Surface>
        )}
        <Button label="Done" variant="secondary" disabled={busy} onPress={() => router.back()} fullWidth />
      </ScrollView>
    );
  }

  // Spoolman needs a density and the bridge a package weight for every product.
  const canSave = !!(
    product ||
    (newProduct && (vendor || vendorName.trim()) && productName.trim() && material.trim() && decimal(density) != null && (positive(netWeight) ?? 0) > 0)
  );
  return (
    <ScrollView keyboardShouldPersistTaps="handled" style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
      <Text style={[type.h1, { color: c.text }]}>New spool</Text>
      {!!error && <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>}

      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Product</Text>
        {product ? (
          <>
            <Text style={body}>{[product.vendor?.name, product.name ?? product.material].filter(Boolean).join(" ")}</Text>
            <Button label="Pick a different product" variant="secondary" disabled={busy} onPress={() => setProduct(null)} />
          </>
        ) : (
          <>
            <Field label="Search products" value={productQuery} onChangeText={setProductQuery} placeholder="Vendor, name or material" />
            <Button
              label="Search"
              variant="secondary"
              disabled={busy}
              onPress={() =>
                void run(async () => {
                  const [mine, fromCatalog] = await Promise.all([
                    inventoryProducts(productQuery),
                    // An older bridge has no catalog route; the list simply stays away.
                    productQuery.trim() ? inventoryCatalog(productQuery).catch(() => []) : Promise.resolve([]),
                  ]);
                  setProducts(mine);
                  setCatalog(fromCatalog);
                })
              }
            />
            {pick(products ?? [], (p) => `${[p.vendor?.name, p.name ?? p.material].filter(Boolean).join(" ")} · ${p.weight ?? "?"} g`, (p) => { setProduct(p); setNewProduct(false); })}
            {products && products.length === 0 && (
              <Text style={small}>
                {catalog.length ? "Nothing in Spoolman matches. Pick it from the catalog below, or tap Product is new to Spoolman." : "Nothing in Spoolman matches. Tap Product is new to Spoolman and fill it in."}
              </Text>
            )}
            {catalog.length > 0 && (
              <>
                <Text style={[type.h2, { color: c.text, marginTop: space.sm }]}>From Spoolman's catalog</Text>
                {pick(
                  catalog.map((e, i) => ({ ...e, id: i })),
                  (e) => `${[e.manufacturer, (e.name ?? "").replace(/[™®©]/g, "")].filter(Boolean).join(" ")} · ${e.material ?? ""}${e.weight ? ` · ${Math.round(e.weight)} g` : ""}`,
                  (e) => useCatalogEntry(catalog[e.id]),
                )}
              </>
            )}
            {!newProduct && (
              <Button label="Product is new to Spoolman" variant="secondary" disabled={busy} onPress={() => setNewProduct(true)} />
            )}
          </>
        )}
      </Surface>

      {newProduct && !product && (
        <Surface padded style={{ gap: space.md }}>
          <Text style={[type.h2, { color: c.text }]}>New product</Text>
          {vendor || vendorName.trim() ? (
            <>
              <Text style={body}>Vendor: {vendor?.name ?? vendorName.trim()}{vendor ? "" : " (from the catalog; found or created on save)"}</Text>
              <Button label="Pick a different vendor" variant="secondary" disabled={busy} onPress={() => { setVendor(null); setVendorName(""); }} />
            </>
          ) : (
            <>
              <Field label="Vendor" value={vendorQuery} onChangeText={setVendorQuery} placeholder="Bambu Lab" autoCapitalize="words" />
              <Button
                label="Search vendors"
                variant="secondary"
                disabled={busy}
                onPress={() => void run(async () => setVendors(await inventoryVendors(vendorQuery)))}
              />
              {pick(vendors, (v) => v.name, (v) => { setVendor(v); if (v.empty_spool_weight != null && !emptyWeight) setEmptyWeight(String(v.empty_spool_weight)); })}
              <Button
                label={`Create vendor "${vendorQuery.trim()}"`}
                variant="secondary"
                disabled={busy || !vendorQuery.trim()}
                onPress={() =>
                  void run(async () => {
                    const created = await createVendor({ name: vendorQuery.trim() });
                    setVendor(created);
                  })
                }
              />
            </>
          )}
          <Field label="Name and colour" value={productName} onChangeText={setProductName} placeholder="PETG Translucent Brown" />
          <Field
            label="Material"
            value={material}
            onChangeText={(text) => {
              setMaterial(text);
              const d = DENSITY[text.trim().toUpperCase()];
              if (d && !density) setDensity(String(d));
            }}
            placeholder="PETG"
            autoCapitalize="characters"
          />
          <Field label="Colour · hex" value={color} onChangeText={setColor} placeholder="6B4423" autoCapitalize="characters" />
          <Field label="Density · g/cm³" value={density} onChangeText={setDensity} keyboardType="decimal-pad" error={decimal(density) == null ? "Spoolman needs a density; the material fills a typical one." : null} />
          <Field label="Filament on a full spool · g" value={netWeight} onChangeText={setNetWeight} keyboardType="number-pad" error={(positive(netWeight) ?? 0) > 0 ? null : "The package weight sets every spool's percent left."} />
          <Field label="Empty spool · g" value={emptyWeight} onChangeText={setEmptyWeight} keyboardType="decimal-pad" hint="Lets a scale reading with the spool on it work." />
        </Surface>
      )}

      <Surface padded style={{ gap: space.md }}>
        <Field
          label="Filament left · g"
          value={remaining}
          onChangeText={setRemaining}
          keyboardType="decimal-pad"
          hint="Leave blank for a full spool."
        />
        <Button
          label={sourceId ? "Create and put in this slot" : "Create spool"}
          disabled={busy || !canSave}
          loading={busy}
          onPress={() =>
            void run(async () => {
              let filament = product;
              if (!filament) {
                const hex = color.replace(/^#/, "").trim();
                filament = await createProduct({
                  name: productName.trim(),
                  vendor_id: await vendorForProduct(),
                  material: material.trim().toUpperCase(),
                  color_hex: /^[0-9a-fA-F]{6}$/.test(hex) ? hex.toUpperCase() : "808080",
                  density: decimal(density)!,
                  weight: positive(netWeight)!,
                  ...(positive(emptyWeight) != null ? { spool_weight: positive(emptyWeight)! } : {}),
                  ...(decimal(diameter) != null && decimal(diameter) !== 1.75 ? { diameter: decimal(diameter)! } : {}),
                  ...(externalId ? { external_id: externalId } : {}),
                });
              }
              const created = await createSpool({
                filament_id: filament.id,
                ...(positive(remaining) != null ? { remaining_weight: positive(remaining)! } : {}),
              });
              let note = `Write #${created.id} on the spool.`;
              try {
                await assignTo(created);
              } catch (e) {
                note = `Write #${created.id} on the spool. It wasn't put in the slot: ${inventoryErrorText(e)}`;
              }
              Alert.alert(`Spool #${created.id}`, note, [{ text: "OK", onPress: () => router.back() }]);
            })
          }
        />
      </Surface>
      {openInSpoolman}
      <Button label="Cancel" variant="secondary" disabled={busy} onPress={() => router.back()} fullWidth />
    </ScrollView>
  );
}
