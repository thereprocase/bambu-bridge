import {
  applyRefusal, confirmText, followPrinter, hitTest, idAt, initialStates, toBase64,
  type SkipObjectsInfo, type SkipMap,
} from "../skipObjects";

// object 63 in columns 0-1, 74 in columns 3-4 of rows 1-2, stray colour 99.
const MAP: SkipMap = {
  width: 6,
  height: 4,
  rows: [[0, 6], [63, 2, 0, 1, 74, 2, 99, 1], [63, 2, 0, 1, 74, 2, 0, 1], [0, 6]],
};
const IDS = new Set([63, 74]);

const INFO: SkipObjectsInfo = {
  job: "multi3", plate: 1, label_object_enabled: true, max_objects: 64, available: true, reason: null,
  map: MAP,
  objects: [
    { id: 63, name: "cube.stl", skipped: false },
    { id: 74, name: "bar.stl", skipped: false },
    { id: 85, name: "frame.stl", skipped: true },
  ],
};

test("idAt walks run-length rows", () => {
  expect([idAt(MAP, 0, 1), idAt(MAP, 2, 1), idAt(MAP, 4, 2), idAt(MAP, 5, 1)]).toEqual([63, 0, 74, 99]);
  expect([idAt(MAP, -1, 0), idAt(MAP, 6, 1), idAt(MAP, 0, 4), idAt(null, 0, 0)]).toEqual([0, 0, 0, 0]);
});

test("hitTest scales the tap to image pixels and ignores unlisted colours", () => {
  expect(hitTest(MAP, IDS, 10, 60, 300, 200)).toBe(63);
  expect(hitTest(MAP, IDS, 100, 60, 300, 200)).toBe(0);
  expect(hitTest(MAP, IDS, 160, 140, 300, 200)).toBe(74);
  expect(hitTest(MAP, IDS, 260, 60, 300, 200)).toBe(0);
  expect(hitTest(MAP, IDS, 10, 60, 0, 0)).toBe(0);
});

test("states start from the GET and the printer's s_obj, then follow the printer", () => {
  const states = initialStates(INFO, [74]);
  expect([...states]).toEqual([[63, "unchecked"], [74, "skipped"], [85, "skipped"]]);
  states.set(63, "checked");
  expect(followPrinter(states, [85])).toBe(states);
  expect(followPrinter(states, [63]).get(63)).toBe("skipped");
});

test("apply refusals use Orca's tooltips", () => {
  const states = initialStates(INFO);
  expect(applyRefusal(INFO, states)).toBe("Nothing selected");
  states.set(63, "checked");
  expect(applyRefusal(INFO, states)).toBe("");
  expect(applyRefusal({ ...INFO, label_object_enabled: false }, states)).toBe("The current print job cannot be skipped");
  expect(applyRefusal({ ...INFO, available: false, reason: "Control support under review" }, states))
    .toBe("Control support under review");
  const many = new Map(Array.from({ length: 65 }, (_, i) => [i, i ? "unchecked" : "checked"] as const));
  expect(applyRefusal(INFO, many)).toBe("Over 64 objects in single plate");
});

test("confirmation text warns when every object would be skipped", () => {
  const states = initialStates(INFO);
  states.set(63, "checked");
  expect(confirmText(states)).toEqual({ title: "Skipping 1 objects.", body: "This action cannot be undone. Continue?", all: false });
  states.set(74, "checked");
  expect(confirmText(states)).toEqual({ title: "Skipping all objects.", body: "The printing job will be stopped. Continue?", all: true });
});

test("toBase64 encodes bytes", () => {
  expect(toBase64(new Uint8Array([0x89, 0x50, 0x4e, 0x47]).buffer)).toBe("iVBORw==");
});
