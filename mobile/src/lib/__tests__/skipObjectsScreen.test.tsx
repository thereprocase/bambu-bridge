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
  useTheme: () => ({
    c: new Proxy({}, { get: () => "#000" }),
    space: new Proxy({}, { get: () => 8 }),
    radius: new Proxy({}, { get: () => 8 }),
    type: new Proxy({}, { get: () => ({}) }),
  }),
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

describe("plate map", () => {
  const control = jest.requireMock("../../api/control") as {
    getSkipObjects: jest.Mock; getSkipMap: jest.Mock;
  };
  const info = {
    job: "job", gcode_file: "job.gcode.3mf", run_id: 1, plate: 1, digest: "d1",
    label_object_enabled: true, map_source: "gcode", max_objects: 64,
    objects: [{ id: 7, name: "a", skipped: false }, { id: 9, name: "b", skipped: false }],
    // 4x1 px: ids 7 7 9 9
    map: { width: 4, height: 1, rows: [[7, 2, 9, 2]] },
    available: true, reason: null,
  };

  it("keeps the shown map until a re-rendered one has loaded, with no fade", async () => {
    control.getSkipObjects.mockResolvedValue(info);
    control.getSkipMap.mockResolvedValueOnce(new Uint8Array([1])).mockResolvedValueOnce(new Uint8Array([2]));
    let tree: ReturnType<typeof create> | undefined;
    await act(async () => { tree = create(<SkipObjectsScreen />); });
    await act(async () => {});
    const images = () => tree!.root.findAll((n) => n.type === "Image" && n.props.source?.uri);
    expect(images()).toHaveLength(1);
    act(() => { images()[0].props.onLoad?.(); });
    const first = images()[0];
    expect(first.props.fadeDuration).toBe(0);

    // Tap object 7 on the map: a new PNG is fetched and stacked over the old one.
    const plate = tree!.root.find((n) => n.props.accessibilityLabel === "Plate map");
    await act(async () => {
      plate.props.onLayout({ nativeEvent: { layout: { width: 4, height: 1 } } });
    });
    await act(async () => { plate.props.onPress({ nativeEvent: { locationX: 0.5, locationY: 0.5 } }); });
    await act(async () => {});
    const layered = images();
    expect(layered).toHaveLength(2);
    expect(layered[0].props.source.uri).toBe(first.props.source.uri);   // old map still there

    // Once the new one decodes it becomes the only layer, without remounting.
    act(() => { layered[1].props.onLoad(); });
    const after = images();
    expect(after).toHaveLength(1);
    expect(after[0].props.source.uri).toBe(layered[1].props.source.uri);
  }, 30_000);
});
