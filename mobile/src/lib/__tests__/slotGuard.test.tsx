/** The raw slot write explains a refusal in place and writes anyway only when asked. */
import React from "react";
import { Text } from "react-native";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import * as control from "../../api/control";
import { BridgeError, type BridgeEnvelope } from "../../api/errors";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { useLiveStore } from "../../store/live";
import { usePrintersStore } from "../../store/printers";
import { slotGuard, slotWriteDone } from "../slotGuard";

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
const refusal = (context: Record<string, unknown>, extra: Partial<BridgeEnvelope> = {}): BridgeEnvelope => ({
  error: "slot_in_use", message: "Slot 3 holds spool #8 and is part of the slot 3 → slot 4 backup.", context, ...extra,
});

describe("slotGuard words", () => {
  it("names the spool, whether it stays assigned, and where to change it", () => {
    expect(slotGuard(refusal({ wire_slot: 2, spool_id: 8, refill: null, assignment_kept: true }))).toEqual({
      title: "Slot 3 is in use",
      overrideLabel: "Write anyway",
      lines: [
        "Slot 3 holds spool #8. Writing here changes what the printer declares for it.",
        "Spool #8 stays assigned to slot 3.",
        "To change the spool, use Filament → slot 3 → Change spool; the bridge sets the slot to match.",
      ],
    });
    // A new material ends the assignment; the card and the button say so.
    const ends = slotGuard(refusal({ wire_slot: 2, spool_id: 8, assignment_kept: false }), { tray_type: "petg" })!;
    expect(ends.lines[1]).toBe("This changes slot 3 to PETG, which ends spool #8's assignment in that slot. You would assign it again on Filament.");
    expect(ends.overrideLabel).toBe("Write anyway and unassign #8");
    // Without the bridge's word on it, the app does not claim either way.
    expect(slotGuard(refusal({ wire_slot: 2, spool_id: 8 }))!.lines).not.toContainEqual(expect.stringMatching(/assigned to/));
  });

  it("sends a backup member to Remove backup instead", () => {
    expect(slotGuard(refusal({ wire_slot: 3, spool_id: null, refill: { from_wire: 2, to_wire: 3, state: "active" } }))!.lines).toEqual([
      "Slot 4 is part of the slot 3 → slot 4 backup. Writing here makes the backup need you.",
      "To change it, remove the backup first (Filament → slot 3 → Backup → Remove backup); that puts both slots back.",
    ]);
  });

  it("falls back to the bridge's words and ignores other refusals", () => {
    expect(slotGuard(refusal({}, { remediation_hint: "Send with override." }))!.lines)
      .toEqual(["Slot 3 holds spool #8 and is part of the slot 3 → slot 4 backup.", "Send with override."]);
    expect(slotGuard({ error: "conflict", message: "Printer is busy" })).toBeNull();
  });

  it("says what an override did", () => {
    expect(slotWriteDone(undefined)).toBe("Filament setting sent");
    expect(slotWriteDone({ overridden: true, assignment_kept: true, refill_state_expected: "needs_attention" }))
      .toBe("Written anyway · spool still assigned · the backup may need you, check Filament");
    expect(slotWriteDone({ overridden: true, assignment_kept: true, refill_state_expected: null })).toBe("Written anyway · spool still assigned");
    expect(slotWriteDone({ overridden: true, assignment_kept: false })).toBe("Written anyway · spool unassigned, assign it again on Filament");
  });
});

describe("Advanced → AMS filament setting", () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const AdvancedScreen = require("../../../app/(tabs)/advanced").default;
  let tree: ReactTestRenderer;
  const texts = () => tree.root.findAllByType(Text).map((t) => [t.props.children].flat().join(""));
  const button = (label: string) => tree.root.findAllByType(Button).find((b) => b.props.label === label);
  const field = (label: string) => tree.root.findAllByType(Field).find((f) => f.props.label === label)!;
  async function press(label: string) { await act(async () => { await button(label)!.props.onPress(); }); }

  beforeEach(() => {
    jest.clearAllMocks();
    usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
    useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: { model: "P1S", phase: "idle",
      _raw: { ams: { ams: [{ id: "0", tray: [0, 1, 2, 3].map((id) => ({ id: String(id), tray_type: "PLA", tray_info_idx: "GFL99", tray_color: "FF0000FF", nozzle_temp_min: 190, nozzle_temp_max: 240 })) }] } } } } } } as any);
  });
  afterEach(() => act(() => tree?.unmount()));

  it("keeps the refusal on screen and writes anyway only on request", async () => {
    mocked.amsFilamentSetting
      .mockRejectedValueOnce(new BridgeError(refusal({ wire_slot: 2, spool_id: 8, refill: { from_wire: 2, to_wire: 3, state: "active" }, assignment_kept: true }), 409))
      .mockResolvedValueOnce({ sent: {}, guard: { overridden: true, assignment_kept: true, refill_state_expected: "needs_attention" } });
    await act(async () => { tree = create(<AdvancedScreen />); });
    await act(async () => { field("Tray id (0-3)").props.onChangeText("2"); });
    await press("Set filament");
    expect(mocked.amsFilamentSetting).toHaveBeenCalledTimes(1);
    expect(mocked.amsFilamentSetting.mock.calls[0][1]).not.toHaveProperty("override");
    expect(texts()).toContain("Slot 3 is in use");
    expect(texts()).toContain("Spool #8 stays assigned to slot 3.");
    expect(mockShow).not.toHaveBeenCalled();

    await press("Write anyway");
    expect(mocked.amsFilamentSetting).toHaveBeenLastCalledWith("P1", expect.objectContaining({ tray_id: 2, override: true }));
    expect(mockShow).toHaveBeenCalledWith("Written anyway · spool still assigned · the backup may need you, check Filament", { severity: "warn" });
    expect(texts()).not.toContain("Slot 3 is in use");
  });

  it("drops the refusal when a field changes, so Write anyway never sends unseen values", async () => {
    mocked.amsFilamentSetting.mockRejectedValueOnce(new BridgeError(refusal({ wire_slot: 0, spool_id: 1 }), 409));
    await act(async () => { tree = create(<AdvancedScreen />); });
    await press("Set filament");
    expect(button("Write anyway")).toBeDefined();
    await act(async () => { field("Color RRGGBBAA").props.onChangeText("00FF00FF"); });
    expect(button("Write anyway")).toBeUndefined();
  });

  it("writes an unguarded slot as before", async () => {
    mocked.amsFilamentSetting.mockResolvedValueOnce({ sent: {} });
    await act(async () => { tree = create(<AdvancedScreen />); });
    await press("Set filament");
    expect(mockShow).toHaveBeenCalledWith("Filament setting sent", { severity: "success" });
    expect(button("Write anyway")).toBeUndefined();
  });
});

it("names the assignment a material change ends, on the card and the button", async () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const AdvancedScreen = require("../../../app/(tabs)/advanced").default;
  usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
  mocked.amsFilamentSetting.mockReset()
    .mockRejectedValueOnce(new BridgeError(refusal({ wire_slot: 2, spool_id: 8, refill: null, assignment_kept: false }), 409))
    .mockResolvedValueOnce({ sent: {}, guard: { overridden: true, assignment_kept: false, refill_state_expected: null } });
  let tree!: ReactTestRenderer;
  await act(async () => { tree = create(<AdvancedScreen />); });
  const field = (label: string) => tree.root.findAllByType(Field).find((f) => f.props.label === label)!;
  const button = (label: string) => tree.root.findAllByType(Button).find((b) => b.props.label === label);
  // The slot is picked first; its report fills the form, then the owner changes the material.
  await act(async () => { field("Tray id (0-3)").props.onChangeText("2"); });
  await act(async () => { field("Material type").props.onChangeText("PETG"); });
  await act(async () => { await button("Set filament")!.props.onPress(); });
  const shown = tree.root.findAllByType(Text).map((t) => [t.props.children].flat().join(""));
  expect(shown).toContain("This changes slot 3 to PETG, which ends spool #8's assignment in that slot. You would assign it again on Filament.");
  expect(button("Write anyway")).toBeUndefined();
  await act(async () => { await button("Write anyway and unassign #8")!.props.onPress(); });
  expect(mocked.amsFilamentSetting).toHaveBeenLastCalledWith("P1", expect.objectContaining({ tray_type: "PETG", override: true }));
  expect(mockShow).toHaveBeenLastCalledWith("Written anyway · spool unassigned, assign it again on Filament", { severity: "warn" });
  act(() => tree.unmount());
});

describe("slotGuard edges", () => {
  it("puts the spool first and the backup's way out last when both apply", () => {
    expect(slotGuard(refusal({ wire_slot: 2, spool_id: 8, refill: { from_wire: 2, to_wire: 3, state: "active" }, assignment_kept: true }))!.lines).toEqual([
      "Slot 3 holds spool #8. Writing here changes what the printer declares for it.",
      "Spool #8 stays assigned to slot 3.",
      "Slot 3 is part of the slot 3 → slot 4 backup. Writing here makes the backup need you.",
      "To change it, remove the backup first (Filament → slot 3 → Backup → Remove backup); that puts both slots back.",
    ]);
  });

  it("reads only well-formed context, and says 'This slot' without a wire", () => {
    expect(slotGuard(refusal({ spool_id: 8 }))!.title).toBe("This slot is in use");
    expect(slotGuard(refusal({ spool_id: 8 }))!.lines[0]).toBe("This slot holds spool #8. Writing here changes what the printer declares for it.");
    // A string id or half a refill is not trusted; the bridge's own words stand in.
    const odd = slotGuard(refusal({ wire_slot: 2, spool_id: "8", refill: { from_wire: 2 } }))!;
    expect(odd.lines).toEqual(["Slot 3 holds spool #8 and is part of the slot 3 → slot 4 backup."]);
    expect(odd.overrideLabel).toBe("Write anyway");
    expect(slotGuard({ ...refusal({}), context: undefined })!.lines[0]).toBe("Slot 3 holds spool #8 and is part of the slot 3 → slot 4 backup.");
  });

  it("names 'another material' when it does not know the requested one", () => {
    expect(slotGuard(refusal({ wire_slot: 2, spool_id: 8, assignment_kept: false }))!.lines[1])
      .toBe("This changes slot 3 to another material, which ends spool #8's assignment in that slot. You would assign it again on Filament.");
  });

  it("toasts a plain send when the receipt is not an override", () => {
    expect(slotWriteDone({ overridden: false, assignment_kept: true })).toBe("Filament setting sent");
    expect(slotWriteDone(null)).toBe("Filament setting sent");
  });
});

describe("Write anyway, when things go wrong", () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const AdvancedScreen = require("../../../app/(tabs)/advanced").default;
  let tree: ReactTestRenderer;
  const button = (label: string) => tree.root.findAllByType(Button).find((b) => b.props.label === label);
  const texts = () => tree.root.findAllByType(Text).map((t) => [t.props.children].flat().join(""));
  const refused = () => new BridgeError(refusal({ wire_slot: 0, spool_id: 1, assignment_kept: true }), 409);

  beforeEach(() => {
    jest.clearAllMocks();
    usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
    useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: { model: "P1S", phase: "idle",
      _raw: { ams: { ams: [{ id: "0", tray: [{ id: "0", tray_type: "PLA", tray_info_idx: "GFL99", tray_color: "FF0000FF", nozzle_temp_min: 190, nozzle_temp_max: 240 }] }] } } } } } } as any);
  });
  afterEach(() => act(() => tree?.unmount()));

  async function refuse() {
    await act(async () => { tree = create(<AdvancedScreen />); });
    await act(async () => { await button("Set filament")!.props.onPress(); });
    expect(button("Write anyway")).toBeDefined();
  }

  it("does not crash on a refusal with odd context, and shows the bridge's words", async () => {
    mocked.amsFilamentSetting.mockReset().mockRejectedValueOnce(new BridgeError({ error: "slot_in_use", message: "Slot is in use.", context: "nonsense" as any }, 409));
    await act(async () => { tree = create(<AdvancedScreen />); });
    await act(async () => { await button("Set filament")!.props.onPress(); });
    expect(texts()).toContain("This slot is in use");
    expect(texts()).toContain("Slot is in use.");
  });

  it("toasts a second refusal after Write anyway instead of claiming a write", async () => {
    mocked.amsFilamentSetting.mockReset().mockRejectedValueOnce(refused())
      .mockRejectedValueOnce(new BridgeError({ error: "conflict", message: "Printer is printing", remediation_hint: "Wait until it is idle." }, 409));
    await refuse();
    await act(async () => { await button("Write anyway")!.props.onPress(); });
    expect(mockShow).toHaveBeenCalledWith("Printer is printing\nWait until it is idle.", { severity: "danger", ttlMs: 6000 });
    expect(mockShow).not.toHaveBeenCalledWith(expect.stringMatching(/sent|Written/), expect.anything());
    expect(button("Write anyway")).toBeDefined(); // the card stays so the owner can try again
  });

  it("says a network failure on the override plainly, never 'sent'", async () => {
    mocked.amsFilamentSetting.mockReset().mockRejectedValueOnce(refused()).mockRejectedValueOnce(new Error("Network request failed"));
    await refuse();
    await act(async () => { await button("Write anyway")!.props.onPress(); });
    expect(mockShow).toHaveBeenCalledWith("Error: Network request failed", { severity: "danger" });
    expect(mockShow).not.toHaveBeenCalledWith(expect.stringMatching(/sent|Written/), expect.anything());
  });

  it("sends one override for a double tap", async () => {
    let finish!: (v: control.AmsFilamentSettingResult) => void;
    mocked.amsFilamentSetting.mockReset().mockRejectedValueOnce(refused())
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await refuse();
    await act(async () => {
      const press = button("Write anyway")!.props.onPress;
      press(); press();
    });
    expect(mocked.amsFilamentSetting).toHaveBeenCalledTimes(2); // the refused write and one override
    await act(async () => { finish({ sent: {}, guard: { overridden: true, assignment_kept: true } }); });
    expect(mockShow).toHaveBeenCalledTimes(1);
  });
});
