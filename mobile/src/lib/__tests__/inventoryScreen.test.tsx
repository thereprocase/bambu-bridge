import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Text, TextInput } from "react-native";

import * as api from "../../api/inventory";
import { BridgeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { usePrintersStore } from "../../store/printers";

jest.setTimeout(20_000);

const mockPush = jest.fn();
const mockToast = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ push: mockPush }),
  useFocusEffect: (callback: () => void) => jest.requireActual("react").useEffect(callback, [callback]),
}));
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(() => undefined), set: jest.fn(), remove: jest.fn(), getBoolean: jest.fn() } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../ws/live", () => ({ LiveConnection: jest.fn(), applyDelta: jest.fn() }));
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: any) => pick({ show: mockToast }) }));
jest.mock("../../api/inventory", () => ({
  inventoryStatus: jest.fn(), inventorySources: jest.fn(), inventorySpools: jest.fn(), inventoryAssign: jest.fn(),
}));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const InventoryScreen = require("../../screens/InventoryScreen").default;
const mocked = api as jest.Mocked<typeof api>;

const source = (wire: number, over: Partial<api.InventorySource> = {}): api.InventorySource => ({
  printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material: "PETG", color: "FF0000FF",
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false, ...over,
});
const spool = (id: number, vendor: string, name: string, material: string, remaining: number | null, over: Partial<api.InventorySpool> = {}, color = "1A1A1A"): api.InventorySpool => ({
  id, archived: false, remaining_weight: remaining, initial_weight: 1000, spool_weight: null,
  filament: { id, name, vendor: { id: 1, name: vendor, empty_spool_weight: null }, material, color_hex: color, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
  ...over,
});
const shelf = [
  spool(7, "Polymaker", "PETG Galaxy Black", "PETG", 97, { lot_nr: "A7533841419", last_used: "2026-10-26T13:25:29Z" }),
  spool(14, "Polymaker", "PETG Galaxy Black", "PETG", 886, { last_used: "2026-10-26T17:14:22Z" }),
  spool(9, "Elegoo", "Rapid PETG Grey", "PETG", 830, {}, "828282"),
  spool(15, "Polymaker", "PolyTerra PLA Army Red", "PLA", 154, { lot_nr: "A2311180040" }, "B5232B"),
  spool(6, "Polymaker", "PolyLite PC Transparent", "PC", 845, {}, "FFFFFF"),
  spool(8, "Elegoo", "Rapid PETG White", "PETG", 0, { archived: true, last_used: "2026-10-24T21:55:51Z" }, "FFFFFF"),
];
const assigned = (src: string, spool_id: number) => ({ printer_id: "P1", source: src, spool_id, assigned_at: 1, observed_identity: null, wire_slot: 0 });
const status = (...assignments: ReturnType<typeof assigned>[]) =>
  ({ configured: true, api_version: 2, provider_id: "prov", revision: 1, accounting_mode: "automatic", native_overlay_enabled: false, assignments }) as any;

let tree: ReactTestRenderer;
async function mount() { await act(async () => { tree = create(<InventoryScreen />); }); }
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
const buttons = () => tree.root.findAllByType(Button).map((n) => n.props.label as string);
const button = (label: string) => tree.root.findAllByType(Button).find((n) => n.props.label === label)!;
const rows = () => tree.root.findAll((n) => typeof n.props.onPress === "function" && /^#\d+ .*, (open|close) actions$/.test(String(n.props.accessibilityLabel ?? "")));
const rowLabels = () => rows().map((n) => String(n.props.accessibilityLabel).replace(/, (open|close) actions$/, ""));
const row = (id: number) => rows().find((n) => String(n.props.accessibilityLabel).startsWith(`#${id} `))!;
const chip = (label: string) => tree.root.findAll((n) => typeof n.props.onPress === "function" && n.props.accessibilityLabel === label)[0]!;
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }

beforeEach(() => {
  jest.clearAllMocks();
  usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
  mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:1", 14), assigned("external:x", 9)));
  mocked.inventorySources.mockResolvedValue([source(0, { material: "PLA" }), source(1), source(2, { issues: ["source_empty"] }), source(254, { source: "external:x", material: "PETG" })]);
  mocked.inventorySpools.mockResolvedValue(shelf);
  mocked.inventoryAssign.mockResolvedValue({ provider_id: "prov", revision: 2, assignments: [] });
});

describe("summary", () => {
  it("counts the active spools, sums grams by material, and names the one running low", async () => {
    await mount();
    expect(mocked.inventorySpools).toHaveBeenCalledWith("", 0, undefined, 100, true);
    expect(texts()).toContain("5");
    expect(texts()).toContain("2.8 kg left");
    expect(texts()).toContain("Running low: #7 PETG Galaxy Black 97 g");
    const legend = tree.root.findAll((n) => /^PETG, .*, show only this material$/.test(String(n.props.accessibilityLabel ?? "")));
    expect(legend[0].props.accessibilityLabel).toBe("PETG, 1.8 kg, show only this material");
  });

  it("filters to a material when its legend entry is tapped, and back", async () => {
    await mount();
    await press(tree.root.findAll((n) => String(n.props.accessibilityLabel ?? "").startsWith("PLA, "))[0]);
    expect(rowLabels()).toEqual(["#15 · Polymaker PolyTerra PLA Army Red, 154 g left"]);
    await press(tree.root.findAll((n) => String(n.props.accessibilityLabel ?? "").startsWith("PLA, "))[0]);
    expect(rows()).toHaveLength(5);
  });
});

describe("the list", () => {
  it("shows every active spool by material, with where it sits and a lot when known", async () => {
    await mount();
    expect(texts()).toEqual(expect.arrayContaining(["PC · 1", "PETG · 3", "PLA · 1", "SLOT 2", "EXT"]));
    expect(rowLabels()).toEqual([
      "#6 · Polymaker PolyLite PC Transparent, 845 g left",
      "#9 · Elegoo Rapid PETG Grey, 830 g left, in ext",
      "#14 · Polymaker PETG Galaxy Black, 886 g left, in slot 2",
      "#7 · Polymaker PETG Galaxy Black, 97 g left, running low",
      "#15 · Polymaker PolyTerra PLA Army Red, 154 g left",
    ]);
    expect(texts()).toContain("154 g of 1,000 g · lot A2311180040");
  });

  it("searches, narrows by chips, says how many are left, and clears", async () => {
    await mount();
    await act(async () => { tree.root.findAllByType(TextInput)[0].props.onChangeText("galaxy"); });
    expect(rowLabels().map((l) => l.slice(0, 3))).toEqual(["#14", "#7 "]);
    expect(texts()).toContain("2 of 5 spools");
    await press(chip("Vendor: Elegoo, 1"));
    expect(rows()).toHaveLength(0);
    expect(texts()).toContain("No spool matches. Clear the filters or widen the search.");
    await press(tree.root.findAll((n) => n.props.accessibilityRole === "button" && n.findAllByType(Text).some((t) => t.props.children === "Clear"))[0]);
    expect(rows()).toHaveLength(5);
    await press(chip("Where: Off the AMS"));
    expect(rowLabels().map((l) => l.slice(0, 3))).toEqual(["#6 ", "#7 ", "#15"]);
    await press(chip("Where: In the printer"));
    expect(rowLabels().map((l) => l.slice(0, 3))).toEqual(["#9 ", "#14"]);
  });

  it("sorts by what is left, by colour, and by recent use", async () => {
    await mount();
    await press(chip("Sort: Left"));
    expect(rowLabels().map((l) => l.slice(0, 3))).toEqual(["#7 ", "#15", "#9 ", "#6 ", "#14"]);
    expect(texts()).not.toContain("PETG · 3");
    expect(texts()).toContain("running low · 97 g of 1,000 g · PETG · lot A7533841419");
    await press(chip("Sort: Colour"));
    expect(rowLabels()[0]).toMatch(/^#15 /); // the red leads
    await press(chip("Sort: Recent"));
    expect(rowLabels().map((l) => l.slice(0, 3))).toEqual(["#14", "#7 ", "#9 ", "#15", "#6 "]); // never used: the bridge's order
  });
});

describe("a row's drawer", () => {
  it("offers a stored spool the slots it can go into, assigns on tap, and tells the owner", async () => {
    await mount();
    await press(row(15));
    // Slot 1 is untagged PLA with no spool; slot 2 holds #14 and the holder #9; slot 3 is empty.
    expect(buttons()).toEqual(expect.arrayContaining(["Into slot 1 · no spool there yet", "Into slot 2 · replaces #14", "Into external spool · replaces #9", "Details · weigh · archive"]));
    expect(buttons()).not.toContain(expect.stringContaining("slot 3"));
    await press(button("Into slot 1 · no spool there yet"));
    expect(mocked.inventoryAssign).toHaveBeenCalledWith("P1", { source: "ams:abc:tray:0", spool_id: 15, observed_identity: "a".repeat(64) });
    expect(mockToast).toHaveBeenCalledWith("#15 · Polymaker PolyTerra PLA Army Red in slot 1", { severity: "success" });
  });

  it("opens a seated spool's sheet with its slot, a stored one's without", async () => {
    await mount();
    await press(row(14));
    expect(buttons()).not.toContain(expect.stringContaining("Into "));
    await press(button("Details · weigh · archive"));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/spool", params: { id: "14", printer: "P1", source: "ams:abc:tray:1" } });
    await press(row(6));
    await press(button("Details · weigh · archive"));
    expect(mockPush).toHaveBeenLastCalledWith({ pathname: "/spool", params: { id: "6" } });
  });

  it("shows the bridge's refusal and keeps the drawer open", async () => {
    mocked.inventoryAssign.mockRejectedValue(new BridgeError({ message: "Slot 1 changed since you looked." } as any, 409));
    await mount();
    await press(row(15));
    await press(button("Into slot 1 · no spool there yet"));
    expect(mockToast).toHaveBeenCalledWith("Slot 1 changed since you looked.", { severity: "danger" });
    expect(buttons()).toContain("Details · weigh · archive");
  });

  it("tells the owner to pick a printer when none is shown", async () => {
    usePrintersStore.setState({ selectedId: null } as any);
    await mount();
    expect(mocked.inventorySources).not.toHaveBeenCalled();
    await press(row(15));
    expect(texts()).toContain("Pick a printer to put it in a slot.");
  });
});

describe("archived", () => {
  it("folds the archived spools away, and opens them with their last use", async () => {
    await mount();
    expect(texts()).toContain("Archived · 1");
    expect(rowLabels().some((l) => l.startsWith("#8 "))).toBe(false);
    await press(tree.root.findAll((n) => String(n.props.accessibilityLabel ?? "").startsWith("Archived, 1"))[0]);
    expect(rowLabels()).toContain("#8 · Elegoo Rapid PETG White, 0 g left, archived");
    expect(texts()).toContain("archived · last used Oct 24");
    await press(row(8));
    expect(buttons()).toContain("Details");
    expect(buttons()).not.toContain(expect.stringContaining("Into "));
  });
});

describe("when Spoolman is not there", () => {
  it("says the bridge has no Spoolman and points at Settings", async () => {
    mocked.inventoryStatus.mockResolvedValue({ configured: false, api_version: 2, accounting_mode: "off", native_overlay_enabled: false } as any);
    await mount();
    expect(texts()).toContain("Spoolman isn't set up on this bridge. Point the bridge at Spoolman in its settings; spools then show here.");
    await press(button("Settings"));
    expect(mockPush).toHaveBeenCalledWith("/settings");
    expect(mocked.inventorySpools).not.toHaveBeenCalled();
  });

  it("shows the bridge's words when the spool list fails, with Try again", async () => {
    mocked.inventorySpools.mockRejectedValue(new BridgeError({ message: "Spoolman: timeout" } as any, 503));
    await mount();
    expect(texts()).toContain("Spoolman didn't answer. Try again.");
    expect(texts()).not.toContain("spools"); // no summary over an empty, failed list
    mocked.inventorySpools.mockResolvedValue(shelf);
    await press(button("Try again"));
    expect(texts()).not.toContain("Spoolman didn't answer. Try again.");
    expect(rows()).toHaveLength(5);
  });
});
