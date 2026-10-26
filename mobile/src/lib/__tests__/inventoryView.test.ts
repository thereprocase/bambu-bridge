import type { InventorySource, InventorySpool, InventoryStatus } from "../../api/inventory";
import {
  colourKey,
  dateText,
  fillText,
  filterSpools,
  inventorySummary,
  materialOptions,
  NO_FILTER,
  placeOf,
  sectionsFor,
  sortSpools,
  vendorOptions,
  weightText,
} from "../inventoryView";
import type { SlotView } from "../slotInventory";

// shelf.ts (LOW_PERCENT, storedPercent) reads device storage as it loads; none is wanted here.
jest.mock("../kv", () => ({ kv: { getString: () => undefined, set: () => undefined } }));

const spool = (id: number, vendor: string, name: string, material: string | null, remaining: number | null, over: Partial<InventorySpool> = {}, color = "1a2b3c"): InventorySpool => ({
  id, archived: false, remaining_weight: remaining, initial_weight: 1000, spool_weight: null,
  filament: { id, name, vendor: vendor ? { id: 1, name: vendor, empty_spool_weight: null } : null, material, color_hex: color, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
  ...over,
});

// Today's Spoolman, roughly.
const shelf = [
  spool(7, "Polymaker", "PETG Galaxy Black", "PETG", 97, { lot_nr: "A7533841419", last_used: "2026-10-26T13:25:29Z" }, "1A1A1A"),
  spool(14, "Polymaker", "PETG Galaxy Black", "PETG", 886, { last_used: "2026-10-26T17:14:22Z" }, "1A1A1A"),
  spool(9, "Elegoo", "Rapid PETG Grey", "PETG", 830, { last_used: "2026-10-25T12:25:28Z" }, "828282"),
  spool(15, "Polymaker", "PolyTerra PLA Army Red", "PLA", 154, { lot_nr: "A2311180040" }, "b5232b"),
  spool(10, "Polymaker", "PolyTerra PLA Sapphire Blue", "PLA", 452, { last_used: "2026-10-25T14:21:03Z" }, "005aa2"),
  spool(6, "Polymaker", "PolyLite PC Transparent", "PC", 845, {}, "FFFFFF"),
  spool(30, "", "Mystery", null, null, {}, ""),
  spool(8, "Elegoo", "Rapid PETG White", "PETG", 0, { archived: true, last_used: "2026-10-24T21:55:51Z" }, "FFFFFF"),
];

const view = (wire: number, assigned: number | null): SlotView => {
  const source: InventorySource = {
    printer_id: "P1", source: `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`, material: "PETG", color: "FF0000FF",
    observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [],
  };
  return { source, assignment: assigned == null ? null : { printer_id: "P1", source: source.source, spool_id: assigned, assigned_at: 1, observed_identity: null, wire_slot: wire }, spool: null };
};
const status: InventoryStatus = {
  configured: true, api_version: 2, accounting_mode: "automatic", native_overlay_enabled: false,
  assignments: [
    { printer_id: "P1", source: "ams:abc:tray:1", spool_id: 14, assigned_at: 1, observed_identity: null, wire_slot: 1 },
    { printer_id: "P2", source: "ams:zzz:tray:0", spool_id: 6, assigned_at: 1, observed_identity: null, wire_slot: 0 },
  ],
};
const views = [view(0, null), view(1, 14), view(254, 9)];
const place = (s: InventorySpool) => placeOf(s, status, views, "P1");

describe("placeOf", () => {
  it("names the slot or holder on this printer, another printer by id, and nothing for a stored spool", () => {
    expect(place(shelf[1])).toBe("Slot 2");
    expect(place(shelf[2])).toBe("External spool");
    expect(place(shelf[5])).toBe("On P2");
    expect(place(shelf[3])).toBeNull();
  });
});

describe("chips", () => {
  it("count spools per material and vendor, most first", () => {
    expect(materialOptions(shelf)).toEqual([
      { id: "PETG", label: "PETG", count: 4 },
      { id: "PLA", label: "PLA", count: 2 },
      { id: "Other", label: "Other", count: 1 },
      { id: "PC", label: "PC", count: 1 },
    ]);
    expect(vendorOptions(shelf).map((o) => `${o.label} ${o.count}`)).toEqual(["Polymaker 5", "Elegoo 2", "No vendor 1"]);
  });
});

describe("filterSpools", () => {
  it("narrows by material, vendor and place", () => {
    expect(filterSpools(shelf, { ...NO_FILTER, material: "PLA" }, place).map((s) => s.id)).toEqual([15, 10]);
    expect(filterSpools(shelf, { ...NO_FILTER, vendor: "Elegoo" }, place).map((s) => s.id)).toEqual([9, 8]);
    expect(filterSpools(shelf, { ...NO_FILTER, where: "printer" }, place).map((s) => s.id)).toEqual([14, 9, 6]);
    expect(filterSpools(shelf, { ...NO_FILTER, where: "shelf" }, place).map((s) => s.id)).toEqual([7, 15, 10, 30, 8]);
  });

  it("searches number, name, vendor, material, colour, lot and SKU, every word required", () => {
    const ids = (query: string) => filterSpools(shelf, { ...NO_FILTER, query }, place).map((s) => s.id);
    expect(ids("#15")).toEqual([15]);
    expect(ids("15")).toEqual([15]);
    expect(ids("galaxy")).toEqual([7, 14]);
    expect(ids("elegoo grey")).toEqual([9]);
    expect(ids("A7533841419")).toEqual([7]);
    expect(ids("005aa2")).toEqual([10]);
    expect(ids("petg red")).toEqual([]);
    expect(ids("  ")).toHaveLength(shelf.length);
  });
});

describe("sortSpools", () => {
  const ids = (key: Parameters<typeof sortSpools>[1]) => sortSpools(shelf, key).map((s) => s.id);

  it("by material: alphabetical, no-material last, one product together fullest first", () => {
    expect(ids("material")).toEqual([6, 9, 8, 14, 7, 15, 10, 30]);
    expect(sectionsFor(sortSpools(shelf, "material"), "material").map((s) => [s.title, s.spools.length])).toEqual([["PC", 1], ["PETG", 4], ["PLA", 2], ["Other", 1]]);
  });

  it("by colour: around the hue circle, greys dark to light after, unknown last", () => {
    expect(ids("colour")).toEqual([15, 10, 14, 7, 9, 6, 8, 30]);
    expect(colourKey("ff0000")).toBe(20);
    expect(colourKey("00ff00")).toBe(140);
    expect(colourKey("#0000ff")).toBe(260);
    expect(colourKey("b5232b")).toBeLessThan(colourKey("005aa2")); // a dark red leads a blue
    expect(colourKey("000000")).toBeLessThan(colourKey("ffffff"));
    expect(colourKey("zz")).toBe(1000);
    expect(sectionsFor(sortSpools(shelf, "colour"), "colour")).toEqual([{ title: null, spools: sortSpools(shelf, "colour") }]);
    expect(sectionsFor([], "colour")).toEqual([]);
  });

  it("by left: least first, unknown weight last", () => {
    expect(ids("left")).toEqual([8, 7, 15, 10, 9, 6, 14, 30]);
  });

  it("by recent: last used newest first, never-used after in the bridge's order", () => {
    expect(ids("recent")).toEqual([14, 7, 10, 9, 8, 15, 6, 30]);
  });
});

describe("inventorySummary", () => {
  it("counts active spools, sums grams by material, and names the low ones least first", () => {
    const summary = inventorySummary(shelf);
    expect(summary.count).toBe(7);
    expect(summary.grams).toBe(3264);
    expect(summary.unweighed).toBe(1);
    expect(summary.byMaterial.map((m) => [m.material, m.grams, Math.round(m.share * 100)])).toEqual([["PETG", 1813, 56], ["PC", 845, 26], ["PLA", 606, 19]]);
    expect(summary.low.map((s) => s.id)).toEqual([7]);
    expect(inventorySummary([])).toEqual({ count: 0, grams: 0, unweighed: 0, byMaterial: [], low: [] });
  });
});

describe("words", () => {
  it("weightText rounds to a tenth of a kilogram past 1 kg and whole kilograms past 10", () => {
    expect(weightText(845)).toBe("845 g");
    expect(weightText(3264)).toBe("3.3 kg");
    expect(weightText(12_400)).toBe("12 kg");
  });

  it("fillText shows left of full, or left alone, or unknown", () => {
    expect(fillText(shelf[3])).toBe("154 g of 1,000 g");
    expect(fillText(spool(1, "V", "n", "PLA", 154, { initial_weight: null, filament: { ...shelf[3].filament, weight: null } }))).toBe("154 g left");
    expect(fillText(shelf[6])).toBe("weight unknown");
  });

  it("dateText drops the year inside it and keeps it outside; junk is null", () => {
    const now = new Date("2026-10-26T12:00:00Z");
    expect(dateText("2026-10-25T19:17:07Z", now)).toMatch(/^Oct 2[56]$/);
    expect(dateText("2025-03-02T00:00:00Z", now)).toMatch(/^Mar [12], 2025$/);
    expect(dateText(null, now)).toBeNull();
    expect(dateText("yesterday", now)).toBeNull();
  });
});
