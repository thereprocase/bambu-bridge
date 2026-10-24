import { BridgeError, BridgeNetworkError } from "../../api/errors";
import type { InventorySource, InventorySpool, InventoryStatus } from "../../api/inventory";
import {
  candidateSpools,
  gramsLeft,
  inventoryErrorText,
  materialLine,
  slotTitle,
  slotViews,
  sourceColor,
  spoolLabel,
} from "../slotInventory";

const source = (over: Partial<InventorySource> = {}): InventorySource => ({
  printer_id: "P1",
  source: "ams:abc:tray:2",
  wire_slot: 2,
  label: "AMS A · slot 3",
  material: "PETG",
  color: "FF0000FF",
  observed_identity: "a".repeat(64),
  observed_at: 1,
  assignable: true,
  issues: [],
  tagged: false,
  ...over,
});
const spool = (id: number, material: string | null, over: Partial<InventorySpool> = {}): InventorySpool => ({
  id,
  filament: { id: id * 10, name: `Name ${id}`, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material, color_hex: null, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
  remaining_weight: 640.4,
  initial_weight: 1000,
  spool_weight: null,
  archived: false,
  ...over,
});

it("labels a spool by number, vendor and product", () => {
  expect(spoolLabel(spool(12, "PETG"))).toBe("#12 · Bambu Name 12");
  expect(spoolLabel(spool(3, "PLA", { filament: { ...spool(3, "PLA").filament, name: null, vendor: null } }))).toBe("#3 · PLA");
  expect(gramsLeft(spool(12, "PETG"))).toBe("640 g left");
  expect(gramsLeft(spool(12, "PETG", { remaining_weight: null }))).toBe("weight unknown");
  expect(gramsLeft(null)).toBe("weight unknown");
});

it("names slots and materials in the owner's words", () => {
  expect(slotTitle(source())).toBe("Slot 3");
  expect(slotTitle(source({ wire_slot: 5 }))).toBe("AMS B slot 2");
  expect(slotTitle(source({ wire_slot: 254 }))).toBe("External spool");
  expect(materialLine(source({ tagged: true }))).toBe("PETG from the tag");
  expect(materialLine(source())).toBe("PETG");
  expect(materialLine(source({ issues: ["source_empty"], material: "" }))).toBe("Empty");
  expect(sourceColor(source())).toBe("#FF0000");
  expect(sourceColor(source({ color: "" }))).toBeNull();
});

it("joins sources, assignments and spool records into rows, external last", () => {
  const status = {
    configured: true, api_version: 2, accounting_mode: "automatic", native_overlay_enabled: false,
    assignments: [
      { printer_id: "P1", source: "ams:abc:tray:2", spool_id: 12, assigned_at: 1, observed_identity: null, wire_slot: 2 },
      { printer_id: "P2", source: "external", spool_id: 99, assigned_at: 1, observed_identity: null, wire_slot: null },
    ],
  } as InventoryStatus;
  const rows = slotViews("P1", [source({ wire_slot: 254, source: "external" }), source()], status, [spool(12, "PETG")]);
  expect(rows.map((r) => r.source.wire_slot)).toEqual([2, 254]);
  expect(rows[0].assignment?.spool_id).toBe(12);
  expect(rows[0].spool?.id).toBe(12);
  expect(rows[1].assignment).toBeNull();
});

it("offers same-material spools first and other materials only to an untagged slot", () => {
  const spools = [spool(1, "PLA"), spool(2, "PETG"), spool(3, "PETG", { archived: true }), spool(4, "PETG"), spool(5, null)];
  const current = { printer_id: "P1", source: "ams:abc:tray:2", spool_id: 4, assigned_at: 1, observed_identity: null, wire_slot: 2 };
  const untagged = candidateSpools(source(), spools, current);
  expect(untagged.matching.map((s) => s.id)).toEqual([2]);
  expect(untagged.others.map((s) => s.id)).toEqual([1]);
  const tagged = candidateSpools(source({ tagged: true }), spools, null);
  expect(tagged.matching.map((s) => s.id)).toEqual([2, 4]);
  expect(tagged.others).toEqual([]);
  expect(candidateSpools(source(), spools, null, "name 4").matching.map((s) => s.id)).toEqual([4]);
});

it("turns failures into one plain sentence with a next step", () => {
  expect(inventoryErrorText(new BridgeNetworkError())).toBe("Couldn't reach the bridge. Try again.");
  expect(inventoryErrorText(new BridgeError({ error: "internal_error", message: "Spoolman: timeout" }, 503))).toBe("Spoolman didn't answer. Try again.");
  expect(inventoryErrorText(new BridgeError({ error: "conflict", message: "#12 is archived." }, 409))).toBe("#12 is archived.");
  expect(inventoryErrorText(new BridgeError({ error: "not_found", message: "Spoolman: not_found" }, 404))).toBe("Spoolman: not_found");
  expect(inventoryErrorText(new BridgeError({ error: "internal_error", message: "Spool inventory is not configured on this bridge" }, 503))).toBe("Spool inventory is not configured on this bridge");
  expect(inventoryErrorText(new BridgeError({ error: "x", message: "Spoolman didn't answer" }, 503))).toBe("Spoolman didn't answer");
  expect(inventoryErrorText(new Error("boom"))).toBe("boom");
});

it("warns before a print from a slot with no spool", () => {
  const { unassignedPick } = jest.requireActual("../slotInventory");
  const views = [
    { source: source(), assignment: null, spool: null },
    { source: source({ wire_slot: 0, source: "ams:abc:tray:0" }), assignment: { printer_id: "P1", source: "ams:abc:tray:0", spool_id: 1, assigned_at: 1, observed_identity: null, wire_slot: 0 }, spool: null },
    { source: source({ wire_slot: 254, source: "external", issues: ["source_empty"], material: "" }), assignment: null, spool: null },
  ];
  expect(unassignedPick(2, views)?.text).toBe("No spool in slot 3 · this print would go uncharged");
  expect(unassignedPick(0, views)).toBeNull();
  expect(unassignedPick(254, views)).toBeNull();
  expect(unassignedPick(1, views)).toBeNull();
});

it("says for an hour that the bridge put a spool in from its tag", () => {
  const { tagOriginNote } = jest.requireActual("../slotInventory");
  const base = { printer_id: "P1", source: "ams:abc:tray:1", spool_id: 7, observed_identity: null, wire_slot: 1 };
  const now = 1_792_800_000_000;
  expect(tagOriginNote({ ...base, assigned_at: now / 1000 - 60, origin: "rfid_new" }, now)).toBe("added from its tag");
  expect(tagOriginNote({ ...base, assigned_at: now / 1000 - 60, origin: "rfid_known" }, now)).toBe("matched by its tag");
  expect(tagOriginNote({ ...base, assigned_at: now / 1000 - 7200, origin: "rfid_new" }, now)).toBeNull();
  expect(tagOriginNote({ ...base, assigned_at: now / 1000 - 60, origin: "owner" }, now)).toBeNull();
  expect(tagOriginNote({ ...base, assigned_at: now / 1000 - 60 }, now)).toBeNull();
  expect(tagOriginNote(null, now)).toBeNull();
});

it("words a stale slot report", () => {
  const { staleLine } = jest.requireActual("../slotInventory");
  expect(staleLine(source())).toBeNull();
  expect(staleLine(source({ stale: true, age_seconds: 45.2, issues: ["ams_stale"] }))).toBe("last seen 45 s ago");
  expect(staleLine(source({ stale: true, age_seconds: 400, issues: ["ams_stale"] }))).toBe("last seen 7 min ago");
  expect(staleLine(source({ issues: ["ams_stale"] }))).toBe("last report is old");
});

it("fills the swatch with Spoolman's colour for an assigned slot and rings it with the tray colour", () => {
  const { swatchFor, percentFor } = jest.requireActual("../slotInventory");
  const galaxyBlack = spool(7, "PETG", { filament: { ...spool(7, "PETG").filament, color_hex: "1A1A1A" }, remaining_weight: 127, initial_weight: 1000 });
  const assigned = { source: source({ wire_slot: 3 }), assignment: { printer_id: "P1", source: "ams:abc:tray:3", spool_id: 7, assigned_at: 1, observed_identity: null, wire_slot: 3 }, spool: galaxyBlack };
  expect(swatchFor(assigned, "#F330F9")).toEqual({ fill: "#1A1A1A", ring: "#F330F9" });
  expect(swatchFor(assigned, "#1a1a1a")).toEqual({ fill: "#1A1A1A", ring: null });
  expect(swatchFor({ ...assigned, assignment: null, spool: null }, "#F330F9")).toEqual({ fill: "#F330F9", ring: null });
  expect(swatchFor(null, "F330F9FF")).toEqual({ fill: "#F330F9", ring: null });
  // The bar: Spoolman's figure for an assigned spool, the printer's for an unassigned slot.
  expect(percentFor(assigned, null)).toBe(13);
  expect(percentFor({ ...assigned, spool: { ...galaxyBlack, remaining_percent: 12 } }, 64)).toBe(12);
  expect(percentFor({ ...assigned, spool: { ...galaxyBlack, remaining_weight: 1000, initial_weight: null } }, null)).toBe(100);
  expect(percentFor({ ...assigned, assignment: null, spool: null }, 64)).toBe(64);
  expect(percentFor({ ...assigned, assignment: null, spool: null }, null)).toBeNull();
});
