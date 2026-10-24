import React from "react";

// Mounting a whole screen under jest-expo takes seconds when the host is busy
// (a Python suite running alongside); give these suites room.
jest.setTimeout(20_000);
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Switch, Text } from "react-native";

import { SpoolmanSettings, displayReasonText } from "../../components/SpoolmanSettings";
import * as api from "../../api/inventory";
import { usePrintersStore } from "../../store/printers";

jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(() => null), set: jest.fn(), remove: jest.fn(), getBoolean: jest.fn() } }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../api/inventory", () => ({
  inventoryStatus: jest.fn(),
  inventoryDisplay: jest.fn(),
  saveInventoryDisplay: jest.fn(),
}));
const mocked = api as jest.Mocked<typeof api>;
const display = (over: Partial<api.InventoryDisplayStatus> = {}): api.InventoryDisplayStatus => ({
  provider_id: "prov", revision: 3, global_enabled: false, printers: [], runtime_qualified: true,
  effective_enabled: false, reason: "disabled", projected_slots: [], ...over,
});

let tree: ReactTestRenderer;
async function mount() {
  await act(async () => { tree = create(<SpoolmanSettings />); });
}
const texts = () => tree.root.findAllByType(Text).map((n) => String(Array.isArray(n.props.children) ? n.props.children.join("") : n.props.children));

beforeEach(() => {
  jest.clearAllMocks();
  usePrintersStore.setState({ list: [{ serial: "P1", friendly_name: "P1S" }] } as any);
  mocked.inventoryStatus.mockResolvedValue({ configured: true, api_version: 2, accounting_mode: "automatic", native_overlay_enabled: false } as any);
  mocked.inventoryDisplay.mockResolvedValue(display());
  mocked.saveInventoryDisplay.mockResolvedValue({ provider_id: "prov", revision: 4, global_enabled: true, printers: ["P1"] });
});

it("turns the display on for every registered printer with one switch", async () => {
  await mount();
  const toggle = tree.root.findByType(Switch);
  expect(toggle.props.value).toBe(false);
  expect(texts()).toContain("Off.");
  mocked.inventoryDisplay.mockResolvedValue(display({ global_enabled: true, printers: ["P1"], effective_enabled: true, reason: "candidate" }));
  await act(async () => { await toggle.props.onValueChange(true); });
  expect(mocked.saveInventoryDisplay).toHaveBeenCalledWith({ provider_id: "prov", expected_revision: 3, global_enabled: true, printers: ["P1"] });
  expect(tree.root.findByType(Switch).props.value).toBe(true);
  expect(texts()).toContain("Orca shows them now.");
});

it("explains why Orca isn't showing spools", () => {
  expect(displayReasonText(display({ reason: "inventory_unavailable" }))).toBe("Spoolman is unreachable.");
  expect(displayReasonText(display({ reason: "something_new" }))).toBe("Orca isn't showing them right now.");
});

it("says when the bridge has no Spoolman", async () => {
  mocked.inventoryStatus.mockResolvedValue({ configured: false, api_version: 2, accounting_mode: "disabled", native_overlay_enabled: false });
  await mount();
  expect(texts().some((t) => t.startsWith("This bridge has no Spoolman address."))).toBe(true);
  expect(tree.root.findAllByType(Switch)).toHaveLength(0);
});
