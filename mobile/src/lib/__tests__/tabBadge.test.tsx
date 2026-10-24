import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import * as inv from "../../api/inventory";
import { useReviewStore } from "../../store/review";

const captured: Record<string, any> = {};
jest.mock("../../api/inventory", () => ({ inventoryStatus: jest.fn() }));
jest.mock("expo-router", () => {
  const Screen = (props: any) => { captured[props.name] = props.options; return null; };
  const Tabs = ({ children }: any) => children ?? null;
  (Tabs as any).Screen = Screen;
  return { Tabs, Link: ({ children }: any) => children, Redirect: () => null };
});
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("react-native-safe-area-context", () => ({ SafeAreaView: ({ children }: any) => children }));
jest.mock("../../components/ConnectionStrip", () => ({ ConnectionStrip: () => null }));
jest.mock("../../components/ReachabilityBanner", () => ({ ReachabilityBanner: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({ useTheme: () => ({ c: jest.requireActual("../../theme/tokens").light }) }));
jest.mock("../../store/bridge", () => ({ useBridgeStore: (sel: any) => sel({ baseUrl: "http://bridge.test", bearer: "t", baseUrlLan: null }) }));
jest.mock("../../store/live", () => ({ useLiveStore: (sel: any) => sel({ ensureConnection: () => {}, closeConnection: () => {} }) }));
jest.mock("../../store/printers", () => ({ usePrintersStore: (sel: any) => sel({ selectedId: null, refresh: () => {}, list: [] }) }));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const TabsLayout = require("../../../app/(tabs)/_layout").default;
const mocked = inv as jest.Mocked<typeof inv>;

test("the Filament badge shows the bridge's count and clears the moment a card closes", async () => {
  mocked.inventoryStatus.mockResolvedValue({ configured: true, api_version: 2, review_count: 1, accounting_mode: "automatic", native_overlay_enabled: false } as any);
  let tree!: ReactTestRenderer;
  await act(async () => { tree = create(<TabsLayout />); });
  expect(captured["filament"].tabBarBadge).toBe(1);
  // The Filament tab resolves the only card and writes the shared count; no poll needed.
  await act(async () => { useReviewStore.getState().setCount(0); });
  expect(captured["filament"].tabBarBadge).toBeUndefined();
  tree.unmount();
});
