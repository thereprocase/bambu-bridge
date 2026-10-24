import { useEffect, useRef, useState } from "react";
import { Text, View } from "react-native";

import {
  inventorySpool,
  inventoryStatus,
  inventoryReady,
  materialMappings,
  selectStockMaterial,
  stockMaterials,
  type InventorySpool,
  type MaterialMappings,
  type StockCatalogue,
} from "../api/inventory";
import { Button } from "./Button";
import { Field } from "./Field";
import { useTheme } from "../theme/ThemeProvider";

interface Props {
  spool: InventorySpool;
  providerId: string;
  disabled?: boolean;
  onChanged: () => Promise<void>;
}
const issues: Record<string, string> = {
  not_selected: "No stock material selected.",
  catalogue_changed: "The stock catalogue changed. Review this selection.",
  inventory_product_changed:
    "The inventory product changed. Review this selection.",
  stock_profile_unavailable: "The selected stock identity is unavailable.",
};

export function StockMaterialSelector({
  spool,
  providerId,
  disabled,
  onChanged,
}: Props) {
  const { c, space, type } = useTheme();
  const [opened, setOpened] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reloadRequired, setReloadRequired] = useState(true);
  const [mapping, setMapping] = useState<MaterialMappings | null>(null);
  const [catalogue, setCatalogue] = useState<StockCatalogue | null>(null);
  const [current, setCurrent] = useState(spool);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const guard = useRef(false);
  const generation = useRef(0);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  const body = [type.body, { color: c.text }];

  async function run(action: (version: number) => Promise<void>) {
    if (guard.current || disabled) return;
    const version = generation.current;
    guard.current = true;
    setBusy(true);
    setError("");
    try {
      await action(version);
    } catch (e) {
      if (version === generation.current) {
        setError(e instanceof Error ? e.message : String(e));
        setReloadRequired(true);
      }
    } finally {
      guard.current = false;
      if (version === generation.current) setBusy(false);
    }
  }

  async function load(version: number) {
    const [state, selections, fresh] = await Promise.all([
      inventoryStatus(),
      materialMappings(),
      inventorySpool(spool.id),
    ]);
    if (
      state.provider_id !== providerId ||
      selections.provider_id !== providerId
    )
      throw new Error(
        "The inventory provider changed. Refresh inventory before selecting.",
      );
    if (fresh.filament.id !== spool.filament.id)
      throw new Error(
        "This spool’s product changed. Refresh inventory before selecting.",
      );
    if (!fresh.filament.material)
      throw new Error(
        "Set this product’s chemistry in Spoolman before choosing a stock identity.",
      );
    const profiles = await stockMaterials(fresh.filament.material);
    if (version !== generation.current) return;
    setCurrent(fresh);
    setMapping(selections);
    setCatalogue(profiles);
    setOpened(true);
    setReloadRequired(false);
  }

  async function save(key: string | null, version: number) {
    if (!mapping || reloadRequired)
      throw new Error("Reload the choices before saving.");
    const state = await inventoryStatus();
    if (
      state.provider_id !== providerId ||
      (key !== null && !inventoryReady(state))
    )
      throw new Error(
        "Review the inventory connection before selecting a stock identity.",
      );
    const result = await selectStockMaterial(current.filament.id, {
      provider_id: providerId,
      expected_revision: mapping.revision,
      stock_key: key,
      catalogue_digest: catalogue?.catalogue_digest,
    });
    if (version !== generation.current) return;
    if (result.provider_id !== providerId)
      throw new Error(
        "The inventory provider changed. Reload before continuing.",
      );
    const savedKey =
      result.mappings.find((m) => m.product_id === current.filament.id)
        ?.stock_key ?? null;
    if (savedKey !== key)
      throw new Error("The saved selection differs. Reload before continuing.");
    const chosen = catalogue?.profiles.find((p) => p.key === key);
    setCurrent((previous) => ({
      ...previous,
      orca_material: {
        selection: chosen
          ? {
              key: chosen.key,
              name: chosen.name,
              material_id: chosen.material_id,
              material: chosen.material,
              source_verified: true,
              runtime_qualified: false,
            }
          : null,
        issue: chosen ? null : "not_selected",
      },
    }));
    setMapping(result);
    setMessage(
      key === null ? "Stock selection cleared." : "Stock selection saved.",
    );
    await onChanged();
    if (version === generation.current) await load(version);
  }

  const selected = mapping?.mappings.find(
    (m) => m.product_id === current.filament.id,
  );
  const name = mapping
    ? selected
      ? (catalogue?.profiles.find((p) => p.key === selected.stock_key)?.name ??
        "Unavailable stock identity")
      : null
    : current.orca_material?.selection?.name;
  const selectionIssue = current.orca_material?.issue;
  const blocked = busy || !!disabled;
  return (
    <View style={{ gap: space.md }}>
      <Text style={[type.h2, { color: c.text }]}>Stock Orca material</Text>
      <Text style={body}>Saved identity: {name ?? "None"}</Text>
      {!!selectionIssue && selectionIssue !== "not_selected" && (
        <Text style={body}>
          {issues[selectionIssue] ?? "Review the saved material selection."}
        </Text>
      )}
      <Text style={body}>
        This choice applies to every spool of this product. Your tuned preset
        remains a separate selection in Orca.
      </Text>
      <Text style={body}>
        Saving a choice does not enable display sync. Check display controls for
        its current state.
      </Text>
      {!!error && (
        <Text
          accessibilityRole="alert"
          style={[type.body, { color: c.danger }]}
        >
          {error}
        </Text>
      )}
      {!!message && (
        <Text accessibilityLiveRegion="polite" style={body}>
          {message}
        </Text>
      )}
      <Button
        label={opened ? "Reload stock choices" : "Choose stock identity"}
        variant="secondary"
        disabled={blocked}
        onPress={() => void run(load)}
      />
      {opened && (
        <>
          <Text style={body}>
            Orca {catalogue?.upstream_tag} · {current.filament.material} ·
            nominal diameter {current.filament.diameter ?? "unknown"} mm
          </Text>
          <Field
            label="Filter stock names"
            value={query}
            onChangeText={setQuery}
          />
          {reloadRequired && (
            <Text style={body}>
              Reload to inspect the saved result before making another change.
              Nothing is retried automatically.
            </Text>
          )}
          {catalogue?.profiles
            .filter((p) =>
              p.name.toLowerCase().includes(query.trim().toLowerCase()),
            )
            .map((p) => (
              <Button
                key={p.key}
                label={`Use ${p.name}`}
                variant={
                  selected?.stock_key === p.key ? "primary" : "secondary"
                }
                disabled={
                  blocked ||
                  reloadRequired ||
                  current.filament.diameter !== 1.75
                }
                onPress={() => void run((version) => save(p.key, version))}
              />
            ))}
          <Button
            label="Clear stock selection"
            variant="secondary"
            disabled={blocked || reloadRequired || !selected}
            onPress={() => void run((version) => save(null, version))}
          />
        </>
      )}
    </View>
  );
}
