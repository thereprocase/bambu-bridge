import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Text } from "react-native";
import { StockMaterialSelector } from "../../components/StockMaterialSelector";
import { Button } from "../../components/Button";
import * as api from "../../api/inventory";

jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return {
      c: tokens.light,
      space: tokens.space,
      type: tokens.typography,
      radius: tokens.radius,
    };
  },
}));
jest.mock("../../api/inventory", () => ({
  ...Object.fromEntries(
    [
      "inventorySpool",
      "inventoryStatus",
      "materialMappings",
      "selectStockMaterial",
      "stockMaterials",
    ].map((key) => [key, jest.fn()]),
  ),
  inventoryReady: (s: any) => s?.configured === true && s?.api_version === 2,
}));
const provider = "00000000-0000-4000-8000-000000000001";
const digest = "a".repeat(64);
const profile: api.StockMaterial = {
  key: "Generic ASA @base.json",
  name: "Generic ASA",
  material: "ASA",
  material_id: "GFB98",
  resource_sha256: "b".repeat(64),
  inheritance_sha256: "c".repeat(64),
};
const original: api.InventorySpool = {
  id: 43,
  remaining_weight: 400,
  initial_weight: 1000,
  spool_weight: null,
  archived: false,
  filament: {
    id: 2,
    name: "ASA Black",
    vendor: null,
    material: "ASA",
    color_hex: "000000",
    diameter: 1.75,
    density: 1.07,
    weight: 1000,
    spool_weight: null,
  },
  orca_material: { selection: null, issue: "not_selected" },
};
let saved: api.MaterialMappings;
let tree: ReactTestRenderer;
let onChanged: jest.Mock;
async function mount() {
  await act(async () => {
    tree = create(
      <StockMaterialSelector
        spool={original}
        providerId={provider}
        onChanged={onChanged}
      />,
    );
  });
}
function button(label: string) {
  return tree.root
    .findAllByType(Button)
    .find((node) => node.props.label === label)!;
}
async function click(label: string) {
  expect(button(label).props.disabled).toBeFalsy();
  await act(async () => {
    button(label).props.onPress();
  });
}
function text() {
  return tree.root
    .findAllByType(Text)
    .map((node) => node.props.children)
    .flat(Infinity)
    .join(" ")
    .replace(/\s+/g, " ");
}

beforeEach(() => {
  jest.clearAllMocks();
  saved = { provider_id: provider, revision: 0, mappings: [] };
  onChanged = jest.fn(async () => {});
  jest.mocked(api.inventoryStatus).mockResolvedValue({
    configured: true,
    provider_id: provider,
    api_version: 2,
    accounting_mode: "disabled",
    native_overlay_enabled: false,
  });
  jest.mocked(api.materialMappings).mockImplementation(async () => saved);
  jest.mocked(api.stockMaterials).mockResolvedValue({
    upstream_tag: "v2.4.2",
    catalogue_digest: digest,
    runtime_qualified: false,
    profiles: [profile],
  });
  jest.mocked(api.inventorySpool).mockImplementation(async () => ({
    ...original,
    orca_material: saved.mappings.length
      ? {
          selection: {
            ...profile,
            source_verified: true,
            runtime_qualified: false,
          },
          issue: null,
        }
      : original.orca_material,
  }));
  jest.mocked(api.selectStockMaterial).mockImplementation(async (id, body) => {
    saved = {
      provider_id: provider,
      revision: body.expected_revision + 1,
      mappings: body.stock_key
        ? [
            {
              product_id: id,
              stock_key: body.stock_key,
              catalogue_digest: digest,
              product_fingerprint: "fingerprint",
            },
          ]
        : [],
    };
    return saved;
  });
});
afterEach(async () => {
  if (tree) await act(async () => tree.unmount());
});

test("opening reads choices without selecting a default; explicit choice seals the reviewed catalogue", async () => {
  await mount();
  expect(api.selectStockMaterial).not.toHaveBeenCalled();
  await click("Choose stock identity");
  expect(api.stockMaterials).toHaveBeenCalledWith("ASA");
  expect(api.selectStockMaterial).not.toHaveBeenCalled();
  await click("Use Generic ASA");
  expect(api.selectStockMaterial).toHaveBeenCalledWith(2, {
    provider_id: provider,
    expected_revision: 0,
    stock_key: profile.key,
    catalogue_digest: digest,
  });
  expect(text()).toContain("Saved identity: Generic ASA");
  expect(original.filament.color_hex).toBe("000000");
  expect(text()).toContain("Saving a choice does not enable display sync");
});

test("lost accepted response requires a readback and never automatically resends", async () => {
  await mount();
  await click("Choose stock identity");
  jest.mocked(api.selectStockMaterial).mockImplementation(async () => {
    saved = {
      provider_id: provider,
      revision: 1,
      mappings: [
        {
          product_id: 2,
          stock_key: profile.key,
          catalogue_digest: digest,
          product_fingerprint: "fingerprint",
        },
      ],
    };
    throw new Error("response lost");
  });
  await click("Use Generic ASA");
  expect(button("Use Generic ASA").props.disabled).toBe(true);
  expect(text()).toContain("Nothing is retried automatically");
  await click("Reload stock choices");
  expect(text()).toContain("Saved identity: Generic ASA");
  expect(api.selectStockMaterial).toHaveBeenCalledTimes(1);
});

test("provider namespace change prevents saving under a new provider", async () => {
  await mount();
  await click("Choose stock identity");
  jest.mocked(api.inventoryStatus).mockResolvedValue({
    configured: true,
    provider_id: "different-provider",
    api_version: 2,
    accounting_mode: "disabled",
    native_overlay_enabled: false,
  });
  await click("Use Generic ASA");
  expect(api.selectStockMaterial).not.toHaveBeenCalled();
  expect(text()).toContain("Review the inventory connection");
});

test("acknowledged clear is shown even if subsequent provider refresh is unavailable", async () => {
  saved = {
    provider_id: provider,
    revision: 1,
    mappings: [
      {
        product_id: 2,
        stock_key: profile.key,
        catalogue_digest: digest,
        product_fingerprint: "fingerprint",
      },
    ],
  };
  await mount();
  await click("Choose stock identity");
  jest.mocked(api.inventoryStatus).mockResolvedValue({
    configured: true,
    provider_id: provider,
    api_version: 2,
    accounting_mode: "disabled",
    native_overlay_enabled: false,
  });
  onChanged.mockRejectedValue(new Error("provider unavailable"));
  await click("Clear stock selection");
  expect(api.selectStockMaterial).toHaveBeenCalledWith(2, {
    provider_id: provider,
    expected_revision: 1,
    stock_key: null,
    catalogue_digest: digest,
  });
  expect(text()).toContain("Stock selection cleared");
  expect(text()).toContain("Saved identity: None");
});

test("a response after unmount cannot replace a newly selected spool", async () => {
  await mount();
  await click("Choose stock identity");
  let resolve!: (result: api.MaterialMappings) => void;
  jest.mocked(api.selectStockMaterial).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  await click("Use Generic ASA");
  await act(async () => tree.unmount());
  await act(async () =>
    resolve({
      provider_id: provider,
      revision: 1,
      mappings: [
        {
          product_id: 2,
          stock_key: profile.key,
          catalogue_digest: digest,
          product_fingerprint: "fingerprint",
        },
      ],
    }),
  );
  expect(onChanged).not.toHaveBeenCalled();
});
