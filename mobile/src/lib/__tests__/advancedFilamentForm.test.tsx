/** Advanced → AMS filament setting starts from the slot and keeps its profile; toasts say what was sent. */
import React from "react";
import { Text } from "react-native";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import * as control from "../../api/control";
import { Button } from "../../components/Button";
import { ChoiceTiles } from "../../components/ChoiceTiles";
import { Field } from "../../components/Field";
import { useLiveStore } from "../../store/live";
import { usePrintersStore } from "../../store/printers";
import { profileNote, profileToSend, reportedDeclaration } from "../slotDeclaration";

const mockShow = jest.fn();
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
jest.mock("../../components/Toast", () => ({ useToastStore: (pick: (s: unknown) => unknown) => pick({ show: mockShow }) }));
jest.mock("../../components/CapabilityCard", () => ({
  CapabilityCard: ({ children }: { children: React.ReactNode }) => children,
  CapabilityList: ({ children }: { children: React.ReactNode }) => children,
}));
jest.mock("../../api/control", () => ({
  amsDrying: jest.fn(), amsFilamentSetting: jest.fn(), amsRfidRead: jest.fn(), amsUserSetting: jest.fn(), calibrate: jest.fn(),
  extrude: jest.fn(), getVersion: jest.fn(() => new Promise(() => {})), setNozzle: jest.fn(), setPrintOption: jest.fn(),
  setXcam: jest.fn(), steppersOff: jest.fn(), sendRawGcode: jest.fn(),
}));

const mocked = control as jest.Mocked<typeof control>;
// The P1S reports temperatures as strings; slot 3 (tray 2) is today's Elegoo PETG on its own profile.
const tray = (id: number, over: Record<string, unknown> = {}) => ({
  id: String(id), tray_type: "PETG", tray_info_idx: "GFG02", tray_color: "ffffffff", nozzle_temp_min: "230", nozzle_temp_max: "260", ...over,
});
function live(trays: Record<string, unknown>[]) {
  useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: {
    model: "P1S", phase: "idle", _raw: { gcode_state: "IDLE", ams: { ams: [{ id: "0", tray: trays }] } },
  } } } } as any);
}

describe("reportedDeclaration", () => {
  it("reads the slot from the raw report, string temperatures and all", () => {
    const snap = { _raw: { ams: { ams: [{ id: "0", tray: [tray(2)] }] } } };
    expect(reportedDeclaration(snap, 0, 2)).toEqual({ tray_type: "PETG", tray_info_idx: "GFG02", tray_color: "FFFFFFFF", nozzle_temp_min: 230, nozzle_temp_max: 260 });
    expect(reportedDeclaration(snap, 0, 1)).toBeNull();
    expect(reportedDeclaration(snap, 1, 2)).toBeNull();
    expect(reportedDeclaration(null, 0, 2)).toBeNull();
  });

  it("tells a missing profile from a manual one, and drops a partial report", () => {
    const of = (t: Record<string, unknown>) => reportedDeclaration({ _raw: { ams: { ams: [{ id: "0", tray: [t] }] } } }, 0, 2);
    const { tray_info_idx: _drop, ...noIdx } = tray(2);
    expect(of(noIdx)!.tray_info_idx).toBeNull();
    expect(of(tray(2, { tray_info_idx: "" }))!.tray_info_idx).toBe("");
    expect(of(tray(2, { tray_type: "" }))).toBeNull();
    expect(of(tray(2, { nozzle_temp_min: "hot" }))).toBeNull();
  });

  it("sends the kept profile, a picked one, or none, and never guesses", () => {
    const current = reportedDeclaration({ _raw: { ams: { ams: [{ id: "0", tray: [tray(2)] }] } } }, 0, 2);
    expect(profileToSend("keep", current)).toBe("GFG02");
    expect(profileToSend("GFL99", current)).toBe("GFL99");
    expect(profileToSend("none", current)).toBe("");
    expect(profileToSend("keep", null)).toBeNull();
    expect(profileToSend("GFX12", current)).toBeNull();
    expect(profileNote("keep", current)).toBe("Sends GFG02, the slot's current profile.");
    expect(profileNote("GFL99", current)).toBe("Sends Generic PLA (GFL99).");
    expect(profileNote("keep", null)).toBe("The printer hasn't reported this slot's profile yet. Pick one to send.");
  });
});

describe("Advanced screen", () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const AdvancedScreen = require("../../../app/(tabs)/advanced").default;
  let tree: ReactTestRenderer;
  const field = (label: string) => tree.root.findAllByType(Field).find((f) => f.props.label === label)!;
  const button = (label: string) => tree.root.findAllByType(Button).find((b) => b.props.label === label)!;
  const profiles = () => tree.root.findAllByType(ChoiceTiles).find((t) => t.props.label === "Filament profile")!;
  const texts = () => tree.root.findAllByType(Text).map((t) => [t.props.children].flat().join(""));
  async function type(label: string, value: string) { await act(async () => { field(label).props.onChangeText(value); }); }
  async function press(label: string) { await act(async () => { await button(label).props.onPress(); }); }
  const sent = () => mocked.amsFilamentSetting.mock.calls.at(-1)![1];

  beforeEach(() => {
    jest.clearAllMocks();
    usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
    live([tray(0, { tray_info_idx: "GFL99", tray_type: "PLA", tray_color: "FF0000FF", nozzle_temp_min: "190", nozzle_temp_max: "240" }), tray(2)]);
    mocked.amsFilamentSetting.mockResolvedValue({ sent: {} });
  });
  afterEach(() => act(() => tree?.unmount()));

  it("starts from the chosen slot and keeps its profile through a colour fix", async () => {
    await act(async () => { tree = create(<AdvancedScreen />); });
    await type("Tray id (0-3)", "2");
    expect(field("Material type").props.value).toBe("PETG");
    expect(field("Nozzle temp min").props.value).toBe("230");
    expect(field("Color RRGGBBAA").props.value).toBe("FFFFFFFF");
    expect(texts()).toContain("Sends GFG02, the slot's current profile.");
    await type("Color RRGGBBAA", "161616FF");
    await press("Set filament");
    // Only the colour changes; the old form sent tray_info_idx "" and red PLA here.
    expect(sent()).toEqual({ ams_id: 0, tray_id: 2, tray_info_idx: "GFG02", tray_color: "161616FF", nozzle_temp_min: 230, nozzle_temp_max: 260, tray_type: "PETG" });
  });

  it("sends a picked preset or no profile when the owner asks", async () => {
    await act(async () => { tree = create(<AdvancedScreen />); });
    await type("Tray id (0-3)", "2");
    await act(async () => { profiles().props.onSelect("GFG99"); });
    await press("Set filament");
    expect(sent().tray_info_idx).toBe("GFG99");
    await act(async () => { profiles().props.onSelect("none"); });
    expect(texts()).toContain("Sends no profile (manual entry).");
    await press("Set filament");
    expect(sent().tray_info_idx).toBe("");
  });

  it("keeps Set off until a profile is known or picked", async () => {
    live([]);
    await act(async () => { tree = create(<AdvancedScreen />); });
    expect(texts()).toContain("The printer hasn't reported this slot's profile yet. Pick one to send.");
    expect(button("Set filament").props.disabled).toBe(true);
    await act(async () => { profiles().props.onSelect("GFL99"); });
    expect(button("Set filament").props.disabled).toBe(false);
    // The report arriving afterwards fills the fields but leaves the owner's pick alone.
    await act(async () => { live([tray(0)]); });
    expect(field("Material type").props.value).toBe("PETG");
    expect(profiles().props.value).toBe("GFL99");
  });

  it("does not overwrite the owner's edits when the slot reports again", async () => {
    await act(async () => { tree = create(<AdvancedScreen />); });
    await type("Tray id (0-3)", "2");
    await type("Nozzle temp max", "270");
    await act(async () => { live([tray(2, { tray_color: "000000FF" })]); });
    expect(field("Nozzle temp max").props.value).toBe("270");
    expect(field("Color RRGGBBAA").props.value).toBe("FFFFFFFF");
  });

  it("toasts what each command sent, not a bare 'Command sent'", async () => {
    mocked.amsRfidRead.mockResolvedValue({ sent: {} });
    await act(async () => { tree = create(<AdvancedScreen />); });
    await press("Re-read RFID");
    await act(async () => {});
    expect(mockShow).toHaveBeenCalledWith("RFID re-read sent", { severity: "success" });
    expect(mockShow).not.toHaveBeenCalledWith("Command sent", expect.anything());
  });
});
