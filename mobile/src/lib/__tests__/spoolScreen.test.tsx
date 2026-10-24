import React from "react";

// Mounting a whole screen under jest-expo takes seconds when the host is busy
// (a Python suite running alongside); give these suites room.
jest.setTimeout(20_000);
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Alert, Text } from "react-native";

import SpoolScreen from "../../../app/spool";
import * as api from "../../api/inventory";
import { BridgeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";

const mockBack = jest.fn();
const mockToast = jest.fn();
let mockParams: Record<string, string> = {};
jest.mock("expo-router", () => ({
  useRouter: () => ({ back: mockBack, push: jest.fn(), replace: jest.fn() }),
  useLocalSearchParams: () => mockParams,
}));
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: any) => pick({ show: mockToast }) }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../api/inventory", () => ({
  archiveSpool: jest.fn(), createProduct: jest.fn(), createSpool: jest.fn(), createVendor: jest.fn(),
  inventoryAssign: jest.fn(), inventoryProducts: jest.fn(), inventorySources: jest.fn(), inventorySpool: jest.fn(),
  inventoryStatus: jest.fn(), inventoryVendors: jest.fn(), weighSpool: jest.fn(), spoolHistory: jest.fn(), inventoryCatalog: jest.fn(),
  setVendorEmptyWeight: jest.fn(),
}));
jest.mock("../../components/PartPicture", () => ({ PartPicture: () => null }));
const mocked = api as jest.Mocked<typeof api>;
const status = { configured: true, api_version: 2, provider_id: "prov", revision: 7, assignments: [], accounting_mode: "automatic", native_overlay_enabled: false, spoolman_web_url: "http://spoolman.test" } as api.InventoryStatus;
const spool = (over: Partial<api.InventorySpool> = {}): api.InventorySpool => ({
  id: 5, archived: false, remaining_weight: 412, initial_weight: 1000, spool_weight: 250,
  filament: { id: 50, name: "PET-GF15 Black", vendor: { id: 1, name: "Bambu Lab", empty_spool_weight: 250 }, material: "PET-GF", color_hex: "222222", weight: 1000, spool_weight: 250, density: 1.3, diameter: 1.75 },
  ...over,
});

let tree: ReactTestRenderer;
async function mount() { await act(async () => { tree = create(<SpoolScreen />); }); }
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
const button = (label: string | RegExp) => tree.root.findAllByType(Button).find((n) => (typeof label === "string" ? n.props.label === label : label.test(n.props.label)))!;
const field = (label: string | RegExp) => tree.root.findAllByType(Field).find((n) => (typeof label === "string" ? n.props.label === label : label.test(n.props.label)))!;
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }
async function type(node: any, text: string) { await act(async () => { node.props.onChangeText(text); }); }

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = { id: "5" };
  mocked.inventoryStatus.mockResolvedValue(status);
  mocked.inventorySpool.mockResolvedValue(spool());
  mocked.weighSpool.mockResolvedValue(spool({ remaining_weight: 380 }));
  mocked.archiveSpool.mockResolvedValue(spool({ archived: true }));
  mocked.spoolHistory.mockResolvedValue([
    { attempt_id: "x", title: "Benchy", ended_at: 1792771729, capture_id: null, layers: [41, 186], total_layers: 186, share_pct: 77.4, grams: 12 },
  ]);
});

it("lists the prints a spool fed, with layer ranges", async () => {
  await mount();
  expect(texts()).toContain("Prints from #5");
  expect(texts()).toContain("Benchy");
  expect(texts().some((t) => t.endsWith("layers 41–186 · 77% · 12 g"))).toBe(true);
});

it("weighs with the spool on the scale by default, filament-only on request", async () => {
  await mount();
  expect(texts()).toContain("#5 · Bambu Lab PET-GF15 Black");
  expect(button("Save weight").props.disabled).toBe(true);
  await type(field(/Scale reads/), "630");
  await press(button("Save weight"));
  expect(mocked.weighSpool).toHaveBeenCalledWith(5, { gross_weight: 630 });
  expect(mockToast).toHaveBeenCalledWith("#5 · 380 g left", { severity: "success" });
  await press(button("I weighed the filament only"));
  await type(field("Filament left · g"), "380");
  await press(button("Save weight"));
  expect(mocked.weighSpool).toHaveBeenLastCalledWith(5, { remaining_weight: 380 });
});

it("archives from an inline confirm and goes back", async () => {
  await mount();
  await press(button("Archive in Spoolman"));
  expect(texts()).toContain("Its history stays in Spoolman. Take it out of its slot first.");
  await press(button("Archive"));
  expect(mocked.archiveSpool).toHaveBeenCalledWith(5, undefined);
  expect(mockBack).toHaveBeenCalled();
});

it("records the bare spool's weight when archiving and offers it to the vendor", async () => {
  mocked.archiveSpool.mockResolvedValue({ ...spool({ archived: true }), vendor_offer: { vendor_id: 1, vendor: "Bambu Lab", current_empty_spool_weight: 250, proposed_empty_spool_weight: 246 } } as any);
  mocked.setVendorEmptyWeight.mockResolvedValue({ id: 1, name: "Bambu Lab", empty_spool_weight: 246 });
  const alert = jest.spyOn(Alert, "alert").mockImplementation((_t, _m, buttons) => { void (buttons as any)[1].onPress(); });
  await mount();
  await press(button("Archive in Spoolman"));
  await type(field(/Empty spool on the scale/), "246");
  await press(button("Archive"));
  await act(async () => {});
  expect(mocked.archiveSpool).toHaveBeenCalledWith(5, { empty_spool_weight: 246 });
  expect(alert).toHaveBeenCalledWith("Bambu Lab spools weigh 246 g empty?", expect.stringContaining("Spoolman has 250 g for Bambu Lab. Use 246 g"), expect.anything());
  expect(mocked.setVendorEmptyWeight).toHaveBeenCalledWith(1, 246);
  expect(mockBack).toHaveBeenCalled();
  alert.mockRestore();
});

it("shows the bridge's refusal in place", async () => {
  mocked.weighSpool.mockRejectedValue(new BridgeError({ error: "conflict", message: "#5 is archived in Spoolman" }, 409));
  await mount();
  await type(field(/Scale reads/), "630");
  await press(button("Save weight"));
  expect(texts()).toContain("#5 is archived in Spoolman");
});

it("creates vendor, product and spool inline, then puts the spool in the slot it was opened from", async () => {
  mockParams = { new: "1", printer: "P1", source: "ams:abc:tray:2" };
  mocked.inventoryVendors.mockResolvedValue([]);
  mocked.createVendor.mockResolvedValue({ id: 9, name: "Sunlu", empty_spool_weight: null });
  mocked.createProduct.mockResolvedValue({ id: 90, name: "PETG Blue", vendor: { id: 9, name: "Sunlu", empty_spool_weight: null }, material: "PETG", color_hex: "0000FF", weight: 1000, spool_weight: 180, density: 1.27, diameter: 1.75 });
  mocked.createSpool.mockResolvedValue(spool({ id: 13, filament: { id: 90, name: "PETG Blue", vendor: { id: 9, name: "Sunlu", empty_spool_weight: null }, material: "PETG", color_hex: "0000FF", weight: 1000, spool_weight: 180, density: 1.27, diameter: 1.75 } }));
  mocked.inventorySources.mockResolvedValue([{ printer_id: "P1", source: "ams:abc:tray:2", wire_slot: 2, label: "AMS A · slot 3", material: "ASA", color: "FFFFFFFF", observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false }]);
  mocked.inventoryAssign.mockResolvedValue({ provider_id: "prov", revision: 8, assignments: [] });
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  await mount();
  await press(button("Product is new to Spoolman"));
  await type(field("Vendor"), "Sunlu");
  await press(button('Create vendor "Sunlu"'));
  expect(mocked.createVendor).toHaveBeenCalledWith({ name: "Sunlu" });
  await type(field("Name and colour"), "PETG Blue");
  await type(field("Material"), "petg");
  expect(field("Density · g/cm³").props.value).toBe("1.27");
  await type(field("Colour · hex"), "0000ff");
  await type(field("Empty spool · g"), "180");
  await type(field("Filament left · g"), "950");
  await press(button("Create and put in this slot"));
  expect(mocked.createProduct).toHaveBeenCalledWith({ name: "PETG Blue", vendor_id: 9, material: "PETG", color_hex: "0000FF", density: 1.27, weight: 1000, spool_weight: 180 });
  expect(mocked.createSpool).toHaveBeenCalledWith({ filament_id: 90, remaining_weight: 950 });
  expect(mocked.inventoryAssign).toHaveBeenCalledWith("P1", expect.objectContaining({ source: "ams:abc:tray:2", spool_id: 13, set_slot: true }));
  expect(alert).toHaveBeenCalledWith("Spool #13", "Write #13 on the spool.", expect.anything());
  alert.mockRestore();
});

it("says when nothing in Spoolman matches and offers the catalog, which prefills the new product", async () => {
  mockParams = { new: "1", printer: "P1", source: "ams:abc:tray:1" };
  mocked.inventoryProducts.mockResolvedValue([]);
  mocked.inventoryCatalog.mockResolvedValue([
    { id: "polymaker_petg_polylitepetgblack_1000_175_c", manufacturer: "Polymaker", name: "PolyLite™ PETG Black", material: "PETG", density: 1.27, weight: 1000, spool_weight: 140, diameter: 1.75, color_hex: "000000" },
  ]);
  mocked.inventoryVendors.mockResolvedValue([{ id: 9, name: "Polymaker", empty_spool_weight: null }]);
  mocked.createProduct.mockResolvedValue({ id: 90, name: "PolyLite PETG Black", vendor: { id: 9, name: "Polymaker", empty_spool_weight: null }, material: "PETG", color_hex: "000000", weight: 1000, spool_weight: 140, density: 1.27, diameter: 1.75 });
  mocked.createSpool.mockResolvedValue(spool({ id: 14 }));
  mocked.inventorySources.mockResolvedValue([{ printer_id: "P1", source: "ams:abc:tray:1", wire_slot: 1, label: "AMS A · slot 2", material: "PETG", color: "000000FF", observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false }]);
  mocked.inventoryAssign.mockResolvedValue({ provider_id: "prov", revision: 8, assignments: [] });
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  await mount();
  await type(field("Search products"), "Polymaker Petg galaxy black");
  await press(button("Search"));
  expect(mocked.inventoryCatalog).toHaveBeenCalledWith("Polymaker Petg galaxy black");
  expect(texts()).toContain("Nothing in Spoolman matches. Pick it from the catalog below, or tap Product is new to Spoolman.");
  expect(texts()).toContain("From Spoolman's catalog");
  const entry = tree.root.findAll((n) => typeof n.props.onPress === "function" && String(n.props.accessibilityLabel ?? "").startsWith("Polymaker PolyLite PETG Black"))[0];
  await press(entry);
  expect(field("Name and colour").props.value).toBe("PolyLite PETG Black");
  expect(field("Material").props.value).toBe("PETG");
  expect(field("Density · g/cm³").props.value).toBe("1.27");
  expect(field("Filament on a full spool · g").props.value).toBe("1000");
  expect(field("Empty spool · g").props.value).toBe("140");
  expect(texts().some((t) => t.startsWith("Vendor: Polymaker"))).toBe(true);
  await press(button("Create and put in this slot"));
  expect(mocked.createVendor).not.toHaveBeenCalled();
  expect(mocked.createProduct).toHaveBeenCalledWith({ name: "PolyLite PETG Black", vendor_id: 9, material: "PETG", color_hex: "000000", density: 1.27, weight: 1000, spool_weight: 140, external_id: "polymaker_petg_polylitepetgblack_1000_175_c" });
  expect(mocked.createSpool).toHaveBeenCalledWith({ filament_id: 90 });
  alert.mockRestore();
});

it("points at Product is new to Spoolman when neither Spoolman nor the catalog has it", async () => {
  mockParams = { new: "1" };
  mocked.inventoryProducts.mockResolvedValue([]);
  mocked.inventoryCatalog.mockResolvedValue([]);
  await mount();
  await type(field("Search products"), "unobtainium");
  await press(button("Search"));
  expect(texts()).toContain("Nothing in Spoolman matches. Tap Product is new to Spoolman and fill it in.");
});

it("takes an assigned spool out of its slot before archiving, when opened from that slot", async () => {
  mockParams = { id: "5", printer: "P1", source: "ams:abc:tray:3" };
  mocked.archiveSpool
    .mockRejectedValueOnce(new BridgeError({ error: "conflict", message: "Remove #5 from AMS A · slot 4 first" }, 409))
    .mockResolvedValueOnce(spool({ archived: true }) as any);
  mocked.inventoryAssign.mockResolvedValue({ provider_id: "prov", revision: 9, assignments: [] });
  await mount();
  await press(button("Archive in Spoolman"));
  await press(button("Archive"));
  expect(mocked.inventoryAssign).toHaveBeenCalledWith("P1", { source: "ams:abc:tray:3", spool_id: null });
  expect(mocked.archiveSpool).toHaveBeenCalledTimes(2);
  expect(mockToast).toHaveBeenCalledWith("#5 archived and taken out of its slot", { severity: "success" });
  expect(mockBack).toHaveBeenCalled();
});

it("shows the refusal when the sheet was opened without a slot", async () => {
  mockParams = { id: "5" };
  mocked.archiveSpool.mockRejectedValue(new BridgeError({ error: "conflict", message: "Remove #5 from AMS A · slot 4 first" }, 409));
  await mount();
  await press(button("Archive in Spoolman"));
  await press(button("Archive"));
  expect(mocked.inventoryAssign).not.toHaveBeenCalled();
  expect(texts()).toContain("Remove #5 from AMS A · slot 4 first");
});
