import React from "react";

// Mounting a whole screen under jest-expo takes seconds when the host is busy
// (a Python suite running alongside); give these suites room.
jest.setTimeout(20_000);
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Text } from "react-native";

import AssignSpoolScreen from "../../../app/assign-spool";
import * as api from "../../api/inventory";
import { BridgeError } from "../../api/errors";
import { Button } from "../../components/Button";

const mockBack = jest.fn();
const mockToast = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ back: mockBack, push: jest.fn() }),
  useLocalSearchParams: () => ({ printer: "P1", source: "ams:abc:tray:2" }),
}));
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: any) => pick({ show: mockToast }) }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../api/inventory", () => ({
  inventoryAssign: jest.fn(),
  inventorySources: jest.fn(),
  inventorySpools: jest.fn(),
  inventoryStatus: jest.fn(),
}));

const mocked = api as jest.Mocked<typeof api>;
const status = { configured: true, api_version: 2, provider_id: "prov", revision: 7, assignments: [], accounting_mode: "automatic", native_overlay_enabled: false };
const source = (over: Partial<api.InventorySource> = {}): api.InventorySource => ({
  printer_id: "P1", source: "ams:abc:tray:2", wire_slot: 2, label: "AMS A · slot 3", material: "PETG", color: "FF0000FF",
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false, ...over,
});
const spool = (id: number, material: string, archived = false): api.InventorySpool => ({
  id, archived, remaining_weight: 640, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `Name ${id}`, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material, color_hex: "00FF00", weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});

let tree: ReactTestRenderer;
async function mount() {
  await act(async () => { tree = create(<AssignSpoolScreen />); });
}
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
const rows = (label: string) =>
  tree.root.findAll((n) => typeof n.props.onPress === "function" && String(n.props.accessibilityLabel ?? "").startsWith(label));
const rowFor = (label: string) => rows(label)[0];
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }

beforeEach(() => {
  jest.clearAllMocks();
  mocked.inventoryStatus.mockResolvedValue(status as any);
  mocked.inventorySources.mockResolvedValue([source()]);
  mocked.inventorySpools.mockResolvedValue([spool(1, "PLA"), spool(2, "PETG"), spool(3, "PETG", true)]);
  mocked.inventoryAssign.mockResolvedValue({ provider_id: "prov", revision: 8, assignments: [] });
});

it("lists same-material spools, then other materials for an untagged slot, and assigns in one tap", async () => {
  await mount();
  expect(texts()).toContain("Slot 3");
  expect(texts()).toContain("PETG");
  expect(rowFor("#2 · Bambu Name 2")).toBeDefined();
  expect(rowFor("#1 · Bambu Name 1")).toBeDefined();
  expect(rows("#3")).toHaveLength(0);
  await press(rowFor("#2 · Bambu Name 2"));
  expect(mocked.inventoryAssign).toHaveBeenCalledWith("P1", {
    source: "ams:abc:tray:2", spool_id: 2, observed_identity: "a".repeat(64),
  });
  expect(mockToast).toHaveBeenCalledWith("#2 · Bambu Name 2 in slot 3", { severity: "success" });
  expect(mockBack).toHaveBeenCalled();
});

it("sets the slot's material when another material is picked", async () => {
  await mount();
  await press(rowFor("#1 · Bambu Name 1"));
  expect(mocked.inventoryAssign.mock.calls[0][1]).toMatchObject({ spool_id: 1, set_slot: true });
});

it("hides other materials for an RFID slot and says so when nothing matches", async () => {
  mocked.inventorySources.mockResolvedValue([source({ tagged: true, material: "ASA" })]);
  await mount();
  expect(texts()).toContain("ASA from the tag");
  expect(texts()).toContain("No ASA spools in Spoolman yet.");
  expect(rows("#1")).toHaveLength(0);
});

it("shows the current spool with a remove action", async () => {
  mocked.inventoryStatus.mockResolvedValue({
    ...status,
    assignments: [{ printer_id: "P1", source: "ams:abc:tray:2", spool_id: 2, assigned_at: 1, observed_identity: null, wire_slot: 2 }],
  } as any);
  await mount();
  expect(texts().some((t) => t.includes("#2 · Bambu Name 2") && t.includes("640 g left"))).toBe(true);
  expect(texts()).toContain("No other PETG spools in Spoolman.");
  const remove = tree.root.findAllByType(Button).find((n) => n.props.label === "Remove from slot")!;
  await press(remove);
  expect(mocked.inventoryAssign.mock.calls[0][1]).toMatchObject({ spool_id: null });
  expect(mockToast).toHaveBeenCalledWith("Slot 3 has no spool now", { severity: "success" });
});

it("tells the owner when Spoolman is silent and keeps the sheet open", async () => {
  mocked.inventoryAssign.mockRejectedValue(new BridgeError({ error: "internal_error", message: "Spoolman: timeout" }, 503));
  await mount();
  await press(rowFor("#2 · Bambu Name 2"));
  expect(texts()).toContain("Spoolman didn't answer. Try again.");
  expect(mockBack).not.toHaveBeenCalled();
});

it("explains an empty slot", async () => {
  mocked.inventorySources.mockResolvedValue([source({ issues: ["source_empty"], material: "" })]);
  await mount();
  expect(texts()).toContain("Put a spool in first. The printer reports this slot as empty.");
});

it("offers nothing for a slot the printer hasn't reported lately", async () => {
  mocked.inventorySources.mockResolvedValue([source({ stale: true, age_seconds: 61, issues: ["ams_stale"], assignable: false })]);
  await mount();
  expect(texts()).toContain("The printer hasn't reported this slot lately (last seen 61 s ago). Try again in a moment.");
  expect(rows("#2")).toHaveLength(0);
});
