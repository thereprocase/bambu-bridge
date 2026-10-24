/**
 * The app's refill and slot-guard words over real bridge answers.
 *
 * fixtures/bridge-refill-ccef4b6.json was captured from the bridge driving a
 * loopback mock P1S; nothing is hand-written. Recapture it when the bridge's
 * refill or slot-guard response shapes change.
 */
import type { BridgeEnvelope } from "../../api/errors";
import type { RefillStatus } from "../../api/inventory";
import { backupLine, currentBackup } from "../backup";
import { changeLines, previewSteps, ringAfter, stepHeading } from "../backupPreview";
import { slotGuard, slotWriteDone } from "../slotGuard";
const captured = require("./fixtures/bridge-refill-ccef4b6.json");

const body = (name: string) => captured[name].body;
const rows = (name: string) => {
  const status = body(name) as RefillStatus;
  const chain = currentBackup(status);
  return [backupLine(2, chain, status), backupLine(3, chain, status)];
};

it("reads the backup preview: slot 3 anchors, slot 4 takes its whole filament", () => {
  const steps = previewSteps(body("preview_2_3"));
  expect(steps.map(stepHeading)).toEqual(["Slot 3 · stays as it is; the other slot copies it", "Slot 4 · changes"]);
  expect(changeLines(steps[1].changes)).toEqual([
    "Filament profile: GFG02 → GFG99",
    "Nozzle: 220–270 °C → 230–260 °C",
    "Colour: #161616 → #FFFFFF",
  ]);
  expect(ringAfter(steps[1])).toBe("#FFFFFF");
  expect(body("preview_2_3").preview_digest).toMatch(/^[0-9a-f]{64}$/);
});

it("reads a tagged backup anchoring its primary", () => {
  const steps = previewSteps(body("preview_tagged_backup"));
  expect(steps.map(stepHeading)).toEqual(["Slot 3 · changes", "Slot 2 · RFID spool, not written; the other slot copies it"]);
  expect(changeLines(steps[0].changes)).toEqual(["Filament profile: GFG99 → GFG01", "Colour: #FFFFFF → #8B5A2B"]);
});

it("shows the bridge's refusals as they come", () => {
  expect(captured.preview_material_refused.status).toBe(422);
  expect(body("preview_material_refused").message).toBe("Slot 1 is PLA and slot 3 is PETG; a backup needs the same material. Load a matching spool or set the slot's material first.");
  expect(captured.put_stale_digest.status).toBe(409);
  expect(body("put_stale_digest").message).toBe("Physical refill sources changed; review a new preview");
});

it("words both rows the same through set, confirm, a quiet printer and back", () => {
  expect(rows("put_backup")).toEqual(["Backup: slot 4 · waiting for the printer to confirm", "Backs up slot 3 · waiting for the printer to confirm"]);
  expect(rows("refill_confirmed")).toEqual(["Backup: slot 4 · confirmed", "Backs up slot 3 · confirmed"]);
  // 36 s without a frame: the bridge says pending, stale, and the rows say how long ago it was confirmed.
  expect(rows("refill_quiet")).toEqual(["Backup: slot 4 · last confirmed 36 s ago", "Backs up slot 3 · last confirmed 36 s ago"]);
  expect(rows("refill_back")).toEqual(["Backup: slot 4 · confirmed", "Backs up slot 3 · confirmed"]);
  expect(rows("refill_after_delete")).toEqual([null, null]);
});

it("lists what the standing backup changed from the status steps", () => {
  const applied = previewSteps({ steps: body("refill_confirmed").steps }).filter((s) => s.changes.length);
  expect(applied.map((s) => s.wire)).toEqual([3]);
  expect(changeLines(applied[0].changes)).toContain("Filament profile: GFG02 → GFG99");
});

it("words the raw write guard from the real refusals and receipt", () => {
  const same = slotGuard(body("raw_refused_same_material") as BridgeEnvelope, { tray_type: "PETG" })!;
  expect(same.title).toBe("Slot 3 is in use");
  expect(same.lines).toEqual([
    "Slot 3 holds spool #8. Writing here changes what the printer declares for it.",
    "Spool #8 stays assigned to slot 3.",
    "Slot 3 is part of the slot 3 → slot 4 backup. Writing here makes the backup need you.",
    "To change it, remove the backup first (Filament → slot 3 → Backup → Remove backup); that puts both slots back.",
  ]);
  expect(same.overrideLabel).toBe("Write anyway");
  const pla = slotGuard(body("raw_refused_new_material") as BridgeEnvelope, { tray_type: "PLA" })!;
  expect(pla.lines[1]).toBe("This changes slot 3 to PLA, which ends spool #8's assignment in that slot. You would assign it again on Filament.");
  expect(pla.overrideLabel).toBe("Write anyway and unassign #8");
  expect(slotWriteDone(body("raw_override").guard)).toBe("Written anyway · spool still assigned · the backup may need you, check Filament");
  expect(slotWriteDone(body("raw_unassigned_slot").guard)).toBe("Filament setting sent");
});
