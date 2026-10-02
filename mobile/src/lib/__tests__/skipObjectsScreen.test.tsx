/**
 * The Skip Objects screen mounts without a live snapshot. A fresh [] fallback
 * in its zustand selector re-rendered forever ("Maximum update depth
 * exceeded"); review finding #17.
 */
import { act, create } from "react-test-renderer";

jest.mock("expo-router", () => ({
  useRouter: () => ({ back: jest.fn() }),
  useLocalSearchParams: () => ({ printer: "P1" }),
}));
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => ({ c: new Proxy({}, { get: () => "#000" }), space: { sm: 4, md: 8, lg: 16 }, type: {} }),
}));
jest.mock("../../lib/kv", () => ({ kv: { getString: jest.fn(), set: jest.fn() } }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));
jest.mock("../../ws/live", () => ({ connectLive: jest.fn() }));
jest.mock("../../components/Toast", () => ({ useToastStore: () => jest.fn() }));
jest.mock("../../api/control", () => ({
  getSkipObjects: jest.fn(() => new Promise(() => {})),
  getSkipMap: jest.fn(),
  skipObjects: jest.fn(),
}));

import SkipObjectsScreen from "../../../app/skip-objects";

it("renders without a live snapshot and without a render loop", () => {
  const errors = jest.spyOn(console, "error").mockImplementation(() => {});
  let tree: ReturnType<typeof create> | undefined;
  act(() => { tree = create(<SkipObjectsScreen />); });
  expect(JSON.stringify(tree!.toJSON())).toContain("Loading");
  expect(errors.mock.calls.flat().join(" ")).not.toMatch(/Maximum update depth/);
  errors.mockRestore();
});
