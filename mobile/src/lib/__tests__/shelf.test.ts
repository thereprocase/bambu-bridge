import type { InventorySource, InventorySpool, InventoryStatus } from "../../api/inventory";
import { cameOutOf, everySpool, offAmsSpools, pruneShelved, readShelved, shelfKey, spoolColor, writeShelved } from "../shelf";
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
