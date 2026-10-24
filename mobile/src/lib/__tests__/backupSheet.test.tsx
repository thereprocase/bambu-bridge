/** The refill sheet shows what each slot will be told before anything is written. */
import React from "react";
import { Text } from "react-native";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

jest.setTimeout(20_000);

import * as api from "../../api/inventory";
import { BridgeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { ChoiceTiles } from "../../components/ChoiceTiles";

const mockBack = jest.fn();
let mockParams: Record<string, string> = { printer: "P1", from: "2" };
const mockShow = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ back: mockBack, push: jest.fn() }),
  useLocalSearchParams: () => mockParams,
}));
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: (s: unknown) => unknown) => pick({ show: mockShow }) }));
jest.mock("../../api/inventory", () => ({
  inventoryStatus: jest.fn(), inventorySources: jest.fn(), inventorySpools: jest.fn(),
  refillStatus: jest.fn(), previewBackup: jest.fn(), putBackup: jest.fn(), deleteBackup: jest.fn(),
}));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const BackupScreen = require("../../../app/backup").default;
const mocked = api as jest.Mocked<typeof api>;

const source = (wire: number, color: string): api.InventorySource => ({
  printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material: "PETG", color,
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false,
});
const spool = (id: number, hex: string): api.InventorySpool => ({
  id, archived: false, remaining_weight: 159, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `Name ${id}`, vendor: { id: 1, name: "Elegoo", empty_spool_weight: null }, material: "PETG", color_hex: hex, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});
const decl = (over: Record<string, unknown> = {}) => ({ tray_type: "PETG", tray_info_idx: "GFG99", tray_color: "FFFFFFFF", nozzle_temp_min: 230, nozzle_temp_max: 260, ...over });
const none: api.RefillStatus = { backups: [], printer_auto_refill: true, printer_connected: true };

let tree: ReactTestRenderer;
const texts = () => tree.root.findAllByType(Text).map((t) => [t.props.children].flat().join(""));
const button = (label: string) => tree.root.findAllByType(Button).find((b) => b.props.label === label);
async function pick(id: string) { await act(async () => { tree.root.findByType(ChoiceTiles).props.onSelect(id); }); }

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = { printer: "P1", from: "2" };
  mocked.inventoryStatus.mockResolvedValue({ configured: true, api_version: 2, provider_id: "prov", revision: 1, accounting_mode: "automatic", native_overlay_enabled: false,
    assignments: [
      { printer_id: "P1", source: "ams:abc:tray:2", spool_id: 8, assigned_at: 1, observed_identity: null, wire_slot: 2 },
      { printer_id: "P1", source: "ams:abc:tray:3", spool_id: 7, assigned_at: 1, observed_identity: null, wire_slot: 3 },
    ] } as any);
  mocked.inventorySources.mockResolvedValue([source(2, "FFFFFFFF"), source(3, "161616FF")]);
  mocked.inventorySpools.mockResolvedValue([spool(8, "FFFFFF"), spool(7, "161616")]);
  mocked.refillStatus.mockResolvedValue(none);
});

it("previews every changed field and writes only on Set", async () => {
  mocked.previewBackup.mockResolvedValue({ digest: "d".repeat(64), body: {
    steps: [
      { wire: 2, writes: false, changes: [] },
      { wire: 3, writes: true, changes: [
        { field: "tray_color", before: "161616FF", after: "FFFFFFFF" },
        { field: "tray_info_idx", before: "GFG02", after: "GFG99" },
        { field: "nozzle_temp_min", before: 220, after: 230 },
        { field: "nozzle_temp_max", before: 270, after: 260 },
      ] },
    ],
  } });
  mocked.putBackup.mockResolvedValue({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "pending", message: null }] });
  await act(async () => { tree = create(<BackupScreen />); });
  await pick("3");
  expect(mocked.previewBackup).toHaveBeenCalledWith("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" });
  expect(mocked.putBackup).not.toHaveBeenCalled();
  const shown = texts();
  expect(shown).toContain("What the printer will be told");
  expect(shown).toContain("Slot 3 · not written");
  expect(shown).toContain("Slot 4 · changes");
  const at = shown.indexOf("Slot 4 · changes");
  expect(shown.slice(at + 1, at + 4)).toEqual([
    "Filament profile: GFG02 → GFG99",
    "Nozzle: 220–270 °C → 230–260 °C",
    "Colour: #161616 → #FFFFFF",
  ]);
  expect(shown.join(" ")).toMatch(/removing the backup puts every changed setting back/);

  await act(async () => { await button("Set slot 4 as backup")!.props.onPress(); });
  expect(mocked.putBackup).toHaveBeenCalledWith("P1", { from_wire: 2, to_wire: 3, preview_digest: "d".repeat(64) });
  // The write went through but the printer has not confirmed: the toast says so.
  expect(mockShow).toHaveBeenCalledWith("Backup set: slot 4 · waiting for the printer to confirm", { severity: "success" });
  expect(mockBack).toHaveBeenCalled();
});

it("reads an older bridge's whole-declaration preview the same way", async () => {
  mocked.previewBackup.mockResolvedValue({ digest: null, body: { plan: { steps: [
    { wire: 2, before: decl(), target: decl() },
    { wire: 3, before: decl({ tray_color: "161616FF" }), target: decl() },
  ] } } });
  await act(async () => { tree = create(<BackupScreen />); });
  await pick("3");
  const shown = texts();
  expect(shown).toContain("Slot 3 · stays as it is");
  expect(shown).toContain("Colour: #161616 → #FFFFFF");
  expect(button("Set slot 4 as backup")!.props.disabled).toBe(false);
});

it("shows the bridge's refusal and keeps Set off", async () => {
  mocked.previewBackup.mockRejectedValue(new BridgeError({ error: "invalid_input", message: "Slot 4 declares GFG02; the backup sets it to GFG99" }, 422));
  await act(async () => { tree = create(<BackupScreen />); });
  await pick("3");
  expect(texts().join(" ")).toMatch(/GFG02/);
  expect(button("Set slot 4 as backup")!.props.disabled).toBe(true);
  expect(mocked.putBackup).not.toHaveBeenCalled();
});

it("lists what a standing backup changed and that removing it puts them back", async () => {
  mocked.refillStatus.mockResolvedValue({
    ...none,
    backups: [{ from_wire: 2, to_wire: 3, state: "pending", message: null }],
    steps: [
      { wire: 2, state: "owned", before: decl(), target: decl() },
      { wire: 3, state: "owned", before: decl({ tray_color: "161616FF" }), target: decl() },
    ],
  });
  await act(async () => { tree = create(<BackupScreen />); });
  const shown = texts();
  expect(shown).toContain("Continues from slot 4 · waiting for the printer to confirm");
  expect(shown).toContain("Colour: #161616 → #FFFFFF");
  expect(shown).not.toContain("Slot 3 · stays as it is");
  expect(shown).toContain("Removing the backup puts these settings back.");
});

it("sends no digest after an older bridge's preview, and re-previews when the slots changed", async () => {
  mocked.previewBackup
    .mockResolvedValueOnce({ digest: "d".repeat(64), body: { steps: [{ wire: 3, writes: true, changes: [{ field: "tray_color", before: "161616FF", after: "FFFFFFFF" }] }] } })
    .mockResolvedValueOnce({ digest: "e".repeat(64), body: { steps: [{ wire: 3, writes: true, changes: [{ field: "tray_color", before: "000000FF", after: "FFFFFFFF" }] }] } });
  mocked.putBackup.mockRejectedValueOnce(new BridgeError({ error: "conflict", message: "Physical refill sources changed; review a new preview" }, 409));
  await act(async () => { tree = create(<BackupScreen />); });
  await pick("3");
  await act(async () => { await button("Set slot 4 as backup")!.props.onPress(); });
  expect(mockBack).not.toHaveBeenCalled();
  expect(texts().join(" ")).toMatch(/review a new preview/);
  // The sheet now shows what Set would write against the changed slot.
  expect(texts()).toContain("Colour: #000000 → #FFFFFF");
  expect(mocked.previewBackup).toHaveBeenCalledTimes(2);
});

it("keeps the bridge's refusal on screen after a failed Remove", async () => {
  mocked.refillStatus.mockResolvedValue({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "active", message: null }] });
  mocked.deleteBackup.mockRejectedValueOnce(new BridgeError({ error: "conflict", message: "The printer accepts this change when it's idle" }, 409));
  await act(async () => { tree = create(<BackupScreen />); });
  await act(async () => { await button("Remove backup")!.props.onPress(); });
  expect(texts()).toContain("The printer accepts this change when it's idle");
  expect(mockBack).not.toHaveBeenCalled();
});

it("blocks a second chain: another slot's sheet offers nothing while one stands", async () => {
  mockParams = { printer: "P1", from: "0" };
  mocked.inventorySources.mockResolvedValue([source(0, "FFFFFFFF"), source(2, "FFFFFFFF"), source(3, "161616FF")]);
  mocked.refillStatus.mockResolvedValue({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "active", message: null }] });
  await act(async () => { tree = create(<BackupScreen />); });
  expect(texts()).toContain("Backup in use: slot 3 → slot 4. Remove it first.");
  expect(tree.root.findAllByType(ChoiceTiles)).toHaveLength(0);
});

it("sends one backup for a double tap on Set", async () => {
  mocked.previewBackup.mockResolvedValue({ digest: null, body: { steps: [{ wire: 3, writes: true, changes: [{ field: "tray_color", before: "161616FF", after: "FFFFFFFF" }] }] } });
  let finish!: (v: api.RefillStatus) => void;
  mocked.putBackup.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  await act(async () => { tree = create(<BackupScreen />); });
  await pick("3");
  await act(async () => { const press = button("Set slot 4 as backup")!.props.onPress; press(); press(); });
  expect(mocked.putBackup).toHaveBeenCalledTimes(1);
  await act(async () => { finish({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "active", message: null }] }); });
  expect(mockShow).toHaveBeenCalledTimes(1);
});

it("says a removal waits for an idle printer when the bridge queues it", async () => {
  mocked.refillStatus.mockResolvedValue({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "active", message: null }] });
  mocked.deleteBackup.mockResolvedValueOnce({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "restoring", message: null }] });
  await act(async () => { tree = create(<BackupScreen />); });
  await act(async () => { await button("Remove backup")!.props.onPress(); });
  expect(mockShow).toHaveBeenCalledWith("Removing the backup: slot 3 and slot 4 go back when the printer is idle", { severity: "success" });
});

it("says a removal is done only when the chain is gone", async () => {
  mocked.refillStatus.mockResolvedValue({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "active", message: null }] });
  mocked.deleteBackup.mockResolvedValueOnce(none);
  await act(async () => { tree = create(<BackupScreen />); });
  await act(async () => { await button("Remove backup")!.props.onPress(); });
  expect(mockShow).toHaveBeenCalledWith("slot 3 has no backup now", { severity: "success" });
  mocked.deleteBackup.mockResolvedValueOnce({ ...none, backups: [{ from_wire: 2, to_wire: 3, state: "needs_attention", message: "The printer reports a different product on slot 4" }] });
  act(() => tree.unmount());
  await act(async () => { tree = create(<BackupScreen />); });
  await act(async () => { await button("Remove backup")!.props.onPress(); });
  expect(mockShow).toHaveBeenLastCalledWith("Removing the backup needs you: The printer reports a different product on slot 4", { severity: "warn" });
});
