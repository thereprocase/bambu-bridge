/**
 * The Filament tab keyed an effect on viewOf().ams, a new array each render, so
 * once the AMS reported slots it re-set state forever ("Maximum update depth
 * exceeded") and kept the JS thread busy while the tab stayed mounted.
 */
import { act, create } from "react-test-renderer";

let mockViewOfCalls = 0;
jest.mock("expo-router", () => ({ useRouter: () => ({ push: jest.fn() }) }));
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => ({
    c: new Proxy({}, { get: () => "#000" }),
    space: new Proxy({}, { get: () => 8 }),
    radius: new Proxy({}, { get: () => 8 }),
    type: new Proxy({}, { get: () => ({}) }),
  }),
}));
jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(() => "P1"), set: jest.fn(), remove: jest.fn(), getBoolean: jest.fn() } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../ws/live", () => ({ LiveConnection: jest.fn(), applyDelta: jest.fn() }));
jest.mock("../../components/Toast", () => ({ useToastStore: () => jest.fn() }));
jest.mock("../../api/jobs", () => ({ listSpools: jest.fn(() => new Promise(() => {})), createSpool: jest.fn(), deleteSpool: jest.fn() }));
jest.mock("../../api/control", () => ({ amsChange: jest.fn(), unloadFilament: jest.fn() }));
jest.mock("../../lib/snapshot", () => {
  const actual = jest.requireActual("../../lib/snapshot");
  return { ...actual, viewOf: (s: unknown) => {
    mockViewOfCalls += 1;
    if (mockViewOfCalls > 200) throw new Error("render loop: viewOf called over 200 times");
    return actual.viewOf(s);
  } };
});

import { useLiveStore } from "../../store/live";
import FilamentScreen from "../../../app/(tabs)/filament";

it("renders a bounded number of times once AMS slots exist", () => {
  useLiveStore.setState({ printers: { P1: { status: "open", lastEvent: null, eventLog: [], snapshot: {
    model: "P1S", ams: { present: true, slots: [{ physical_slot: 1, type: "PLA", color: "#fff", state: "loaded" }] },
  } } } } as any);
  const errors = jest.spyOn(console, "error").mockImplementation(() => {});
  let thrown: unknown = null;
  try { act(() => { create(<FilamentScreen />); }); } catch (e) { thrown = e; }
  expect(String(thrown)).not.toMatch(/render loop/);
  expect(errors.mock.calls.flat().join(" ")).not.toMatch(/Maximum update depth/);
  expect(mockViewOfCalls).toBeLessThan(20);
  errors.mockRestore();
});
