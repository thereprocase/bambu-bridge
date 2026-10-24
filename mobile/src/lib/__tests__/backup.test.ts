import type { InventorySource } from "../../api/inventory";
import { backupCandidates, backupLine, backupSettling, backupStatus, canOfferBackup, chainInUse, lastConfirmed, slotWord } from "../backup";
import type { SlotView } from "../slotInventory";

const src = (wire: number, over: Partial<InventorySource> = {}): InventorySource => ({
  printer_id: "P1", source: wire === 254 ? "external" : `ams:abc:tray:${wire}`, wire_slot: wire, label: `AMS A · slot ${wire + 1}`,
  material: "PETG", color: "FF0000FF", observed_identity: "a".repeat(64), observed_at: 1, assignable: true, issues: [], tagged: false, ...over,
});
const view = (wire: number, over: Partial<InventorySource> = {}, assigned = true): SlotView => ({
  source: src(wire, over),
  assignment: assigned ? { printer_id: "P1", source: src(wire).source, spool_id: wire + 1, assigned_at: 1, observed_identity: null, wire_slot: wire } : null,
  spool: null,
});

it("offers loaded, same-material AMS slots as backups, at most one of the pair tagged", () => {
  const views = [view(0), view(1), view(2, { material: "ASA" }), view(3, { tagged: true }), view(254, { material: "PETG" })];
  // A tagged slot anchors the pair: the untagged one copies its tag.
  expect(backupCandidates(views[0], views).map((v) => v.source.wire_slot)).toEqual([1, 3]);
  expect(canOfferBackup(views[0], views)).toBe(true);
  expect(backupCandidates(views[3], views).map((v) => v.source.wire_slot)).toEqual([0, 1]);
  expect(canOfferBackup(views[3], views)).toBe(true);
  // Two tags can't copy each other, and a tag still being read has nothing to copy yet.
  const tags = [view(0, { tagged: true }), view(1, { tagged: true }), view(2, { tag_reading: true })];
  expect(backupCandidates(tags[0], tags)).toEqual([]);
  expect(canOfferBackup(tags[2], [...tags, view(3)])).toBe(false);
  expect(canOfferBackup(view(0, {}, false), views)).toBe(false); // no spool assigned
  expect(canOfferBackup(views[2], views)).toBe(false); // no other ASA slot
  const empty = [view(0), view(1, { issues: ["source_empty"], material: "" })];
  expect(canOfferBackup(empty[0], empty)).toBe(false);
});

it("words the chain on both rows and blocks a second chain", () => {
  const active = { from_wire: 0, to_wire: 1, state: "active", message: null };
  expect(backupLine(0, active)).toBe("Backup: slot 2 · confirmed");
  expect(backupLine(1, active)).toBe("Backs up slot 1 · confirmed");
  expect(backupLine(2, active)).toBeNull();
  expect(backupLine(0, { ...active, state: "needs_attention" })).toBe("Backup: slot 2 · needs you");
  expect(backupLine(1, { ...active, state: "needs_attention" })).toBe("Backs up slot 1 · needs you");
  expect(backupLine(0, null)).toBeNull();
  expect(chainInUse(2, active)).toBe("Backup in use: slot 1 → slot 2. Remove it first.");
  expect(chainInUse(0, active)).toBeNull();
  expect(chainInUse(2, null)).toBeNull();
});

it("gives both rows one status, naming waits that never end on their own", () => {
  const chain = { from_wire: 2, to_wire: 3, state: "pending", message: null };
  for (const [state, words] of [
    ["active", "confirmed"],
    ["pending", "waiting for the printer to confirm"],
    ["restoring", "being removed"],
    ["restoration_pending", "being removed"],
    ["needs_attention", "needs you"],
    ["something_new", null],
  ] as const) {
    expect(backupStatus({ ...chain, state })).toBe(words);
    const suffix = words ? ` · ${words}` : "";
    expect(backupLine(2, { ...chain, state })).toBe(`Backup: slot 4${suffix}`);
    expect(backupLine(3, { ...chain, state })).toBe(`Backs up slot 3${suffix}`);
  }
  expect(backupStatus({ ...chain, message: "Turn on AMS auto-refill on the printer" })).toBe("turn on AMS auto-refill on the printer");
  const offline = { backups: [chain], printer_auto_refill: null, printer_connected: false };
  expect(backupStatus(chain, offline)).toBe("printer offline");
  // Offline only rewords the wait; a chain that needs the owner still says so.
  expect(backupStatus({ ...chain, state: "needs_attention" }, offline)).toBe("needs you");
  expect(backupSettling(null)).toBe(false);
  expect(backupSettling({ ...chain, state: "active" })).toBe(false);
  expect(backupSettling(chain)).toBe(true);
  expect(backupSettling({ ...chain, state: "restoring" })).toBe(true);
});

it("tells a quiet printer from one still confirming", () => {
  const chain = { from_wire: 2, to_wire: 3, state: "pending", message: null };
  // As seen live: idle printer, no frame for a while, last report still mask 12 and auto-refill on.
  const quiet = {
    backups: [chain], printer_connected: true, printer_auto_refill: null, last_reported_auto_refill: true,
    eligibility_fresh: false, eligibility_age_seconds: 140, reported_backup_masks: [12],
  };
  expect(lastConfirmed(chain, quiet)).toEqual({ ageSeconds: 140 });
  expect(backupLine(2, chain, quiet)).toBe("Backup: slot 4 · last confirmed 2 min ago");
  expect(backupLine(3, chain, quiet)).toBe("Backs up slot 3 · last confirmed 2 min ago");
  expect(backupStatus(chain, { ...quiet, eligibility_age_seconds: 45 })).toBe("last confirmed 45 s ago");
  expect(backupStatus(chain, { ...quiet, eligibility_age_seconds: null })).toBe("last confirmed a while ago");
  // Right after the write the frames are fresh and the mask is not there yet: a real wait.
  expect(backupStatus(chain, { ...quiet, eligibility_fresh: true, reported_backup_masks: [8] })).toBe("waiting for the printer to confirm");
  // A stale report of a different pairing, or of auto-refill off, confirms nothing.
  expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: [3] })).toBeNull();
  expect(lastConfirmed(chain, { ...quiet, last_reported_auto_refill: false })).toBeNull();
  expect(lastConfirmed(chain, { ...quiet, printer_connected: false })).toBeNull();
  expect(lastConfirmed({ ...chain, state: "needs_attention" }, quiet)).toBeNull();
  // An older bridge without these fields keeps the plain wait.
  expect(backupStatus(chain, { backups: [chain], printer_connected: true, printer_auto_refill: true })).toBe("waiting for the printer to confirm");
  // The group's own wires make the mask when the bridge lists them.
  expect(lastConfirmed(chain, { ...quiet, steps: [{ wire: 1, state: "owned", before: {}, target: {} }, { wire: 3, state: "owned", before: {}, target: {} }], reported_backup_masks: [10] }))
    .toEqual({ ageSeconds: 140 });
});

describe("edges", () => {
  const chain = { from_wire: 2, to_wire: 3, state: "pending", message: null };
  const quiet = {
    backups: [chain], printer_connected: true, printer_auto_refill: null, last_reported_auto_refill: true,
    eligibility_fresh: false, eligibility_age_seconds: 140, reported_backup_masks: [12] as number[] | null,
  };

  it("matches the pair's mask exactly, among others, and nothing looser", () => {
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: [3, 12] })).not.toBeNull();
    // A wider group (slots 2-4) is a different pairing to the printer.
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: [14] })).toBeNull();
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: [8] })).toBeNull();
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: [] })).toBeNull();
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: null })).toBeNull();
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: ["12"] as unknown as number[] })).toBeNull();
    expect(lastConfirmed(chain, null)).toBeNull();
    expect(lastConfirmed({ ...chain, state: "active" }, quiet)).toBeNull();
    // eligibility_fresh missing (older bridge) is not "stale".
    const { eligibility_fresh: _fresh, ...older } = quiet;
    expect(lastConfirmed(chain, older)).toBeNull();
  });

  it("words the age at each boundary", () => {
    const at = (age: number | null) => backupStatus(chain, { ...quiet, eligibility_age_seconds: age });
    expect(at(0)).toBe("last confirmed 0 s ago");
    expect(at(89)).toBe("last confirmed 89 s ago");
    expect(at(90)).toBe("last confirmed 2 min ago");
    expect(at(89 * 60)).toBe("last confirmed 89 min ago");
    expect(at(90 * 60)).toBe("last confirmed 2 h ago");
    expect(at(-5)).toBe("last confirmed a while ago");
  });

  it("matches material whatever its case or padding", () => {
    const views = [view(0, { material: " petg " }), view(1, { material: "PETG" }), view(2, { material: "Petg\n" }), view(3, { material: "" })];
    expect(backupCandidates(views[0], views).map((v) => v.source.wire_slot)).toEqual([1, 2]);
    expect(backupCandidates(views[3], views)).toEqual([]); // no material, no match, not even with another blank
    expect(backupCandidates(view(1, { material: "  " }), [view(1, { material: "  " }), view(2, { material: "" })])).toEqual([]);
  });

  it("words the end slots exactly", () => {
    const ends = { from_wire: 0, to_wire: 3, state: "active", message: null };
    expect(backupLine(0, ends)).toBe("Backup: slot 4 · confirmed");
    expect(backupLine(3, ends)).toBe("Backs up slot 1 · confirmed");
    expect(backupLine(1, ends)).toBeNull();
    expect(backupLine(2, ends)).toBeNull();
    expect(chainInUse(1, ends)).toBe("Backup in use: slot 1 → slot 4. Remove it first.");
  });
});

describe("remaining edges", () => {
  const chain = { from_wire: 2, to_wire: 3, state: "pending", message: null };
  const quiet = {
    backups: [chain], printer_connected: true as boolean | undefined, printer_auto_refill: null, last_reported_auto_refill: true as boolean | null,
    eligibility_fresh: false as boolean | undefined, eligibility_age_seconds: 140 as number | null, reported_backup_masks: [12] as number[] | null,
  };

  it("needs auto-refill reported on, a mask exactly the pair's, and reads the pair from steps or the chain", () => {
    expect(lastConfirmed(chain, { ...quiet, last_reported_auto_refill: null })).toBeNull();
    expect(lastConfirmed(chain, { ...quiet, reported_backup_masks: [4] })).toBeNull(); // only slot 3
    expect(lastConfirmed(chain, { ...quiet, steps: [] })).not.toBeNull(); // empty steps: the chain's own wires
    expect(lastConfirmed(chain, { ...quiet, printer_connected: undefined as unknown as boolean })).not.toBeNull();
    expect(backupStatus(chain, { ...quiet, eligibility_age_seconds: Number.NaN })).toBe("last confirmed a while ago");
  });

  it("gives both rows the same status for every state, freshness, connection and mask", () => {
    for (const state of ["active", "pending", "needs_attention", "restoring", "restoration_pending", "other"])
      for (const fresh of [true, false, undefined])
        for (const connected of [true, false])
          for (const masks of [[12], [14], [], null]) {
            const status = { ...quiet, eligibility_fresh: fresh, printer_connected: connected, reported_backup_masks: masks };
            const b = { ...chain, state };
            const tail = (line: string | null) => line?.replace(/^(Backup: slot 4|Backs up slot 3)/, "");
            expect(tail(backupLine(2, b, status))).toBe(tail(backupLine(3, b, status)));
            expect(backupLine(2, b, status)).toMatch(/^Backup: slot 4/);
            expect(backupLine(3, b, status)).toMatch(/^Backs up slot 3/);
          }
  });

  it("offers an unassigned slot and skips a tag being read as a candidate", () => {
    const views = [view(0), view(1, {}, false), view(2, { tag_reading: true })];
    expect(backupCandidates(views[0], views).map((v) => v.source.wire_slot)).toEqual([1]);
  });

  it("keeps a second AMS out of a backup, which the bridge drives on the first only", () => {
    const views = [view(0), view(4), view(5)];
    expect(backupCandidates(views[0], views).map((v) => v.source.wire_slot)).toEqual([]);
    expect(canOfferBackup(views[1], views)).toBe(false);
    // Rows number every slot wire + 1 ("Slot 6" for AMS B slot 2); the words match the row title.
    expect(slotWord(5)).toBe("slot 6");
  });
});

describe("the bridge's own staleness", () => {
  const chain = { from_wire: 2, to_wire: 3, state: "pending", message: null };
  const derivable = {
    backups: [chain], printer_connected: true, printer_auto_refill: null, last_reported_auto_refill: true,
    eligibility_fresh: false, eligibility_age_seconds: 140, reported_backup_masks: [12],
  };

  it("takes the bridge's word over the app's derivation", () => {
    const stale = { ...chain, stale: true, confirmed_at: 1_792_800_000, last_confirmed_age_seconds: 300 };
    expect(backupLine(2, stale, derivable)).toBe("Backup: slot 4 · last confirmed 5 min ago");
    expect(backupLine(3, stale, derivable)).toBe("Backs up slot 3 · last confirmed 5 min ago");
    // The bridge saw contrary evidence the app cannot: it is a real wait, whatever the masks say.
    const cleared = { ...chain, stale: false, confirmed_at: null, last_confirmed_age_seconds: null };
    expect(backupLine(2, cleared, derivable)).toBe("Backup: slot 4 · waiting for the printer to confirm");
    // Restarted bridge: stale but no confirmation kept.
    expect(backupStatus({ ...chain, stale: true, last_confirmed_age_seconds: null }, derivable)).toBe("last confirmed a while ago");
    // Offline still wins.
    expect(backupStatus(stale, { ...derivable, printer_connected: false })).toBe("printer offline");
  });
});
