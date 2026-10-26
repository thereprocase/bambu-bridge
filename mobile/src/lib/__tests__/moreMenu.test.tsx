import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { Button } from "../../components/Button";
import { BAR_TABS, FOLDED_TABS, MENU_ITEMS } from "../moreMenu";

const mockRouter = { dismissTo: jest.fn(), replace: jest.fn(), back: jest.fn(), navigate: jest.fn(), push: jest.fn() };
const mockCaptured: Record<string, any> = {};
const mockOpts: { screen: any } = { screen: null };
jest.mock("expo-router", () => {
  const Screen = (props: any) => { mockCaptured[props.name] = props.options; return null; };
  const Tabs = ({ children, screenOptions }: any) => { mockOpts.screen = screenOptions; return children ?? null; };
  (Tabs as any).Screen = Screen;
  return { Tabs, Link: ({ children }: any) => children, Redirect: () => null, useRouter: () => mockRouter };
});
jest.mock("@expo/vector-icons", () => ({ Ionicons: () => null }));
jest.mock("react-native-safe-area-context", () => ({ SafeAreaView: ({ children }: any) => children }));
jest.mock("../../components/ConnectionStrip", () => ({ ConnectionStrip: () => null }));
jest.mock("../../components/ReachabilityBanner", () => ({ ReachabilityBanner: () => null }));
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => {
    const tokens = jest.requireActual("../../theme/tokens");
    return { c: tokens.light, space: tokens.space, type: tokens.typography, radius: tokens.radius };
  },
}));
jest.mock("../../store/bridge", () => ({ useBridgeStore: (sel: any) => sel({ baseUrl: "http://bridge.test", bearer: "t", baseUrlLan: null }) }));
jest.mock("../../store/live", () => ({ useLiveStore: (sel: any) => sel({ ensureConnection: () => {}, closeConnection: () => {} }) }));
jest.mock("../../store/printers", () => ({ usePrintersStore: (sel: any) => sel({ selectedId: null, refresh: () => {}, list: [] }) }));
jest.mock("../../api/inventory", () => ({ inventoryStatus: jest.fn(async () => ({ configured: true, api_version: 2, review_count: 0 })) }));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const TabsLayout = require("../../../app/(tabs)/_layout").default;
// eslint-disable-next-line @typescript-eslint/no-require-imports
const MoreScreen = require("../../../app/more").default;

let tree: ReactTestRenderer;
const press = async (node: any) => { await act(async () => { await node.props.onPress(); }); };

beforeEach(() => { jest.clearAllMocks(); for (const k of Object.keys(mockCaptured)) delete mockCaptured[k]; });

describe("the bottom bar", () => {
  it("keeps the daily tabs and folds the rest away, routes intact", async () => {
    await act(async () => { tree = create(<TabsLayout />); });
    const visible = Object.entries(mockCaptured).filter(([, o]) => o.href !== null).map(([n]) => n);
    expect(visible).toEqual([...BAR_TABS]);
    expect(FOLDED_TABS).toEqual(["controls", "activity", "advanced"]);
    for (const name of FOLDED_TABS) {
      expect(mockCaptured[name].href).toBeNull();
      expect(mockCaptured[name].title).toBeTruthy();
      expect(typeof mockCaptured[name].headerLeft).toBe("function");
    }
    expect(mockCaptured["status"].headerLeft).toBeUndefined();
    tree.unmount();
  });

  it("has one header button, the gear, which opens the menu", async () => {
    await act(async () => { tree = create(<TabsLayout />); });
    let header!: ReactTestRenderer;
    await act(async () => { header = create(mockOpts.screen.headerRight()); });
    const buttons = header.root.findAll((n) => typeof n.type === "string" && n.props.accessibilityRole === "button");
    expect(buttons.map((b) => b.props.accessibilityLabel)).toEqual(["Menu"]);
    // The Link wrapper is mocked away; the href lives on it in the real tree, checked by the menu test below.
    tree.unmount();
  });

  it("takes a folded screen back to Status from its header", async () => {
    await act(async () => { tree = create(<TabsLayout />); });
    let left!: ReactTestRenderer;
    await act(async () => { left = create(mockCaptured["controls"].headerLeft()); });
    await press(left.root.findAll((n) => n.props.accessibilityLabel === "Back to Status")[0]);
    expect(mockRouter.navigate).toHaveBeenCalledWith("/(tabs)/status");
    tree.unmount();
  });
});

describe("the menu", () => {
  it("lists the folded screens, the bridge queue and Settings, each one tap away", async () => {
    await act(async () => { tree = create(<MoreScreen />); });
    // The Pressable itself (it has onPress); the host View it renders carries the same labels but no onPress.
    const rows = tree.root.findAll((n) => typeof n.props.onPress === "function" && typeof n.props.accessibilityHint === "string");
    expect(rows.map((r) => r.props.accessibilityLabel)).toEqual(["Controls", "Activity", "Advanced", "Bridge queue and recovery", "Settings"]);
    expect(MENU_ITEMS.map((m) => m.href)).toEqual(["/(tabs)/controls", "/(tabs)/activity", "/(tabs)/advanced", "/native-recovery", "/settings"]);
    await press(rows[0]);
    expect(mockRouter.dismissTo).toHaveBeenCalledWith("/(tabs)/controls");
    await press(rows[4]);
    expect(mockRouter.replace).toHaveBeenCalledWith("/settings");
    await press(tree.root.findAllByType(Button).find((b) => b.props.label === "Close")!);
    expect(mockRouter.back).toHaveBeenCalled();
    tree.unmount();
  });
});
