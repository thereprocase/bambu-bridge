import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Text } from "react-native";

import * as api from "../../api/inventory";
import { Button } from "../../components/Button";
import { useLiveStore } from "../../store/live";
import { usePrintersStore } from "../../store/printers";

jest.setTimeout(20_000);

const mockPush = jest.fn();
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
// A working store: the stores read it as their modules load, before this file's own constants exist.
jest.mock("../../lib/kv", () => {
  const store: Record<string, string> = {};
  return { store, kv: {
    getString: (key: string) => store[key],
    set: (key: string, value: string) => { store[key] = String(value); },
    remove: (key: string) => { delete store[key]; },
    getBoolean: () => undefined,
  } };
});
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../ws/live", () => ({ LiveConnection: jest.fn(), applyDelta: jest.fn() }));
jest.mock("../../components/Toast", () => ({ useToastStore: () => jest.fn() }));
jest.mock("../../components/NeedsYouCard", () => ({ NeedsYouCard: () => null }));
jest.mock("../../components/RecentPrints", () => ({ RecentPrints: () => null }));
jest.mock("../../api/control", () => ({ amsChange: jest.fn(), amsRfidRead: jest.fn(), unloadFilament: jest.fn() }));
jest.mock("../../api/inventory", () => ({
  inventoryStatus: jest.fn(), inventorySources: jest.fn(), inventorySpools: jest.fn(),
  inventoryAccounting: jest.fn(), refillStatus: jest.fn(), archiveSpool: jest.fn(),
}));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const FilamentScreen = require("../../../app/(tabs)/filament").default;
const mocked = api as jest.Mocked<typeof api>;
// eslint-disable-next-line @typescript-eslint/no-require-imports
const mockStore: Record<string, string> = require("../../lib/kv").store;

const source = (wire: number, last: number | null = null): api.InventorySource => ({
  printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material: "PETG", color: "FF0000FF",
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false, last_spool_id: last,
});
const spool = (id: number, over: Partial<api.InventorySpool> = {}): api.InventorySpool => ({
  id, archived: false, remaining_weight: 120 + id, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `PETG Colour ${id}`, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material: "PETG", color_hex: "1A1A1A", weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
  ...over,
});
const assigned = (source: string, spool_id: number) => ({ printer_id: "P1", source, spool_id, assigned_at: 1, observed_identity: null, wire_slot: 0 });
const status = (...assignments: ReturnType<typeof assigned>[]) =>
  ({ configured: true, api_version: 2, provider_id: "prov", revision: 1, accounting_mode: "automatic", native_overlay_enabled: false, assignments }) as any;

let tree: ReactTestRenderer;
async function mount() { await act(async () => { tree = create(<FilamentScreen />); }); }
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
const buttons = () => tree.root.findAllByType(Button).map((n) => n.props.label as string);
const button = (label: string) => tree.root.findAllByType(Button).find((n) => n.props.label === label)!;
const shelfRows = () => tree.root.findAll((n) => typeof n.props.onPress === "function" && / weigh or archive$/.test(String(n.props.accessibilityLabel ?? "")));
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }

beforeEach(() => {
  jest.clearAllMocks();
  for (const key of Object.keys(mockStore)) delete mockStore[key];
  usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
  useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: {
    model: "P1S", phase: "idle", _raw: { gcode_state: "IDLE" },
    ams: { present: true, slots: [
      { physical_slot: 1, type: "PETG", color: "#ff0000", state: "loaded" },
      { physical_slot: 2, type: "PETG", color: "#000000", state: "loaded" },
    ] },
  } } } } as any);
  mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1)));
  mocked.inventorySources.mockResolvedValue([source(0), source(1)]);
  mocked.inventorySpools.mockResolvedValue([spool(1), spool(8), spool(3), spool(4, { archived: true })]);
  mocked.inventoryAccounting.mockResolvedValue({ attempts: [] });
  mocked.refillStatus.mockResolvedValue({ backups: [], printer_auto_refill: true, printer_connected: true });
  mocked.archiveSpool.mockResolvedValue(spool(8, { archived: true }));
});

describe("Off the AMS", () => {
  it("lists the unarchived spools in no slot, with number, product and grams", async () => {
    await mount();
    expect(texts()).toContain("Off the AMS");
    expect(shelfRows().map((n) => n.props.accessibilityLabel)).toEqual([
      "#8 · Bambu PETG Colour 8, 128 g left, weigh or archive",
      "#3 · Bambu PETG Colour 3, 123 g left, weigh or archive",
    ]);
  });

  it("opens the weigh-or-archive sheet without a slot, so it never unassigns one", async () => {
    await mount();
    await press(shelfRows()[0]);
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/spool", params: { id: "8" } });
  });

  it("reads every page, so a spool past the first hundred is listed too", async () => {
    const first = Array.from({ length: 100 }, (_, i) => spool(100 + i));
    mocked.inventoryStatus.mockResolvedValue(status(...first.map((s) => assigned(`ams:x:tray:${s.id}`, s.id))));
    mocked.inventorySpools.mockImplementation(async (_q, offset = 0) => (offset === 0 ? first : [spool(8)]));
    await mount();
    expect(mocked.inventorySpools).toHaveBeenCalledWith("", 0, undefined, 100);
    expect(mocked.inventorySpools).toHaveBeenCalledWith("", 100, undefined, 100);
    expect(shelfRows().map((n) => n.props.accessibilityLabel)).toEqual(["#8 · Bambu PETG Colour 8, 128 g left, weigh or archive"]);
  });

  it("is hidden when every unarchived spool sits in a slot", async () => {
    mocked.inventorySpools.mockResolvedValue([spool(1), spool(4, { archived: true })]);
    await mount();
    expect(texts()).not.toContain("Off the AMS");
    expect(shelfRows()).toHaveLength(0);
  });
});

describe("a spool that came out of a slot", () => {
  beforeEach(() => {
    mocked.inventorySources.mockResolvedValue([source(0), source(1, 8)]);
  });

  it("asks on that slot, with both answers", async () => {
    await mount();
    expect(texts()).toContain("#8 · Bambu PETG Colour 8 came out of this slot");
    expect(buttons()).toEqual(expect.arrayContaining(["Empty – archive it", "Back on the shelf"]));
  });

  it("archives it in Spoolman on Empty, and the question goes away", async () => {
    await mount();
    mocked.inventorySpools.mockResolvedValue([spool(1), spool(3)]);
    await press(button("Empty – archive it"));
    expect(mocked.archiveSpool).toHaveBeenCalledWith(8);
    expect(texts()).not.toContain("#8 · Bambu PETG Colour 8 came out of this slot");
  });

  it("keeps the question answered on this device after Back on the shelf", async () => {
    await mount();
    await press(button("Back on the shelf"));
    expect(mocked.archiveSpool).not.toHaveBeenCalled();
    expect(texts()).not.toContain("#8 · Bambu PETG Colour 8 came out of this slot");
    expect(JSON.parse(mockStore["filament.shelved"])).toEqual(["P1|ams:abc:tray:1|8"]);
    await act(async () => { tree.unmount(); });
    await mount();
    expect(texts()).not.toContain("#8 · Bambu PETG Colour 8 came out of this slot");
    expect(shelfRows().map((n) => n.props.accessibilityLabel)).toContain("#8 · Bambu PETG Colour 8, 128 g left, weigh or archive");
  });

  it("asks again once the spool has been back in a slot", async () => {
    mockStore["filament.shelved"] = JSON.stringify(["P1|ams:abc:tray:1|8"]);
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), assigned("ams:abc:tray:3", 8)));
    await mount();
    expect(JSON.parse(mockStore["filament.shelved"])).toEqual([]);
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1)));
    await act(async () => { tree.unmount(); });
    await mount();
    expect(texts()).toContain("#8 · Bambu PETG Colour 8 came out of this slot");
  });

  it("says nothing while the spool sits in another slot", async () => {
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), assigned("ams:abc:tray:3", 8)));
    await mount();
    expect(texts()).not.toContain("#8 · Bambu PETG Colour 8 came out of this slot");
  });
});

describe("a swap made while the app was closed", () => {
  const SLOT3 = "ams:abc:tray:2";
  const question = "#8 · Bambu PETG Colour 8 came out of this slot";
  const pla = (id: number) => spool(id, { filament: { ...spool(id).filament, name: `PLA ${id}`, material: "PLA" } });
  const reopen = async () => { await act(async () => { tree.unmount(); }); await mount(); };

  beforeEach(async () => {
    useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: {
      model: "P1S", phase: "idle", _raw: { gcode_state: "IDLE" },
      ams: { present: true, slots: [
        { physical_slot: 1, type: "PETG", color: "#ff0000", state: "loaded" },
        { physical_slot: 2, type: "PETG", color: "#000000", state: "loaded" },
        { physical_slot: 3, type: "PETG", color: "#ffffff", state: "loaded" },
      ] },
    } } } } as any);
    mocked.inventorySpools.mockResolvedValue([spool(1), spool(8), spool(9), pla(12), spool(20)]);
    // Untagged Elegoo White #8 seen in slot 3.
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), assigned(SLOT3, 8)));
    mocked.inventorySources.mockResolvedValue([source(0), source(1), source(2, 8)]);
    await mount();
    expect(texts()).not.toContain(question);
  });

  it("asks about #8 after the slot emptied and #9 of the same product was assigned", async () => {
    // The bridge's history for that identity and slot now names #9 only.
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), assigned(SLOT3, 9)));
    mocked.inventorySources.mockResolvedValue([source(0), source(1), source(2, 9)]);
    await reopen();
    expect(texts()).toContain(question);
    await reopen(); // still asked until answered
    expect(texts()).toContain(question);
    await press(button("Back on the shelf"));
    expect(texts()).not.toContain(question);
    await reopen();
    expect(texts()).not.toContain(question);
  });

  it("asks on the emptied slot before anything else is loaded", async () => {
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1)));
    mocked.inventorySources.mockResolvedValue([source(0), source(1), { ...source(2), issues: ["source_empty"] }]);
    await reopen();
    expect(texts()).toContain(question);
  });

  it("asks after a swap to another material", async () => {
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), assigned(SLOT3, 12)));
    mocked.inventorySources.mockResolvedValue([source(0), source(1), { ...source(2, null), material: "PLA" }]);
    await reopen();
    expect(texts()).toContain(question);
  });

  it("asks after a swap to a tagged spool", async () => {
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), { ...assigned(SLOT3, 20), origin: "rfid_new" } as any));
    mocked.inventorySources.mockResolvedValue([source(0), source(1), { ...source(2, null), tagged: true }]);
    await reopen();
    expect(texts()).toContain(question);
    await press(button("Empty – archive it"));
    expect(mocked.archiveSpool).toHaveBeenCalledWith(8);
  });

  it("says nothing when #8 moved to another slot, and forgets it", async () => {
    mocked.inventoryStatus.mockResolvedValue(status(assigned("ams:abc:tray:0", 1), assigned("ams:abc:tray:1", 8)));
    mocked.inventorySources.mockResolvedValue([source(0), source(1), source(2)]);
    await reopen();
    expect(texts()).not.toContain(question);
    expect(JSON.parse(mockStore["filament.seen"])).toEqual({
      "P1|ams:abc:tray:0": { seen: 1, out: null },
      "P1|ams:abc:tray:1": { seen: 8, out: null },
    });
  });
});
