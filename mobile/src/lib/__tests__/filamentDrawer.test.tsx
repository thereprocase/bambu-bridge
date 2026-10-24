import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

jest.setTimeout(20_000);

import * as api from "../../api/inventory";
import { Button } from "../../components/Button";
import { useLiveStore } from "../../store/live";
import { usePrintersStore } from "../../store/printers";

jest.mock("expo-router", () => ({
  useRouter: () => ({ push: jest.fn() }),
  useFocusEffect: (callback: () => void) => jest.requireActual("react").useEffect(callback, [callback]),
}));
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(() => "P1"), set: jest.fn(), remove: jest.fn(), getBoolean: jest.fn() } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../ws/live", () => ({ LiveConnection: jest.fn(), applyDelta: jest.fn() }));
jest.mock("../../components/Toast", () => ({ useToastStore: () => jest.fn() }));
jest.mock("../../components/NeedsYouCard", () => ({ NeedsYouCard: () => null }));
jest.mock("../../components/RecentPrints", () => ({ RecentPrints: () => null }));
jest.mock("../../api/control", () => ({ amsChange: jest.fn(), amsRfidRead: jest.fn(), unloadFilament: jest.fn() }));
jest.mock("../../api/inventory", () => ({
  inventoryStatus: jest.fn(), inventorySources: jest.fn(), inventorySpools: jest.fn(),
  inventoryAccounting: jest.fn(), refillStatus: jest.fn(),
}));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const FilamentScreen = require("../../../app/(tabs)/filament").default;
const mocked = api as jest.Mocked<typeof api>;

const source = (wire: number, material = "PETG"): api.InventorySource => ({
  printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material, color: "FF0000FF",
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false,
});
const spool = (id: number): api.InventorySpool => ({
  id, archived: false, remaining_weight: 640, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `Name ${id}`, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material: "PETG", color_hex: "1A1A1A", weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});

let tree: ReactTestRenderer;
const buttons = () => tree.root.findAllByType(Button).map((n) => n.props.label as string);
const row = (slot: number) => tree.root.findAll((n) => typeof n.props.onPress === "function" && String(n.props.accessibilityLabel ?? "").startsWith(`Slot ${slot},`))[0];
async function press(node: any) { await act(async () => { await node.props.onPress(); }); }

beforeEach(() => {
  usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
  useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: {
    model: "P1S", phase: "idle", _raw: { gcode_state: "IDLE" },
    ams: { present: true, slots: [
      { physical_slot: 1, type: "PETG", color: "#ff0000", state: "loaded" },
      { physical_slot: 2, type: "PETG", color: "#000000", state: "loaded" },
    ] },
  } } } } as any);
  mocked.inventoryStatus.mockResolvedValue({ configured: true, api_version: 2, provider_id: "prov", revision: 1, accounting_mode: "automatic", native_overlay_enabled: false,
    assignments: [{ printer_id: "P1", source: "ams:abc:tray:0", spool_id: 1, assigned_at: 1, observed_identity: null, wire_slot: 0 }] } as any);
  mocked.inventorySources.mockResolvedValue([source(0), source(1)]);
  mocked.inventorySpools.mockResolvedValue([spool(1)]);
  mocked.inventoryAccounting.mockResolvedValue({ attempts: [] });
  mocked.refillStatus.mockResolvedValue({ backups: [], printer_auto_refill: true, printer_connected: true });
});

it("keeps the actions in a drawer: none on the collapsed row, one slot open at a time", async () => {
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(buttons().filter((l) => l === "Read tag")).toHaveLength(0);
  expect(buttons()).toContain("Read all tags");
  expect(row(1)).toBeDefined();
  expect(row(1).props.accessibilityState).toEqual({ expanded: false });

  await press(row(1));
  expect(buttons().filter((l) => l === "Read tag")).toHaveLength(1);
  expect(buttons()).toContain("Change spool");
  expect(buttons()).toContain("Weigh or archive #1");
  expect(row(1).props.accessibilityState).toEqual({ expanded: true });

  await press(row(2));
  expect(buttons().filter((l) => l === "Read tag")).toHaveLength(1);
  expect(buttons()).toContain("Assign a spool");
  expect(buttons()).not.toContain("Weigh or archive #1");
  expect(row(1).props.accessibilityState).toEqual({ expanded: false });

  await press(row(2));
  expect(buttons().filter((l) => l === "Read tag")).toHaveLength(0);
});

it("says the printer hasn't reported, instead of \"idle\", when its status isn't live", async () => {
  useLiveStore.setState({ printers: { P1: { status: "closed", lastEvent: null, eventLog: [], refills: [], snapshot: {
    model: "P1S", phase: "idle", _raw: { gcode_state: "IDLE" },
    ams: { present: true, slots: [{ physical_slot: 1, type: "PETG", color: "#ff0000", state: "loaded" }] },
  } } } } as any);
  await act(async () => { tree = create(<FilamentScreen />); });
  const texts = tree.root.findAllByType(require("react-native").Text).map((n: any) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));
  expect(texts).toContain("Tags can be read once the printer reports again.");
  expect(texts).not.toContain("Read tags when the printer is idle.");
});
