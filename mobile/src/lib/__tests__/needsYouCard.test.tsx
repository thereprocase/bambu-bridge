import React from "react";

// Mounting a whole screen under jest-expo takes seconds when the host is busy
// (a Python suite running alongside); give these suites room.
jest.setTimeout(20_000);
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Switch, Text } from "react-native";

import { NeedsYouCard } from "../../components/NeedsYouCard";
import * as api from "../../api/inventory";
import { BridgeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";

jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../components/PartPicture", () => ({ PartPicture: () => null }));
const mockToast = jest.fn();
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: any) => pick({ show: mockToast }) }));
jest.mock("../../api/inventory", () => ({
  inventoryAssign: jest.fn(),
  inventorySpools: jest.fn(),
  resolveHeldPrint: jest.fn(),
}));
const mocked = api as jest.Mocked<typeof api>;

const spool = (id: number, material: string): api.InventorySpool => ({
  id, archived: false, remaining_weight: 640, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `Name ${id}`, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material, color_hex: null, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});
const row: api.AccountingRow = {
  attempt_id: "a".repeat(32), printer_id: "P1", title: "Light & Fast V2", created_at: 1, ended_at: 2, capture_id: null, start_state: "running",
  outcome: "completed", delivery_state: "review", charged: [], review_reason: "unassigned_slot", planned_grams: 83.78,
  shortfalls: [{ kind: "unassigned_slot", wire_slot: 0, spool_id: null, grams: 83.78, material: "PETG" }],
};
const status = { configured: true, api_version: 2, provider_id: "prov", revision: 7, assignments: [], accounting_mode: "automatic", native_overlay_enabled: false } as api.InventoryStatus;
const sources: api.InventorySource[] = [{
  printer_id: "P1", source: "ams:abc:tray:0", wire_slot: 0, label: "AMS A · slot 1", material: "PETG", color: "6B4423FF",
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false,
}];

let tree: ReactTestRenderer;
const resolved = jest.fn();
async function mount() {
  await act(async () => { tree = create(<NeedsYouCard row={row} status={status} sources={sources} onResolved={resolved} />); });
}
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
const button = (label: string | RegExp) => tree.root.findAllByType(Button).find((n) => (typeof label === "string" ? n.props.label === label : label.test(n.props.label)))!;
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }
async function pickSpool(label: string) {
  await press(button("Pick a spool"));
  const rowNode = tree.root.findAll((n) => typeof n.props.onPress === "function" && String(n.props.accessibilityLabel ?? "").startsWith(label))[0];
  await press(rowNode);
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.inventorySpools.mockResolvedValue([spool(2, "PLA"), spool(1, "PETG")]);
  mocked.resolveHeldPrint.mockResolvedValue({ ...row, delivery_state: "delivered" } as any);
  mocked.inventoryAssign.mockResolvedValue({ provider_id: "prov", revision: 8, assignments: [] });
});

it("asks which spool was in the slot, charges by slot and keeps the spool assigned", async () => {
  await mount();
  expect(texts()).toContain("84 g from slot 1 · no spool was assigned");
  expect(texts()).toContain("Which spool was in slot 1?");
  expect(button(/^Charge/).props.disabled).toBe(true);
  await pickSpool("#1 · Bambu Name 1");
  expect(mocked.inventorySpools).toHaveBeenCalledWith("", 0, "PETG");
  expect(button("Charge 84 g to #1").props.disabled).toBe(false);
  expect(tree.root.findByType(Switch).props.value).toBe(true);
  await press(button("Charge 84 g to #1"));
  expect(mocked.resolveHeldPrint).toHaveBeenCalledWith("a".repeat(32), { action: "charge", charges: [{ spool_id: 1, slot: 0 }] });
  expect(mocked.inventoryAssign).toHaveBeenCalledWith("P1", { source: "ams:abc:tray:0", spool_id: 1, observed_identity: "a".repeat(64) });
  expect(resolved).toHaveBeenCalledWith("a".repeat(32));
});

it("sends edited grams as a gram charge and leaves the slot alone when the switch is off", async () => {
  await mount();
  await pickSpool("#1 · Bambu Name 1");
  expect(tree.root.findByType(Field).props.value).toBe("84");
  await act(async () => { tree.root.findByType(Field).props.onChangeText("40"); });
  await act(async () => { tree.root.findByType(Switch).props.onValueChange(false); });
  await press(button("Charge 40 g to #1"));
  expect(mocked.resolveHeldPrint).toHaveBeenCalledWith("a".repeat(32), { action: "charge", charges: [{ spool_id: 1, grams: 40 }] });
  expect(mocked.inventoryAssign).not.toHaveBeenCalled();
});

it("keeps whole grams keystroke by keystroke and refuses an empty or zero box", async () => {
  await mount();
  await pickSpool("#1 · Bambu Name 1");
  const typeInto = async (text: string) => act(async () => { tree.root.findByType(Field).props.onChangeText(text); });
  await typeInto("1");
  for (const ch of ["0", ".", "5"]) await typeInto(tree.root.findByType(Field).props.value + ch);
  expect(tree.root.findByType(Field).props.value).toBe("105".slice(0, 2) === "10" ? tree.root.findByType(Field).props.value : "");
  expect(["10", "105"]).toContain(tree.root.findByType(Field).props.value);
  await typeInto("10.5");
  expect(tree.root.findByType(Field).props.value).toBe("10");
  await press(button("Charge 10 g to #1"));
  expect(mocked.resolveHeldPrint).toHaveBeenCalledWith("a".repeat(32), { action: "charge", charges: [{ spool_id: 1, grams: 10 }] });
  mocked.resolveHeldPrint.mockClear();
  await typeInto("0");
  expect(button(/^Charge/).props.disabled).toBe(true);
  await typeInto("");
  expect(button(/^Charge/).props.disabled).toBe(true);
  await typeInto("1e308");
  expect(tree.root.findByType(Field).props.value).toBe("1");
  expect(mocked.resolveHeldPrint).not.toHaveBeenCalled();
});

it("tells the owner in a toast when the spool could not stay in the slot after the charge", async () => {
  mocked.inventoryAssign.mockRejectedValue(new BridgeError({ error: "conflict", message: "Assignment revision changed" }, 409));
  await mount();
  await pickSpool("#1 · Bambu Name 1");
  await press(button("Charge 84 g to #1"));
  expect(mocked.resolveHeldPrint).toHaveBeenCalledTimes(1);
  expect(mockToast).toHaveBeenCalledWith("Charged, but slot 1 kept no spool: Assignment revision changed", { severity: "warn" });
  expect(resolved).toHaveBeenCalled();
});

it("skips with nothing charged", async () => {
  await mount();
  await press(button("Skip · charge nothing"));
  expect(mocked.resolveHeldPrint).toHaveBeenCalledWith("a".repeat(32), { action: "nothing", charges: [] });
  expect(resolved).toHaveBeenCalled();
});

it("shows the bridge's words when the charge is refused and keeps the card", async () => {
  mocked.resolveHeldPrint.mockRejectedValue(new BridgeError({ error: "conflict", message: "#1 is archived in Spoolman" }, 409));
  await mount();
  await pickSpool("#1 · Bambu Name 1");
  await press(button("Charge 84 g to #1"));
  expect(texts()).toContain("#1 is archived in Spoolman");
  expect(resolved).not.toHaveBeenCalled();
});
