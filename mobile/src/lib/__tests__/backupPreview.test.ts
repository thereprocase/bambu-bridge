import { previewBackup } from "../../api/inventory";
import { request } from "../../api/client";
import { BridgeError } from "../../api/errors";
import { changeLines, previewSteps, ringAfter, stepHeading } from "../backupPreview";

jest.mock("../../api/client", () => ({ request: jest.fn() }));
const mockedRequest = request as jest.MockedFunction<typeof request>;

it("reads the per-field shape and the whole-declaration shape alike", () => {
  const perField = previewSteps({ steps: [{ wire: 3, changes: [{ field: "tray_color", before: "161616FF", after: "FFFFFFFF" }] }] });
  const whole = previewSteps({ plan: { steps: [{ wire: 3, before: { tray_color: "161616FF", tray_type: "PETG" }, target: { tray_color: "FFFFFFFF", tray_type: "PETG" } }] } });
  expect(perField).toEqual(whole);
  expect(perField).toEqual([{ wire: 3, writes: true, changes: [{ field: "tray_color", before: "161616FF", after: "FFFFFFFF" }] }]);
});

it("drops what it cannot read instead of guessing", () => {
  expect(previewSteps(null)).toEqual([]);
  expect(previewSteps({ steps: "no" })).toEqual([]);
  expect(previewSteps({ steps: [{ wire: "3", changes: [] }, { wire: 1 }, null] })).toEqual([]);
});

it("words each change, one nozzle range, what it is before how it looks", () => {
  expect(changeLines([
    { field: "tray_color", before: "161616FF", after: "ffffffff" },
    { field: "nozzle_temp_max", before: 270, after: 260 },
    { field: "tray_type", before: "PLA", after: "PETG" },
    { field: "nozzle_temp_min", before: 220, after: 230 },
    { field: "tray_info_idx", before: "", after: "GFG99" },
  ])).toEqual([
    "Material: PLA → PETG",
    "Filament profile: none → GFG99",
    "Nozzle: 220–270 °C → 230–260 °C",
    "Colour: #161616 → #FFFFFF",
  ]);
  expect(changeLines([{ field: "nozzle_temp_min", before: 220, after: 230 }])).toEqual(["Lowest nozzle: 220 → 230 °C"]);
  expect(changeLines([{ field: "nozzle_temp_max", before: 270, after: 260 }])).toEqual(["Highest nozzle: 270 → 260 °C"]);
});

it("heads each slot by what happens to it", () => {
  expect(stepHeading({ wire: 1, writes: false, changes: [] })).toBe("Slot 2 · not written");
  expect(stepHeading({ wire: 2, writes: true, changes: [] })).toBe("Slot 3 · stays as it is");
  const step = { wire: 3, writes: true, changes: [{ field: "tray_color", before: "161616FF", after: "FFFFFFFF" }] };
  expect(stepHeading(step)).toBe("Slot 4 · changes");
  expect(ringAfter(step)).toBe("#FFFFFF");
  expect(ringAfter({ wire: 3, writes: true, changes: [] })).toBeNull();
});

describe("previewBackup", () => {
  beforeEach(() => mockedRequest.mockReset());

  it("asks the backup route first", async () => {
    mockedRequest.mockResolvedValueOnce({ steps: [] });
    await previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" });
    expect(mockedRequest).toHaveBeenCalledTimes(1);
    expect(mockedRequest).toHaveBeenCalledWith("/inventory/printers/P1/refill/backup/preview", { method: "POST", body: { from_wire: 2, to_wire: 3 } });
  });

  it("falls back to the general preview on a bridge without the route", async () => {
    mockedRequest.mockRejectedValueOnce(new BridgeError({ error: "not_found", message: "Not Found" }, 404)).mockResolvedValueOnce({ plan: { steps: [] } });
    await previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" });
    expect(mockedRequest).toHaveBeenLastCalledWith("/inventory/printers/P1/refill/preview", { method: "POST", body: { wires: [2, 3], color: "FFFFFFFF" } });
  });

  it("passes a refusal straight through", async () => {
    const refusal = new BridgeError({ error: "invalid_input", message: "Slot 4 holds an RFID spool" }, 422);
    mockedRequest.mockRejectedValueOnce(refusal);
    await expect(previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" })).rejects.toBe(refusal);
    expect(mockedRequest).toHaveBeenCalledTimes(1);
  });
});

it("heads an anchor by the bridge's flags, whichever slot it is", () => {
  const tagged = previewSteps({ steps: [
    { wire: 2, writes: true, anchor: false, changes: [{ field: "tray_color", before: "FFFFFFFF", after: "8B5A2BFF" }] },
    { wire: 1, writes: false, anchor: true, tagged: true, changes: [] },
  ] });
  expect(tagged.map(stepHeading)).toEqual(["Slot 3 · changes", "Slot 2 · RFID spool, not written; the other slot copies it"]);
  expect(stepHeading({ wire: 2, writes: false, anchor: true, changes: [] })).toBe("Slot 3 · stays as it is; the other slot copies it");
});

it("keeps only the backup route's digest", async () => {
  const digest = "a".repeat(64);
  mockedRequest.mockReset().mockResolvedValueOnce({ steps: [], preview_digest: digest });
  expect((await previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" })).digest).toBe(digest);
  mockedRequest.mockReset()
    .mockRejectedValueOnce(new BridgeError({ error: "not_found", message: "Not Found" }, 404))
    .mockResolvedValueOnce({ plan: { steps: [] }, preview_digest: digest });
  expect((await previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" })).digest).toBeNull();
});

describe("preview edges", () => {
  it("drops malformed changes and treats a missing writes flag as written", () => {
    expect(previewSteps({ steps: [{ wire: 3, changes: [null, { before: 1 }, { field: "tray_type", before: "PLA", after: "PETG" }] }] }))
      .toEqual([{ wire: 3, writes: true, changes: [{ field: "tray_type", before: "PLA", after: "PETG" }] }]);
    expect(previewSteps({ steps: [{ wire: 3, writes: "no", changes: [] }] })[0].writes).toBe(true);
    // Per-field changes win over before/target when a bridge sends both.
    expect(previewSteps({ steps: [{ wire: 3, changes: [], before: { tray_color: "A" }, target: { tray_color: "B" } }] })[0].changes).toEqual([]);
  });

  it("shows a change in the alpha byte alone", () => {
    expect(changeLines([{ field: "tray_color", before: "FFFFFFFF", after: "FFFFFF00" }])).toEqual(["Colour: #FFFFFFFF → #FFFFFF00"]);
    expect(changeLines([{ field: "tray_color", before: "161616FF", after: "FFFFFFFF" }])).toEqual(["Colour: #161616 → #FFFFFF"]);
  });

  it("words an unknown field plainly rather than hiding it", () => {
    expect(changeLines([{ field: "tray_sub_brands", before: "", after: "Rapid" }])).toEqual(["tray_sub_brands: none → Rapid"]);
  });
});

describe("preview edge cases", () => {
  it("omits a field the bridge lists without changing it, colours in either format included", () => {
    const [step] = previewSteps({ steps: [{ wire: 3, changes: [
      { field: "tray_info_idx", before: "GFG99", after: "GFG99" },
      { field: "tray_color", before: "ffffff", after: "FFFFFFFF" },
      { field: "nozzle_temp_min", before: 230, after: "230" },
    ] }] });
    expect(step.changes).toEqual([]);
    expect(stepHeading(step)).toBe("Slot 4 · stays as it is");
    expect(previewSteps({ steps: [{ wire: 3, before: { tray_color: "161616" }, target: { tray_color: "161616FF" } }] })[0].changes).toEqual([]);
  });

  it("words a blank on either side as none", () => {
    expect(changeLines(previewSteps({ steps: [{ wire: 3, changes: [{ field: "tray_info_idx", before: null, after: "GFG99" }] }] })[0].changes))
      .toEqual(["Filament profile: none → GFG99"]);
    expect(changeLines(previewSteps({ steps: [{ wire: 3, before: { tray_type: "PETG" }, target: { tray_type: "PETG", tray_info_idx: "GFG99" } }] })[0].changes))
      .toEqual(["Filament profile: none → GFG99"]);
  });

  it("shows a reversed or half range as the bridge sent it", () => {
    expect(changeLines([{ field: "nozzle_temp_min", before: 260, after: 270 }, { field: "nozzle_temp_max", before: 230, after: 220 }]))
      .toEqual(["Nozzle: 260–230 °C → 270–220 °C"]);
  });

  it("shows a value that is not a colour as sent, and an RGB colour as opaque", () => {
    expect(changeLines([{ field: "tray_color", before: "red", after: "FFFFFFFF" }])).toEqual(["Colour: red → #FFFFFF"]);
    expect(changeLines([{ field: "tray_color", before: "161616", after: "FFFFFF00" }])).toEqual(["Colour: #161616FF → #FFFFFF00"]);
    expect(ringAfter({ wire: 3, writes: true, changes: [{ field: "tray_color", before: "x", after: "nope" }] })).toBeNull();
  });

  it("falls back to the general preview on 404 or 405 only", async () => {
    for (const status of [404, 405]) {
      mockedRequest.mockReset().mockRejectedValueOnce(new BridgeError({ error: "not_found", message: "x" }, status)).mockResolvedValueOnce({ plan: { steps: [] } });
      await previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" });
      expect(mockedRequest).toHaveBeenCalledTimes(2);
    }
    for (const failure of [new BridgeError({ error: "internal_error", message: "boom" }, 500), new BridgeError({ error: "conflict", message: "busy" }, 409), new Error("network")]) {
      mockedRequest.mockReset().mockRejectedValueOnce(failure);
      await expect(previewBackup("P1", { from_wire: 2, to_wire: 3, from_color: "FFFFFFFF" })).rejects.toBe(failure);
      expect(mockedRequest).toHaveBeenCalledTimes(1);
    }
  });
});
