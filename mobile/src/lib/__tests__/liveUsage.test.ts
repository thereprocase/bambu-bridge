import type { InventorySpool } from "../../api/inventory";
import { usageLines } from "../liveUsage";

const spool = (id: number, name: string): InventorySpool => ({
  id, archived: false, remaining_weight: 400, initial_weight: 1000, spool_weight: null,
  filament: { id, name, vendor: { id: 1, name: "Bambu", empty_spool_weight: null }, material: "PETG", color_hex: null, weight: 1000, spool_weight: null, density: null, diameter: 1.75 },
});

it("says nothing when no print is running", () => {
  expect(usageLines({ printing: false }, [])).toBeNull();
});

it("reads as one line for a single-spool print, with what Spoolman was sent", () => {
  const lines = usageLines(
    { printing: true, attempt_id: "a", planned_grams: 83.78, slots: [{ wire_slot: 254, spool_id: 5, used_g: 23.4, written_g: 19.3, state: "open" }] },
    [spool(5, "PET-GF")],
  )!;
  expect(lines.headline).toBe("Filament · 23 g used · 60 g to go");
  expect(lines.slots).toEqual([]);
  expect(lines.sent).toBe("Sent to Spoolman: 19 g");
});

it("lists each slot for a multi-spool print and flags a slot with no spool", () => {
  const lines = usageLines(
    {
      printing: true, attempt_id: "a", planned_grams: null,
      slots: [
        { wire_slot: 0, spool_id: 1, used_g: 10, written_g: 0, state: null },
        { wire_slot: 1, spool_id: null, used_g: 2.25, written_g: 0, state: null },
      ],
    },
    [spool(1, "Brown")],
  )!;
  expect(lines.headline).toBe("Filament · 12 g used");
  expect(lines.slots).toEqual([
    { text: "Slot 1 · #1 · Bambu Brown · 10 g", unassigned: false },
    { text: "Slot 2 · no spool assigned · 2 g", unassigned: true },
  ]);
  expect(lines.sent).toBeNull();
});

it("never shows a negative remainder when the count passes the plan", () => {
  const lines = usageLines(
    { printing: true, planned_grams: 10, slots: [{ wire_slot: 0, spool_id: 1, used_g: 12, written_g: 12, state: "done" }] },
    [],
  )!;
  expect(lines.headline).toBe("Filament · 12 g used · 0 g to go");
});
