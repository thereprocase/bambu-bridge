import type { AccountingRow, Shortfall } from "../../api/inventory";
import { chargeable, chargesFor, gramsText, parseGrams, recentLine, shortfallLine, shortfallQuestion, slotName } from "../heldPrints";

const sf = (over: Partial<Shortfall>): Shortfall => ({ kind: "unassigned_slot", wire_slot: 0, spool_id: null, grams: 83.78, material: "PETG", ...over });
const row = (over: Partial<AccountingRow>): AccountingRow => ({
  attempt_id: "a", printer_id: "P1", title: "Light & Fast V2", created_at: 1, ended_at: 2, capture_id: null, start_state: "running",
  outcome: "completed", delivery_state: "delivered", charged: [], shortfalls: [], review_reason: null, planned_grams: 83.78, ...over,
});

it("states each shortfall in one line and asks one question", () => {
  expect(shortfallLine(sf({}))).toBe("84 g from slot 1 · no spool was assigned");
  expect(shortfallQuestion(sf({}))).toBe("Which spool was in slot 1?");
  expect(shortfallLine(sf({ wire_slot: 254 }))).toBe("84 g from the external spool · no spool was assigned");
  expect(shortfallLine(sf({ kind: "spool_empty", spool_id: 5, grams: 12 }))).toBe("12 g short · #5 ran out in Spoolman");
  expect(shortfallQuestion(sf({ kind: "spool_empty", spool_id: 5 }))).toBe("Charge the rest to which spool?");
  expect(shortfallLine(sf({ kind: "delivery_error", spool_id: 5, grams: 40.2 }))).toBe("40 g for #5 · Spoolman didn't answer");
  expect(shortfallQuestion(sf({ kind: "delivery_error", spool_id: 5 }))).toBe("Charge #5 now?");
  expect(shortfallLine(sf({ kind: "unweighed_filament", grams: 0 }))).toBe("A filament the file gave no weight for");
  expect(shortfallLine(sf({ kind: "no_layer_plan", wire_slot: null }))).toBe("84 g by the slicer's figure · the bridge counted no layers");
  expect(shortfallLine(sf({ kind: "something_else" }))).toBe("84 g uncharged");
  expect(slotName(null)).toBe("a slot");
});

it("summarises a recent print", () => {
  expect(recentLine(row({ charged: [{ spool_id: 5, grams: 83.78 }] }))).toBe("84 g → #5");
  expect(recentLine(row({ delivery_state: "review", shortfalls: [sf({ grams: 40.2 })] }))).toBe("40 g waiting for you");
  expect(recentLine(row({ charged: [{ spool_id: 5, grams: 43.5 }], shortfalls: [sf({ grams: 40.2 })] }))).toBe("44 g → #5");
  expect(recentLine(row({ shortfalls: [sf({ grams: 40.2 })] }))).toBe("40 g left uncharged");
  expect(recentLine(row({ charged: [{ spool_id: 3, grams: 83.78 }], shortfalls: [sf({})], resolution: { action: "charge", resolved_at: 3 } }))).toBe("84 g → #3 · settled by you");
  expect(recentLine(row({ shortfalls: [sf({ grams: 40.2 })], resolution: { action: "nothing", resolved_at: 3 } }))).toBe("40 g skipped");
  expect(recentLine(row({ shortfalls: [sf({})], resolution: { action: "charged_elsewhere", resolved_at: 3 } }))).toBe("charged by hand in Spoolman");
  expect(recentLine(row({ review_reason: "stopped_before_first_layer", outcome: "stopped" }))).toBe("stopped before the first layer · 0 g");
  expect(recentLine(row({}))).toBe("nothing to charge");
});

it("builds slot charges when the counted figure stands and gram charges when edited", () => {
  const charges = chargesFor([
    { shortfall: sf({}), spoolId: 1, gramsText: "84" },
    { shortfall: sf({ wire_slot: 2, grams: 10 }), spoolId: 3, gramsText: "9" },
    { shortfall: sf({ kind: "spool_empty", spool_id: 5, wire_slot: null, grams: 12 }), spoolId: 2, gramsText: "12" },
    { shortfall: sf({ kind: "unweighed_filament", wire_slot: null, grams: 0 }), spoolId: 4, gramsText: "" },
    { shortfall: sf({ wire_slot: 3 }), spoolId: null, gramsText: "84" },
  ]);
  expect(charges).toEqual([
    { spool_id: 1, slot: 0 },
    { spool_id: 3, grams: 9 },
    { spool_id: 2, grams: 12 },
  ]);
});

it("keeps only whole grams from a keystroke and never inflates a figure", () => {
  expect(gramsText("10.5")).toBe("10");
  expect(gramsText("1e308")).toBe("1");
  expect(gramsText("-5")).toBe("");
  expect(gramsText("")).toBe("");
  expect(gramsText("12,5")).toBe("12");
  expect(gramsText("99999")).toBe("10000");
  expect(parseGrams("0")).toBeNull();
  expect(parseGrams("")).toBeNull();
  expect(parseGrams("10000")).toBe(10000);
  expect(parseGrams("10001")).toBeNull();
  expect(Number.isFinite(parseGrams("1e308") ?? 0)).toBe(true);
});

it("offers Charge only when every picked spool has usable grams", () => {
  expect(chargeable([{ shortfall: sf({}), spoolId: 1, gramsText: "0" }])).toBe(false);
  expect(chargeable([{ shortfall: sf({}), spoolId: 1, gramsText: "" }])).toBe(false);
  expect(chargeable([{ shortfall: sf({}), spoolId: null, gramsText: "84" }])).toBe(false);
  expect(chargeable([{ shortfall: sf({}), spoolId: 1, gramsText: "84" }, { shortfall: sf({ wire_slot: 1 }), spoolId: 2, gramsText: "" }])).toBe(false);
  expect(chargeable([{ shortfall: sf({}), spoolId: 1, gramsText: "84" }])).toBe(true);
});

it("words a refill-split print by layer ranges and a spool's history row", () => {
  const { splitLine, historyLine } = jest.requireActual("../heldPrints");
  expect(splitLine(row({ charged: [{ spool_id: 12, grams: 60, layers: [41, 186] }, { spool_id: 11, grams: 24, layers: [1, 40] }] }))).toBe("layers 1–40 from #11 · 41–186 from #12");
  expect(splitLine(row({ charged: [{ spool_id: 12, grams: 60 }] }))).toBeNull();
  expect(historyLine({ layers: [41, 186], total_layers: 186, share_pct: 77.4, grams: 12.2 })).toBe("layers 41–186 · 77% · 12 g");
  expect(historyLine({ layers: [1, 186], total_layers: 186, share_pct: 100, grams: 84 })).toBe("layers 1–186 · 84 g");
  expect(historyLine({ layers: null, total_layers: null, share_pct: null, grams: 84 })).toBe("84 g");
  expect(historyLine({ layers: [1, 6], total_layers: 10, share_pct: 100, grams: 5 })).toBe("layers 1–6 of 10 · 100% · 5 g");
});
