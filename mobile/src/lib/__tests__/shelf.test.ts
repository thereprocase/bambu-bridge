import type { InventorySource, InventorySpool, InventoryStatus } from "../../api/inventory";
import { answered, cameOutOf, everySpool, offAmsSpools, pruneShelved, readMemory, readShelved, remember, shelfKey, slotsFor, spoolColor, storedGroups, storedPercent, storedSummary, writeMemory, writeShelved } from "../shelf";
import type { SlotView } from "../slotInventory";

const mockStore: Record<string, string> = {};
const mockSet = jest.fn((key: string, value: string) => { mockStore[key] = String(value); });
jest.mock("../kv", () => ({ kv: { getString: (key: string) => mockStore[key], set: (k: string, v: string) => mockSet(k, v) } }));


const spool = (id: number, over: Partial<InventorySpool> = {}): InventorySpool => ({
  id, archived: false, remaining_weight: 300, initial_weight: 1000, spool_weight: null,
  filament: { id, name: `PETG ${id}`, vendor: null, material: "PETG", color_hex: "1a2b3c", weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
  ...over,
});
const status = (...seated: [string, number][]): InventoryStatus => ({
  configured: true, api_version: 2, accounting_mode: "automatic", native_overlay_enabled: false,
  assignments: seated.map(([source, spool_id]) => ({ printer_id: "P1", source, spool_id, assigned_at: 1, observed_identity: null, wire_slot: 0 })),
});
const view = (last: number | null | undefined, assigned: number | null = null): SlotView => {
  const source: InventorySource = {
    printer_id: "P1", source: "ams:abc:tray:0", wire_slot: 0, label: "AMS A · slot 1", material: "PETG", color: "FF0000FF",
    observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], last_spool_id: last,
  };
  return {
    source,
    assignment: assigned == null ? null : { printer_id: "P1", source: source.source, spool_id: assigned, assigned_at: 1, observed_identity: null, wire_slot: 0 },
    spool: null,
  };
};

beforeEach(() => {
  for (const key of Object.keys(mockStore)) delete mockStore[key];
  mockSet.mockClear();
});

describe("offAmsSpools", () => {
  it("keeps unarchived spools that sit in no slot on any printer, in the bridge's order", () => {
    const spools = [spool(9), spool(3), spool(4, { archived: true }), spool(5)];
    const seated = status(["ams:abc:tray:0", 3], ["external:x", 5]);
    expect(offAmsSpools(spools, seated).map((s) => s.id)).toEqual([9]);
    expect(offAmsSpools(spools, null).map((s) => s.id)).toEqual([9, 3, 5]);
  });
});

describe("cameOutOf", () => {
  const spools = [spool(8), spool(9)];

  it("names the last spool when it is in no slot and still in Spoolman", () => {
    expect(cameOutOf(view(8), spools, status(), new Set())?.id).toBe(8);
    expect(cameOutOf(view(8, 9), spools, status(["ams:abc:tray:0", 9]), new Set())?.id).toBe(8);
  });

  it("stays quiet when the spool is still here, elsewhere, archived, unknown or already answered", () => {
    expect(cameOutOf(view(8, 8), spools, status(["ams:abc:tray:0", 8]), new Set())).toBeNull();
    expect(cameOutOf(view(8), spools, status(["ams:abc:tray:2", 8]), new Set())).toBeNull();
    expect(cameOutOf(view(8), [spool(8, { archived: true })], status(), new Set())).toBeNull();
    expect(cameOutOf(view(7), spools, status(), new Set())).toBeNull();
    expect(cameOutOf(view(null), spools, status(), new Set())).toBeNull();
    expect(cameOutOf(view(undefined), spools, status(), new Set())).toBeNull();
    expect(cameOutOf(view(8), spools, status(), new Set([shelfKey("P1", "ams:abc:tray:0", 8)]))).toBeNull();
  });

  it("asks again in another slot after a Back on the shelf here", () => {
    expect(cameOutOf(view(8), spools, status(), new Set([shelfKey("P1", "ams:abc:tray:3", 8)]))?.id).toBe(8);
  });
});

describe("shelved answers", () => {
  it("round-trip through local storage and survive junk", () => {
    expect(readShelved().size).toBe(0);
    expect(writeShelved(new Set(["P1|ams:abc:tray:0|8"]))).toBe(true);
    expect([...readShelved()]).toEqual(["P1|ams:abc:tray:0|8"]);
    mockStore["filament.shelved"] = "{not json";
    expect(readShelved().size).toBe(0);
    mockStore["filament.shelved"] = JSON.stringify(["P1|s|1", 7, null]);
    expect([...readShelved()]).toEqual(["P1|s|1"]);
  });

  it("report a failed save instead of throwing", () => {
    mockSet.mockImplementationOnce(() => { throw new Error("quota"); });
    expect(writeShelved(new Set(["P1|s|1"]))).toBe(false);
  });

  it("drop a spool's answers once it is back in a slot", () => {
    const shelved = new Set(["P1|ams:abc:tray:0|8", "P1|ams:abc:tray:1|12", "P2|ams:def:tray:0|8"]);
    expect([...pruneShelved(shelved, status(["ams:abc:tray:2", 8]))]).toEqual(["P1|ams:abc:tray:1|12"]);
    expect(pruneShelved(shelved, null)).toEqual(shelved);
  });
});

it("spoolColor reads Spoolman's hex as #RRGGBB, or null", () => {
  expect(spoolColor(spool(1))).toBe("#1A2B3C");
  expect(spoolColor(spool(1, { filament: { ...spool(1).filament, color_hex: null } }))).toBeNull();
});

it("everySpool pages by 100 until a short page, and stops at the bridge's 1000", async () => {
  const page = jest.fn(async (offset: number) => (offset < 200 ? Array.from({ length: 100 }, (_, i) => spool(offset + i)) : [spool(999)]));
  expect((await everySpool(page)).length).toBe(201);
  expect(page.mock.calls).toEqual([[0, 100], [100, 100], [200, 100]]);
  const endless = jest.fn(async (offset: number) => Array.from({ length: 100 }, (_, i) => spool(offset + i)));
  expect((await everySpool(endless)).length).toBe(1000);
});

describe("slot memory", () => {
  const S = "ams:abc:tray:2";
  const spools = [spool(8), spool(9), spool(1)];

  it("turns the spool seen before into the slot's question, and keeps it until it is seated or gone", () => {
    let memory = remember({}, "P1", status([S, 8]), spools);
    expect(memory).toEqual({ [`P1|${S}`]: { seen: 8, out: null } });
    memory = remember(memory, "P1", status(), spools); // emptied
    expect(memory).toEqual({ [`P1|${S}`]: { seen: null, out: 8 } });
    memory = remember(memory, "P1", status([S, 9]), spools); // #9 loaded; #8 still asked
    expect(memory).toEqual({ [`P1|${S}`]: { seen: 9, out: 8 } });
    expect(cameOutOf(view(9, 9), spools, status([S, 9]), new Set(), { "P1|ams:abc:tray:0": { seen: 9, out: 8 } })?.id).toBe(8);
    expect(remember(memory, "P1", status([S, 9], ["ams:abc:tray:0", 8]), spools)[`P1|${S}`]).toEqual({ seen: 9, out: null });
    expect(remember(memory, "P1", status([S, 9]), [spool(9)])[`P1|${S}`]).toEqual({ seen: 9, out: null });
  });

  it("leaves other printers' slots as they were seen, and ignores a bridge without assignments", () => {
    const memory = { "P2|ams:def:tray:0": { seen: 8, out: null } };
    expect(remember(memory, "P1", status(), spools)).toEqual(memory);
    const older = { ...status(), assignments: undefined };
    expect(remember({ [`P1|${S}`]: { seen: 8, out: null } }, "P1", older, spools)).toEqual({ [`P1|${S}`]: { seen: 8, out: null } });
  });

  it("is cleared by an answer and round-trips through local storage", () => {
    const memory = { [`P1|${S}`]: { seen: 9, out: 8 }, "P1|ams:abc:tray:3": { seen: null, out: 5 } };
    expect(answered(memory, "P1", S)).toEqual({ [`P1|${S}`]: { seen: 9, out: null }, "P1|ams:abc:tray:3": { seen: null, out: 5 } });
    expect(answered(memory, "P1", "ams:abc:tray:3")).toEqual({ [`P1|${S}`]: { seen: 9, out: 8 } });
    expect(writeMemory(memory)).toBe(true);
    expect(readMemory()).toEqual(memory);
    mockStore["filament.seen"] = JSON.stringify({ a: { seen: "x", out: 3 }, b: null, c: 4 });
    expect(readMemory()).toEqual({ a: { seen: null, out: 3 } });
    mockStore["filament.seen"] = "[";
    expect(readMemory()).toEqual({});
  });
});

describe("the stored browser", () => {
  const product = (id: number, vendor: string, name: string, material: string | null, remaining: number | null, over: Partial<InventorySpool> = {}) =>
    spool(id, {
      remaining_weight: remaining,
      filament: { id, name, vendor: vendor ? { id: 1, name: vendor, empty_spool_weight: null } : null, material, color_hex: "1a2b3c", weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
      ...over,
    });
  // Today's shelf: two Galaxy Blacks, a near-empty one among them, three PolyTerras, a PC, and one with no material.
  const shelf = [
    product(7, "Polymaker", "PETG Galaxy Black", "PETG", 97),
    product(14, "Polymaker", "PETG Galaxy Black", "PETG", 886),
    product(9, "Elegoo", "Rapid PETG Grey", "petg", 830),
    product(15, "Polymaker", "PolyTerra PLA Army Red", "PLA", 154),
    product(17, "Polymaker", "Panchroma PLA Black", "PLA", 675),
    product(16, "Polymaker", "PolyTerra PLA+ Black", "PLA", null),
    product(6, "Polymaker", "PolyLite PC Transparent", "PC", 845),
    product(30, "", "Mystery", null, 500),
  ];

  it("groups by material alphabetically with no-material last, one product's spools together fullest first, unknown weight closing the group", () => {
    expect(storedGroups(shelf).map((g) => [g.material, g.spools.map((s) => s.id)])).toEqual([
      ["PC", [6]],
      ["PETG", [9, 14, 7]],
      ["PLA", [17, 15, 16]],
      ["Other", [30]],
    ]);
    expect(storedGroups([])).toEqual([]);
  });

  it("sums the header only when every spool has a weight", () => {
    expect(storedSummary(shelf.filter((s) => s.id !== 16))).toBe("7 spools · 3,987 g");
    expect(storedSummary(shelf)).toBe("8 spools");
    expect(storedSummary([shelf[0]])).toBe("1 spool · 97 g");
    expect(storedSummary([])).toBe("0 spools");
  });

  it("reads the bar from Spoolman's percent, else remaining over the initial or package weight", () => {
    expect(storedPercent(product(7, "P", "x", "PETG", 97))).toBe(10);
    expect(storedPercent(product(7, "P", "x", "PETG", 97, { remaining_percent: 12 }))).toBe(12);
    expect(storedPercent(product(7, "P", "x", "PETG", 250, { initial_weight: 500 }))).toBe(50);
    expect(storedPercent(product(7, "P", "x", "PETG", 1200))).toBe(100);
    expect(storedPercent(product(7, "P", "x", "PETG", null))).toBeNull();
  });

  describe("slotsFor", () => {
    const slot = (wire: number, over: Partial<InventorySource> = {}, assigned: number | null = null): SlotView => {
      const source: InventorySource = {
        printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material: "PETG", color: "FF0000FF",
        observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false, ...over,
      };
      return { source, assignment: assigned == null ? null : { printer_id: "P1", source: source.source, spool_id: assigned, assigned_at: 1, observed_identity: null, wire_slot: wire }, spool: null };
    };
    const pla = product(15, "Polymaker", "PolyTerra PLA Army Red", "PLA", 154);

    it("offers same-material slots as they are, and untagged slots of another material with the slot set first", () => {
      const views = [slot(0, { material: "PLA" }), slot(1), slot(2, { material: "PLA" }, 12), slot(3, { tagged: true })];
      expect(slotsFor(pla, views).map((o) => [o.view.source.wire_slot, o.setSlot])).toEqual([[0, false], [1, true], [2, false]]);
    });

    it("skips empty, stale and still-reading slots, and the one holding this spool", () => {
      const views = [
        slot(0, { material: "PLA", issues: ["source_empty"] }),
        slot(1, { material: "PLA", stale: true, age_seconds: 200 }),
        slot(2, { material: "PLA", tag_reading: true }),
        slot(3, { material: "PLA" }, 15),
      ];
      expect(slotsFor(pla, views)).toEqual([]);
    });

    it("offers a spool with no material nowhere, even to a slot whose material is blank", () => {
      const mystery = product(30, "", "Mystery", null, 500);
      expect(slotsFor(mystery, [slot(0), slot(1, { material: "" })])).toEqual([]);
    });
  });
});
