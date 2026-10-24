/**
 * 2026-10-24: slot 3 said "Backup: slot 4 · waiting for the printer to confirm"
 * while slot 4 said "Backs up slot 3" (bare, before "confirmed" was a word) and the bridge already reported `active`
 * (printer backup mask 12). These tests replay that: the bridge answers
 * `pending` right after the write, then `active` once the printer reports the
 * mask (6-10 s later in the N9 capture of 2026-10-22).
 */
import React from "react";
import { AppState, Text } from "react-native";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

jest.setTimeout(20_000);

import * as api from "../../api/inventory";
import { useLiveStore } from "../../store/live";
import { usePrintersStore } from "../../store/printers";

// Focus the test can take away: blur runs the effects' cleanups, focus runs them again.
const mockFocus = { value: true, listeners: new Set<(focused: boolean) => void>() };
function setFocused(focused: boolean) { mockFocus.value = focused; mockFocus.listeners.forEach((l) => l(focused)); }
jest.mock("expo-router", () => ({
  useRouter: () => ({ push: jest.fn() }),
  useFocusEffect: (callback: () => void | (() => void)) => {
    const R = jest.requireActual("react");
    const [focused, setLocal] = R.useState(mockFocus.value);
    R.useEffect(() => { mockFocus.listeners.add(setLocal); return () => { mockFocus.listeners.delete(setLocal); }; }, []);
    R.useEffect(() => (focused ? callback() : undefined), [callback, focused]);
  },
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

const source = (wire: number): api.InventorySource => ({
  printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material: "PETG", color: "FFFFFFFF",
  observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false,
});
const spool = (id: number): api.InventorySpool => ({
  id, archived: false, remaining_weight: 159, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `Name ${id}`, vendor: { id: 1, name: "Elegoo", empty_spool_weight: null }, material: "PETG", color_hex: "FFFFFF", weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});
const refill = (state: string, over: Partial<api.RefillStatus> = {}): api.RefillStatus => ({
  backups: [{ from_wire: 2, to_wire: 3, state, message: null }], printer_auto_refill: true, printer_connected: true, ...over,
});
const assign = (wire: number, spoolId: number) => ({ printer_id: "P1", source: `ams:abc:tray:${wire}`, spool_id: spoolId, assigned_at: 1, observed_identity: null, wire_slot: wire });

let tree: ReactTestRenderer;
/** Every line of text on the slot's row, in order. */
function rowText(slot: number): string[] {
  const pressable = tree.root.findAll((n) => typeof n.props.onPress === "function" && String(n.props.accessibilityLabel ?? "").startsWith(`Slot ${slot},`))[0];
  return pressable.findAllByType(Text).map((t) => [t.props.children].flat().join(""));
}
const chainOf = (slot: number) => rowText(slot).find((l) => /^(Backup:|Backs up)/.test(l)) ?? null;
async function tick(ms: number) { await act(async () => { jest.advanceTimersByTime(ms); }); }

function seed(assignments = [assign(2, 8), assign(3, 7)]) {
  usePrintersStore.setState({ selectedId: "P1", list: [{ serial: "P1", friendly_name: "P1S", model: "P1S", connected: true }] } as any);
  useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], refills: [], snapshot: {
    model: "P1S", phase: "idle", _raw: { gcode_state: "IDLE" },
    ams: { present: true, slots: [1, 2, 3, 4].map((n) => ({ physical_slot: n, type: "PETG", color: "#ffffff", state: "loaded" })) },
  } } } } as any);
  mocked.inventoryStatus.mockResolvedValue({ configured: true, api_version: 2, provider_id: "prov", revision: 1, accounting_mode: "automatic", native_overlay_enabled: false,
    assignments } as any);
  mocked.inventorySources.mockResolvedValue([0, 1, 2, 3].map(source));
  mocked.inventorySpools.mockResolvedValue([spool(8), spool(7)]);
  mocked.inventoryAccounting.mockResolvedValue({ attempts: [] });
}

beforeEach(() => { jest.useFakeTimers(); mocked.refillStatus.mockReset(); mockFocus.value = true; seed(); });
afterEach(() => { act(() => tree?.unmount()); jest.useRealTimers(); });

it("says the same thing on both rows of one chain", async () => {
  mocked.refillStatus.mockResolvedValue(refill("pending"));
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(chainOf(3)).toBe("Backup: slot 4 · waiting for the printer to confirm");
  expect(chainOf(4)).toBe("Backs up slot 3 · waiting for the printer to confirm");
});

it("says confirmed on both rows once the printer runs the chain, without leaving the tab", async () => {
  mocked.refillStatus.mockResolvedValueOnce(refill("pending")).mockResolvedValue(refill("active"));
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(chainOf(3)).toMatch(/waiting/);
  await tick(5_000);
  expect(chainOf(3)).toBe("Backup: slot 4 · confirmed");
  expect(chainOf(4)).toBe("Backs up slot 3 · confirmed");
  // A settled chain needs no polling.
  const calls = mocked.refillStatus.mock.calls.length;
  await tick(30_000);
  expect(mocked.refillStatus.mock.calls.length).toBe(calls);
});

it("keeps the last answer when one poll fails", async () => {
  mocked.refillStatus.mockResolvedValueOnce(refill("pending")).mockRejectedValueOnce(new Error("timeout")).mockResolvedValue(refill("pending"));
  await act(async () => { tree = create(<FilamentScreen />); });
  await tick(5_000);
  expect(chainOf(3)).toBe("Backup: slot 4 · waiting for the printer to confirm");
  expect(chainOf(4)).toBe("Backs up slot 3 · waiting for the printer to confirm");
});

it("names the real reason when the printer will never confirm", async () => {
  mocked.refillStatus.mockResolvedValue(refill("pending", {
    printer_auto_refill: false,
    backups: [{ from_wire: 2, to_wire: 3, state: "pending", message: "Turn on AMS auto-refill on the printer" }],
  }));
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(chainOf(3)).toBe("Backup: slot 4 · turn on AMS auto-refill on the printer");
  expect(chainOf(4)).toBe("Backs up slot 3 · turn on AMS auto-refill on the printer");

  mocked.refillStatus.mockResolvedValue(refill("pending", { printer_connected: false }));
  await tick(5_000);
  expect(chainOf(3)).toBe("Backup: slot 4 · printer offline");
  expect(chainOf(4)).toBe("Backs up slot 3 · printer offline");
});

it("shows the chain on a backup slot that has no spool assigned yet", async () => {
  seed([assign(2, 8)]);
  mocked.refillStatus.mockResolvedValue(refill("active"));
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(chainOf(3)).toBe("Backup: slot 4 · confirmed");
  expect(chainOf(4)).toBe("Backs up slot 3 · confirmed");
});

it("holds the last confirmed state through a quiet printer instead of going back to waiting", async () => {
  // Seen live 2026-10-24: idle printer, no frame for a while, bridge drops to pending on staleness alone.
  const quiet = refill("pending", {
    printer_auto_refill: null, last_reported_auto_refill: true, eligibility_fresh: false,
    eligibility_age_seconds: 140, reported_backup_masks: [12],
  });
  mocked.refillStatus.mockResolvedValueOnce(refill("active")).mockResolvedValueOnce(quiet).mockResolvedValue(refill("active"));
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(chainOf(3)).toBe("Backup: slot 4 · confirmed");
  // Active does not poll; the next focus refresh is what sees the stale answer.
  await act(async () => { tree.update(<FilamentScreen key="refocus" />); });
  expect(chainOf(3)).toBe("Backup: slot 4 · last confirmed 2 min ago");
  expect(chainOf(4)).toBe("Backs up slot 3 · last confirmed 2 min ago");
  // A fresh frame: active again, polling stops.
  await tick(5_000);
  expect(chainOf(3)).toBe("Backup: slot 4 · confirmed");
  expect(chainOf(4)).toBe("Backs up slot 3 · confirmed");
});

describe("polling lifecycle", () => {
  it("stops asking once the screen is gone", async () => {
    mocked.refillStatus.mockResolvedValue(refill("pending"));
    await act(async () => { tree = create(<FilamentScreen />); });
    await tick(5_000);
    const calls = mocked.refillStatus.mock.calls.length;
    act(() => tree.unmount());
    await tick(30_000);
    expect(mocked.refillStatus.mock.calls.length).toBe(calls);
  });

  it("stops while the tab is out of focus and picks up again on return", async () => {
    mocked.refillStatus.mockResolvedValue(refill("pending"));
    await act(async () => { tree = create(<FilamentScreen />); });
    await act(async () => { setFocused(false); });
    const blurred = mocked.refillStatus.mock.calls.length;
    await tick(20_000);
    expect(mocked.refillStatus.mock.calls.length).toBe(blurred);
    await act(async () => { setFocused(true); }); // the focus refresh
    expect(mocked.refillStatus.mock.calls.length).toBe(blurred + 1);
    await tick(5_000);
    expect(mocked.refillStatus.mock.calls.length).toBe(blurred + 2);
  });

  it("sends no second request while a slow one is still out", async () => {
    let slow!: (value: api.RefillStatus) => void;
    mocked.refillStatus
      .mockResolvedValueOnce(refill("pending"))
      .mockImplementationOnce(() => new Promise((resolve) => { slow = resolve; }))
      .mockResolvedValue(refill("pending"));
    await act(async () => { tree = create(<FilamentScreen />); });
    await tick(5_000); // poll 1 goes out and hangs
    await tick(5_000);
    await tick(5_000);
    expect(mocked.refillStatus).toHaveBeenCalledTimes(2);
    await act(async () => { slow(refill("active")); });
    expect(chainOf(3)).toBe("Backup: slot 4 · confirmed");
    expect(chainOf(4)).toBe("Backs up slot 3 · confirmed");
  });

  it("drops an answer that lands after the printer was switched", async () => {
    usePrintersStore.setState({ list: [
      { serial: "P1", friendly_name: "P1S", model: "P1S", connected: true },
      { serial: "P2", friendly_name: "Other", model: "P1S", connected: true },
    ] } as any);
    useLiveStore.setState((st: any) => ({ printers: { ...st.printers, P2: st.printers.P1 } }));
    let late!: (value: api.RefillStatus) => void;
    mocked.refillStatus.mockImplementation((printer: string) =>
      printer === "P1" && mocked.refillStatus.mock.calls.length > 1
        ? new Promise((resolve) => { late = resolve; })
        : Promise.resolve(printer === "P1" ? refill("pending") : { ...refill("pending"), backups: [{ from_wire: 0, to_wire: 1, state: "pending", message: null }] }));
    await act(async () => { tree = create(<FilamentScreen />); });
    await tick(5_000); // P1's poll hangs
    await act(async () => { usePrintersStore.setState({ selectedId: "P2" } as any); });
    expect(chainOf(1)).toBe("Backup: slot 2 · waiting for the printer to confirm");
    // P1's answer arrives late; it must not paint P2's rows.
    await act(async () => { late(refill("active")); });
    expect(chainOf(1)).toBe("Backup: slot 2 · waiting for the printer to confirm");
    expect(chainOf(3)).toBeNull();
  });

  it("drops a slow refresh for the printer before a switch", async () => {
    usePrintersStore.setState({ list: [
      { serial: "P1", friendly_name: "P1S", model: "P1S", connected: true },
      { serial: "P2", friendly_name: "Other", model: "P1S", connected: true },
    ] } as any);
    useLiveStore.setState((st: any) => ({ printers: { ...st.printers, P2: st.printers.P1 } }));
    let slowP1!: (value: api.RefillStatus) => void;
    mocked.refillStatus.mockImplementation((printer: string) =>
      printer === "P1"
        ? new Promise((resolve) => { slowP1 = resolve; })
        : Promise.resolve({ ...refill("active"), backups: [{ from_wire: 0, to_wire: 1, state: "active", message: null }] }));
    await act(async () => { tree = create(<FilamentScreen />); });
    await act(async () => { usePrintersStore.setState({ selectedId: "P2" } as any); });
    expect(chainOf(1)).toBe("Backup: slot 2 · confirmed");
    await act(async () => { slowP1(refill("pending")); });
    expect(chainOf(1)).toBe("Backup: slot 2 · confirmed");
    expect(chainOf(3)).toBeNull();
  });

  it("keeps one request per interval through a run of failures, and the last answer", async () => {
    mocked.refillStatus.mockResolvedValueOnce(refill("pending")).mockRejectedValue(new Error("timeout"));
    await act(async () => { tree = create(<FilamentScreen />); });
    for (let i = 0; i < 6; i++) await tick(5_000);
    expect(mocked.refillStatus).toHaveBeenCalledTimes(7);
    expect(chainOf(3)).toBe("Backup: slot 4 · waiting for the printer to confirm");
    expect(chainOf(4)).toBe("Backs up slot 3 · waiting for the printer to confirm");
  });

  it("asks nothing while the app is in the background", async () => {
    const set = (state: string | undefined) => Object.defineProperty(AppState, "currentState", { value: state, configurable: true, writable: true });
    const before = AppState.currentState;
    set("background");
    mocked.refillStatus.mockResolvedValue(refill("pending"));
    await act(async () => { tree = create(<FilamentScreen />); });
    const first = mocked.refillStatus.mock.calls.length;
    await tick(20_000);
    expect(mocked.refillStatus.mock.calls.length).toBe(first);
    set("active");
    await tick(5_000);
    expect(mocked.refillStatus.mock.calls.length).toBe(first + 1);
    set(before);
  });

  it("starts asking again when a refresh finds an active chain pending", async () => {
    mocked.refillStatus.mockResolvedValueOnce(refill("active")).mockResolvedValue(refill("pending"));
    await act(async () => { tree = create(<FilamentScreen />); });
    await tick(15_000);
    expect(mocked.refillStatus).toHaveBeenCalledTimes(1); // active: quiet
    await act(async () => { tree.update(<FilamentScreen key="refocus" />); });
    const after = mocked.refillStatus.mock.calls.length;
    await tick(5_000);
    expect(mocked.refillStatus.mock.calls.length).toBe(after + 1);
  });

  it("keeps asking while the chain needs the owner or is being removed", async () => {
    for (const state of ["needs_attention", "restoring"]) {
      mocked.refillStatus.mockReset().mockResolvedValue(refill(state));
      await act(async () => { tree = create(<FilamentScreen />); });
      const first = mocked.refillStatus.mock.calls.length;
      await tick(5_000);
      await tick(5_000);
      expect(mocked.refillStatus.mock.calls.length).toBe(first + 2);
      act(() => tree.unmount());
    }
  });

  it("asks about the printer now selected, not the one before", async () => {
    usePrintersStore.setState({ list: [
      { serial: "P1", friendly_name: "P1S", model: "P1S", connected: true },
      { serial: "P2", friendly_name: "Other", model: "P1S", connected: true },
    ] } as any);
    useLiveStore.setState((s: any) => ({ printers: { ...s.printers, P2: s.printers.P1 } }));
    mocked.refillStatus.mockResolvedValue(refill("pending"));
    await act(async () => { tree = create(<FilamentScreen />); });
    await act(async () => { usePrintersStore.setState({ selectedId: "P2" } as any); });
    mocked.refillStatus.mockClear();
    await tick(5_000);
    expect(mocked.refillStatus.mock.calls.length).toBeGreaterThan(0);
    expect(mocked.refillStatus.mock.calls.every(([printer]) => printer === "P2")).toBe(true);
  });
});

it("words a chain between the end slots exactly, and leaves the others alone", async () => {
  seed([assign(0, 8), assign(3, 7)]);
  mocked.refillStatus.mockResolvedValue({ ...refill("pending"), backups: [{ from_wire: 0, to_wire: 3, state: "pending", message: null }] });
  await act(async () => { tree = create(<FilamentScreen />); });
  expect(chainOf(1)).toBe("Backup: slot 4 · waiting for the printer to confirm");
  expect(chainOf(4)).toBe("Backs up slot 1 · waiting for the printer to confirm");
  expect(chainOf(2)).toBeNull();
  expect(chainOf(3)).toBeNull();
});
