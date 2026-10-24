import React from "react";

// Mounting a whole screen under jest-expo takes seconds when the host is busy
// (a Python suite running alongside); give these suites room.
jest.setTimeout(20_000);
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Text } from "react-native";

import WrongSpoolScreen from "../../../app/wrong-spool";
import * as api from "../../api/inventory";
import { BridgeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";

const mockBack = jest.fn();
const mockToast = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ back: mockBack, push: jest.fn() }),
  useLocalSearchParams: () => ({ attempt: "a".repeat(32), from: "5", grams: "83.7", title: "Benchy" }),
}));
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: any) => pick({ show: mockToast }) }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../api/inventory", () => ({ inventorySpool: jest.fn(), inventorySpools: jest.fn(), reassignCharge: jest.fn() }));
const mocked = api as jest.Mocked<typeof api>;
const spool = (id: number, material: string): api.InventorySpool => ({
  id, archived: false, remaining_weight: 400, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `Name ${id}`, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material, color_hex: null, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});

let tree: ReactTestRenderer;
async function mount() { await act(async () => { tree = create(<WrongSpoolScreen />); }); }
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
const button = (label: string | RegExp) => tree.root.findAllByType(Button).find((n) => (typeof label === "string" ? n.props.label === label : label.test(n.props.label)))!;
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }
const pick = async (label: string) => press(tree.root.findAll((n) => typeof n.props.onPress === "function" && String(n.props.accessibilityLabel ?? "").startsWith(label))[0]);

beforeEach(() => {
  jest.clearAllMocks();
  mocked.inventorySpool.mockResolvedValue(spool(5, "PETG"));
  mocked.inventorySpools.mockResolvedValue([spool(2, "PLA"), spool(12, "PETG"), spool(5, "PETG")]);
  mocked.reassignCharge.mockResolvedValue({} as any);
});

it("moves all of it by default, same material first, the origin excluded", async () => {
  await mount();
  expect(texts()).toContain("84 g charged to #5 · Bambu Name 5");
  expect(mocked.inventorySpools).toHaveBeenCalledWith("", 0, "PETG");
  const labels = tree.root.findAll((n) => typeof n.props.onPress === "function" && /^#\d+ /.test(String(n.props.accessibilityLabel ?? ""))).map((n) => n.props.accessibilityLabel.split(",")[0]);
  expect(labels).toEqual(["#12 · Bambu Name 12", "#2 · Bambu Name 2"]);
  await pick("#12 ");
  expect(tree.root.findByType(Field).props.value).toBe("84");
  await press(button("Move 84 g from #5 to #12"));
  // All of it: no figure, so the bridge moves the exact 83.7 g rather than a rounded 84 it would refuse.
  expect(mocked.reassignCharge).toHaveBeenCalledWith("a".repeat(32), { from_spool_id: 5, to_spool_id: 12 });
  expect(mockToast).toHaveBeenCalledWith("Moved 84 g from #5 to #12", { severity: "success" });
  expect(mockBack).toHaveBeenCalled();
});

it("takes a smaller whole figure and refuses more than the print put on the spool", async () => {
  await mount();
  await pick("#12 ");
  await act(async () => { tree.root.findByType(Field).props.onChangeText("40.5"); });
  expect(tree.root.findByType(Field).props.value).toBe("40");
  expect(button("Move 40 g from #5 to #12").props.disabled).toBe(false);
  await press(button("Move 40 g from #5 to #12"));
  expect(mocked.reassignCharge).toHaveBeenLastCalledWith("a".repeat(32), { from_spool_id: 5, to_spool_id: 12, grams: 40 });
  await act(async () => { tree.root.findByType(Field).props.onChangeText("90"); });
  expect(button(/^Move/).props.disabled).toBe(true);
  expect(tree.root.findByType(Field).props.error).toBe("This print put 84 g on #5.");
});

it("shows the bridge's words when the move is refused", async () => {
  mocked.reassignCharge.mockRejectedValue(new BridgeError({ error: "conflict", message: "#12 is archived in Spoolman" }, 409));
  await mount();
  await pick("#12 ");
  await press(button("Move 84 g from #5 to #12"));
  expect(texts()).toContain("#12 is archived in Spoolman");
  expect(mockBack).not.toHaveBeenCalled();
});
