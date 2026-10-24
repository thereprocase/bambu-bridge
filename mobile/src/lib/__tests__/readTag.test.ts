import type { InventorySource } from "../../api/inventory";
import { READ_TIMEOUT_MS, READING_TEXT, canRead, readOutcome, readableWires, sameReport } from "../readTag";

const src = (wire: number, over: Partial<InventorySource> = {}): InventorySource => ({
  printer_id: "P1", source: wire === 254 ? "external" : `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`,
  material: "PETG", color: "FF0000FF", observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false, ...over,
});

it("reads AMS trays only, in order, never the external holder", () => {
  expect(readableWires([src(254), src(2), src(0), src(1), src(3)])).toEqual([0, 1, 2, 3]);
});

it("notices when a slot's report moved", () => {
  expect(sameReport(src(0), src(0))).toBe(true);
  expect(sameReport(src(0), src(0, { tagged: true, observed_identity: "b".repeat(64) }))).toBe(false);
  expect(sameReport(src(0), src(0, { material: "ASA" }))).toBe(false);
  expect(sameReport(null, src(0))).toBe(false);
  expect(sameReport(null, null)).toBe(true);
});

it("says what the read found", () => {
  expect(readOutcome(1, src(1, { tagged: true }), "Bambu PETG Translucent", false)).toBe("Tag read: Bambu PETG Translucent");
  expect(readOutcome(1, src(1, { tagged: true }), "Bambu Translucent", false)).toBe("Tag read: Bambu Translucent PETG");
  expect(readOutcome(1, src(1, { tagged: true }), null, false)).toBe("Tag read: PETG");
  expect(readOutcome(1, src(1, { tagged: true }), "PETG", false)).toBe("Tag read: PETG");
  expect(readOutcome(1, src(1), null, true)).toBe("No tag found in slot 2 · reports PETG");
  expect(readOutcome(1, src(1, { issues: ["source_empty"], material: "" }), null, true)).toBe("slot 2 is empty");
  expect(readOutcome(1, null, null, true)).toBe("No tag found in slot 2");
});

it("allows a read only while idle and not already reading that slot", () => {
  const reading = { wire: 0, startedAt: 0, before: null, state: "reading" as const, text: "Reading…" };
  expect(canRead(true, {}, 0)).toBe(true);
  expect(canRead(false, {}, 0)).toBe(false);
  expect(canRead(true, { 0: reading }, 0)).toBe(false);
  expect(canRead(true, { 0: reading }, 1)).toBe(true);
});

it("waits long enough for the measured read and says so", () => {
  // Measured 48 s on the P1S; the budget is per slot, so Read all tags is four of these at most.
  expect(READ_TIMEOUT_MS).toBe(75_000);
  expect(READING_TEXT).toBe("Reading tag… (about a minute)");
});
