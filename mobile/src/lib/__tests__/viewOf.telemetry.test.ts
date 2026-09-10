/**
 * Unit tests for src/lib/snapshot.ts — viewOf() telemetry fields.
 *
 * The colocated snapshot.test.ts covers stage/hms/statusLine/jobContext/
 * jobAnomaly exhaustively. This file covers the REMAINING viewOf output
 * fields that have zero test coverage:
 *
 *   - phase mapping (PHASE_MAP, "completed" → "finished", unknown phases)
 *   - headline / subtitle / indicator
 *   - job block (progress, layer, totalLayers, subtaskName, timeLeftMin)
 *   - ETA (etaTimeStr) — printing + paused phases only; absent/null cases
 *   - temps (nozzleActual/Target, bedActual/Target, chamberTemp)
 *   - cooling fans (fanPart, fanAux, fanChamber)
 *   - lights (lightOn)
 *   - speedLevel (print_params.speed_mm_s)
 *   - AMS (amsPresent, ams slot array, slotMemory normalisation)
 *   - null-snapshot → all defaults
 *
 * snapshot.ts has no native-module imports — no mocks needed.
 * Style mirrors the existing colocated snapshot.test.ts:
 *   - cast partial objects to `as any` for the fixture parameter
 *   - assert the exact field(s) changed by each fixture
 *   - describe blocks named after the View section under test
 */

import { viewOf } from "../snapshot";

// ---------------------------------------------------------------------------
// Helpers — build the minimal realistic fixtures without repeating boilerplate
// ---------------------------------------------------------------------------

/** Wrap fields at the snapshot root exactly as the bridge sends them. */
function snap(fields: Record<string, unknown>) {
  return fields as any;
}

// ---------------------------------------------------------------------------
// 1. null snapshot → all defaults
// ---------------------------------------------------------------------------

describe("viewOf(null) — complete default output", () => {
  it("phase defaults to unknown", () => {
    expect(viewOf(null).phase).toBe("unknown");
  });

  it("headline defaults to em-dash", () => {
    expect(viewOf(null).headline).toBe("—");
  });

  it("subtitle defaults to null", () => {
    expect(viewOf(null).subtitle).toBeNull();
  });

  it("indicator defaults to indeterminate", () => {
    expect(viewOf(null).indicator).toBe("indeterminate");
  });

  it("progress/layer/totalLayers/timeLeftMin default to null", () => {
    const v = viewOf(null);
    expect(v.progress).toBeNull();
    expect(v.layer).toBeNull();
    expect(v.totalLayers).toBeNull();
    expect(v.timeLeftMin).toBeNull();
  });

  it("etaTimeStr defaults to null", () => {
    expect(viewOf(null).etaTimeStr).toBeNull();
  });

  it("all temp fields default to null", () => {
    const v = viewOf(null);
    expect(v.nozzleActual).toBeNull();
    expect(v.nozzleTarget).toBeNull();
    expect(v.bedActual).toBeNull();
    expect(v.bedTarget).toBeNull();
    expect(v.chamberTemp).toBeNull();
  });

  it("all fan fields default to null", () => {
    const v = viewOf(null);
    expect(v.fanPart).toBeNull();
    expect(v.fanAux).toBeNull();
    expect(v.fanChamber).toBeNull();
  });

  it("lightOn defaults to null", () => {
    expect(viewOf(null).lightOn).toBeNull();
  });

  it("speedLevel defaults to null", () => {
    expect(viewOf(null).speedLevel).toBeNull();
  });

  it("amsPresent defaults to false", () => {
    expect(viewOf(null).amsPresent).toBe(false);
  });

  it("ams defaults to empty array", () => {
    expect(viewOf(null).ams).toEqual([]);
  });

  it("printError + printErrorFull default to null", () => {
    expect(viewOf(null).printError).toBeNull();
    expect(viewOf(null).printErrorFull).toBeNull();
  });

  it("subtaskName defaults to null", () => {
    expect(viewOf(null).subtaskName).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Phase mapping
// ---------------------------------------------------------------------------

describe("viewOf — phase mapping", () => {
  it("phase='idle' → 'idle'", () => {
    expect(viewOf(snap({ phase: "idle" })).phase).toBe("idle");
  });

  it("phase='preparing' → 'preparing'", () => {
    expect(viewOf(snap({ phase: "preparing" })).phase).toBe("preparing");
  });

  it("phase='printing' → 'printing'", () => {
    expect(viewOf(snap({ phase: "printing" })).phase).toBe("printing");
  });

  it("phase='paused' → 'paused'", () => {
    expect(viewOf(snap({ phase: "paused" })).phase).toBe("paused");
  });

  // Bridge sends "completed"; the app historically uses "finished".
  it("phase='completed' → 'finished' (remap)", () => {
    expect(viewOf(snap({ phase: "completed" })).phase).toBe("finished");
  });

  it("phase='finished' → 'finished' (pass-through)", () => {
    expect(viewOf(snap({ phase: "finished" })).phase).toBe("finished");
  });

  it("phase='failed' → 'failed'", () => {
    expect(viewOf(snap({ phase: "failed" })).phase).toBe("failed");
  });

  it("phase='unknown' → 'unknown'", () => {
    expect(viewOf(snap({ phase: "unknown" })).phase).toBe("unknown");
  });

  // Unrecognised phase must not crash and must default to 'unknown'.
  it("phase with unknown string → 'unknown' (default, never throws)", () => {
    expect(() => viewOf(snap({ phase: "bogus_future_phase" }))).not.toThrow();
    expect(viewOf(snap({ phase: "bogus_future_phase" })).phase).toBe("unknown");
  });

  it("phase absent → 'unknown'", () => {
    expect(viewOf(snap({ headline: { title: "—" } })).phase).toBe("unknown");
  });

  it("phase = null → 'unknown'", () => {
    expect(viewOf(snap({ phase: null })).phase).toBe("unknown");
  });

  it("phase = 42 (number) → 'unknown'", () => {
    expect(viewOf(snap({ phase: 42 })).phase).toBe("unknown");
  });
});

// ---------------------------------------------------------------------------
// 3. Headline block
// ---------------------------------------------------------------------------

describe("viewOf — headline block", () => {
  it("headline.title → v.headline", () => {
    const v = viewOf(snap({ headline: { title: "Printing cube.3mf" } }));
    expect(v.headline).toBe("Printing cube.3mf");
  });

  it("headline.subtitle string → v.subtitle", () => {
    const v = viewOf(snap({ headline: { title: "Printing", subtitle: "Layer 10 of 100" } }));
    expect(v.subtitle).toBe("Layer 10 of 100");
  });

  it("headline.subtitle absent → v.subtitle null", () => {
    const v = viewOf(snap({ headline: { title: "Idle" } }));
    expect(v.subtitle).toBeNull();
  });

  it("headline.subtitle non-string → v.subtitle null", () => {
    const v = viewOf(snap({ headline: { title: "Idle", subtitle: 42 } }));
    expect(v.subtitle).toBeNull();
  });

  it("headline.indicator → v.indicator", () => {
    const v = viewOf(snap({ headline: { title: "Printing", indicator: "progress" } }));
    expect(v.indicator).toBe("progress");
  });

  it("headline absent → headline='—', subtitle=null, indicator='indeterminate'", () => {
    const v = viewOf(snap({ phase: "idle" }));
    expect(v.headline).toBe("—");
    expect(v.subtitle).toBeNull();
    expect(v.indicator).toBe("indeterminate");
  });

  it("headline is not an object (malformed) → defaults, never throws", () => {
    expect(() => viewOf(snap({ headline: "just a string" }))).not.toThrow();
    const v = viewOf(snap({ headline: "just a string" }));
    expect(v.headline).toBe("—");
  });

  it("headline.title non-string → headline stays default", () => {
    const v = viewOf(snap({ headline: { title: 99 } }));
    expect(v.headline).toBe("—");
  });
});

// ---------------------------------------------------------------------------
// 4. Job block (progress, layers, subtaskName, timeLeftMin)
// ---------------------------------------------------------------------------

describe("viewOf — job block", () => {
  it("job.percent → v.progress", () => {
    const v = viewOf(snap({ job: { percent: 42 } }));
    expect(v.progress).toBe(42);
  });

  it("job.percent = 0 → v.progress = 0 (zero is valid, not null)", () => {
    const v = viewOf(snap({ job: { percent: 0 } }));
    expect(v.progress).toBe(0);
  });

  it("job.percent null → v.progress null", () => {
    const v = viewOf(snap({ job: { percent: null } }));
    expect(v.progress).toBeNull();
  });

  it("job.layer_num → v.layer", () => {
    const v = viewOf(snap({ job: { layer_num: 37 } }));
    expect(v.layer).toBe(37);
  });

  it("job.total_layer_num → v.totalLayers", () => {
    const v = viewOf(snap({ job: { total_layer_num: 200 } }));
    expect(v.totalLayers).toBe(200);
  });

  it("job.subtask_name → v.subtaskName", () => {
    const v = viewOf(snap({ job: { subtask_name: "benchy.3mf" } }));
    expect(v.subtaskName).toBe("benchy.3mf");
  });

  it("job.subtask_name absent → v.subtaskName null", () => {
    const v = viewOf(snap({ job: {} }));
    expect(v.subtaskName).toBeNull();
  });

  it("job.remaining_min → v.timeLeftMin", () => {
    const v = viewOf(snap({ job: { remaining_min: 73 } }));
    expect(v.timeLeftMin).toBe(73);
  });

  it("job.remaining_min = 0 → v.timeLeftMin = 0 (printing almost done)", () => {
    const v = viewOf(snap({ phase: "printing", job: { remaining_min: 0 } }));
    expect(v.timeLeftMin).toBe(0);
  });

  it("job absent → progress/layer/totalLayers/subtaskName/timeLeftMin all null", () => {
    const v = viewOf(snap({ phase: "idle" }));
    expect(v.progress).toBeNull();
    expect(v.layer).toBeNull();
    expect(v.totalLayers).toBeNull();
    expect(v.subtaskName).toBeNull();
    expect(v.timeLeftMin).toBeNull();
  });

  it("job is not an object → defaults, never throws", () => {
    expect(() => viewOf(snap({ job: "bad" }))).not.toThrow();
    const v = viewOf(snap({ job: "bad" }));
    expect(v.progress).toBeNull();
  });

  // Realistic printing snapshot.
  it("realistic printing snapshot → all job fields populated", () => {
    const v = viewOf(snap({
      phase: "printing",
      job: {
        subtask_name: "benchy.3mf",
        layer_num: 45,
        total_layer_num: 180,
        percent: 25,
        remaining_min: 94,
      },
      headline: { title: "Printing benchy.3mf", subtitle: "Layer 45 of 180", indicator: "progress" },
    }));
    expect(v.phase).toBe("printing");
    expect(v.subtaskName).toBe("benchy.3mf");
    expect(v.layer).toBe(45);
    expect(v.totalLayers).toBe(180);
    expect(v.progress).toBe(25);
    expect(v.timeLeftMin).toBe(94);
  });
});

// ---------------------------------------------------------------------------
// 5. ETA (etaTimeStr)
// ---------------------------------------------------------------------------

describe("viewOf — etaTimeStr", () => {
  // ETA is only computed for printing and paused phases when remaining_min is
  // a finite number. It must NEVER be "Invalid Date", "NaN", or a blank string.
  // We don't assert the exact formatted time — device locale is variable and the
  // test runs in Node where toLocaleTimeString output is platform-defined.
  // Instead we assert: non-null ↔ looks like a valid non-empty string.

  it("printing + remaining_min present → etaTimeStr non-null and non-empty", () => {
    const v = viewOf(snap({ phase: "printing", job: { remaining_min: 60 } }));
    expect(v.etaTimeStr).not.toBeNull();
    expect(typeof v.etaTimeStr).toBe("string");
    expect((v.etaTimeStr as string).length).toBeGreaterThan(0);
  });

  it("paused + remaining_min present → etaTimeStr non-null (paused still shows ETA)", () => {
    const v = viewOf(snap({ phase: "paused", job: { remaining_min: 30 } }));
    expect(v.etaTimeStr).not.toBeNull();
  });

  // The bridge nulls remaining_min outside printing/paused phases per §6.1.
  // Even if we get a stale value, only printing/paused phases should show ETA.
  it("idle + remaining_min → etaTimeStr null (idle has no ETA)", () => {
    const v = viewOf(snap({ phase: "idle", job: { remaining_min: 30 } }));
    expect(v.etaTimeStr).toBeNull();
  });

  it("failed + remaining_min → etaTimeStr null", () => {
    const v = viewOf(snap({ phase: "failed", job: { remaining_min: 10 } }));
    expect(v.etaTimeStr).toBeNull();
  });

  it("finished + remaining_min → etaTimeStr null", () => {
    const v = viewOf(snap({ phase: "completed", job: { remaining_min: 0 } }));
    expect(v.etaTimeStr).toBeNull();
  });

  it("printing + remaining_min null → etaTimeStr null (no fake ETA)", () => {
    const v = viewOf(snap({ phase: "printing", job: { remaining_min: null } }));
    expect(v.etaTimeStr).toBeNull();
  });

  it("printing + remaining_min absent → etaTimeStr null", () => {
    const v = viewOf(snap({ phase: "printing", job: {} }));
    expect(v.etaTimeStr).toBeNull();
  });

  it("printing + remaining_min = 0 → etaTimeStr non-null (0 min = almost done, not absent)", () => {
    const v = viewOf(snap({ phase: "printing", job: { remaining_min: 0 } }));
    // 0 is a finite number — ETA is now, should still produce a string.
    expect(v.etaTimeStr).not.toBeNull();
  });

  // "Never Invalid Date" contract: etaTimeStr must not be any invalid sentinel.
  it("printing + remaining_min = 1 → etaTimeStr is not 'Invalid Date' or 'NaN'", () => {
    const v = viewOf(snap({ phase: "printing", job: { remaining_min: 1 } }));
    expect(v.etaTimeStr).not.toMatch(/Invalid Date/i);
    expect(v.etaTimeStr).not.toMatch(/NaN/i);
  });

  // Preparing phase: no ETA (job hasn't started slicing).
  it("preparing + remaining_min → etaTimeStr null", () => {
    const v = viewOf(snap({ phase: "preparing", job: { remaining_min: 5 } }));
    expect(v.etaTimeStr).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 6. Temperature fields
// ---------------------------------------------------------------------------

describe("viewOf — temperature fields", () => {
  it("nozzle.current_c → nozzleActual", () => {
    const v = viewOf(snap({ temps: { nozzle: { current_c: 254.5, target_c: 260 } } }));
    expect(v.nozzleActual).toBe(254.5);
  });

  it("nozzle.target_c → nozzleTarget", () => {
    const v = viewOf(snap({ temps: { nozzle: { current_c: 254.5, target_c: 260 } } }));
    expect(v.nozzleTarget).toBe(260);
  });

  it("bed.current_c → bedActual", () => {
    const v = viewOf(snap({ temps: { bed: { current_c: 65.0, target_c: 70 } } }));
    expect(v.bedActual).toBe(65.0);
  });

  it("bed.target_c → bedTarget", () => {
    const v = viewOf(snap({ temps: { bed: { current_c: 65.0, target_c: 70 } } }));
    expect(v.bedTarget).toBe(70);
  });

  it("chamber.current_c → chamberTemp", () => {
    const v = viewOf(snap({ temps: { chamber: { current_c: 38 } } }));
    expect(v.chamberTemp).toBe(38);
  });

  // Null temps are common in preparing phase before heaters activate.
  it("nozzle.current_c = null → nozzleActual null (preparing/null-target)", () => {
    const v = viewOf(snap({ temps: { nozzle: { current_c: null, target_c: 0 } } }));
    expect(v.nozzleActual).toBeNull();
  });

  it("temps block absent → all temp fields null, never throws", () => {
    expect(() => viewOf(snap({ phase: "idle" }))).not.toThrow();
    const v = viewOf(snap({ phase: "idle" }));
    expect(v.nozzleActual).toBeNull();
    expect(v.nozzleTarget).toBeNull();
    expect(v.bedActual).toBeNull();
    expect(v.bedTarget).toBeNull();
    expect(v.chamberTemp).toBeNull();
  });

  it("temps is not an object (malformed) → all null, never throws", () => {
    expect(() => viewOf(snap({ temps: "bad" }))).not.toThrow();
    const v = viewOf(snap({ temps: "bad" }));
    expect(v.nozzleActual).toBeNull();
  });

  it("nozzle absent but bed present → nozzleActual null, bedActual set", () => {
    const v = viewOf(snap({ temps: { bed: { current_c: 60 } } }));
    expect(v.nozzleActual).toBeNull();
    expect(v.bedActual).toBe(60);
  });

  // Realistic mid-print temps snapshot.
  it("realistic temps → all five fields populated", () => {
    const v = viewOf(snap({
      temps: {
        nozzle: { current_c: 256, target_c: 260 },
        bed: { current_c: 63, target_c: 65 },
        chamber: { current_c: 41 },
      },
    }));
    expect(v.nozzleActual).toBe(256);
    expect(v.nozzleTarget).toBe(260);
    expect(v.bedActual).toBe(63);
    expect(v.bedTarget).toBe(65);
    expect(v.chamberTemp).toBe(41);
  });

  // String-encoded numbers from older bridge versions must be coerced via num().
  it("nozzle.current_c as numeric string → coerced to number", () => {
    const v = viewOf(snap({ temps: { nozzle: { current_c: "254.5", target_c: "260" } } }));
    expect(v.nozzleActual).toBe(254.5);
    expect(v.nozzleTarget).toBe(260);
  });
});

// ---------------------------------------------------------------------------
// 7. Cooling fans
// ---------------------------------------------------------------------------

describe("viewOf — fan fields", () => {
  // The bridge already converts P1S 0-15 range to 0-100 percent; the app
  // trusts that conversion and stores/displays the percent directly.
  it("cooling.part_fan.percent → fanPart", () => {
    const v = viewOf(snap({ cooling: { part_fan: { percent: 100 } } }));
    expect(v.fanPart).toBe(100);
  });

  it("cooling.aux_fan.percent → fanAux", () => {
    const v = viewOf(snap({ cooling: { aux_fan: { percent: 50 } } }));
    expect(v.fanAux).toBe(50);
  });

  it("cooling.chamber_fan.percent → fanChamber", () => {
    const v = viewOf(snap({ cooling: { chamber_fan: { percent: 0 } } }));
    // 0 is valid — fan off.
    expect(v.fanChamber).toBe(0);
  });

  it("cooling.part_fan.percent null → fanPart null", () => {
    const v = viewOf(snap({ cooling: { part_fan: { percent: null } } }));
    expect(v.fanPart).toBeNull();
  });

  it("cooling block absent → all fans null, never throws", () => {
    expect(() => viewOf(snap({ phase: "idle" }))).not.toThrow();
    const v = viewOf(snap({ phase: "idle" }));
    expect(v.fanPart).toBeNull();
    expect(v.fanAux).toBeNull();
    expect(v.fanChamber).toBeNull();
  });

  it("cooling.part_fan absent → fanPart null", () => {
    const v = viewOf(snap({ cooling: { aux_fan: { percent: 30 } } }));
    expect(v.fanPart).toBeNull();
    expect(v.fanAux).toBe(30);
  });

  it("all three fans set → three fan fields correctly populated", () => {
    const v = viewOf(snap({
      cooling: {
        part_fan: { percent: 100 },
        aux_fan: { percent: 60 },
        chamber_fan: { percent: 0 },
      },
    }));
    expect(v.fanPart).toBe(100);
    expect(v.fanAux).toBe(60);
    expect(v.fanChamber).toBe(0);
  });

  it("fan percent as numeric string → coerced via num()", () => {
    const v = viewOf(snap({ cooling: { part_fan: { percent: "75" } } }));
    expect(v.fanPart).toBe(75);
  });
});

// ---------------------------------------------------------------------------
// 8. Lights
// ---------------------------------------------------------------------------

describe("viewOf — lightOn", () => {
  it("lights.chamber_on = true → lightOn true", () => {
    expect(viewOf(snap({ lights: { chamber_on: true } })).lightOn).toBe(true);
  });

  it("lights.chamber_on = false → lightOn false", () => {
    expect(viewOf(snap({ lights: { chamber_on: false } })).lightOn).toBe(false);
  });

  it("lights.chamber_on absent → lightOn null", () => {
    expect(viewOf(snap({ lights: {} })).lightOn).toBeNull();
  });

  it("lights block absent → lightOn null", () => {
    expect(viewOf(snap({ phase: "idle" })).lightOn).toBeNull();
  });

  it("lights.chamber_on non-boolean (e.g. 1) → lightOn null (strict boolean check)", () => {
    // The source reads: `typeof lights.chamber_on === "boolean" ? ... : null`
    expect(viewOf(snap({ lights: { chamber_on: 1 } })).lightOn).toBeNull();
  });

  it("lights is not an object → lightOn null, never throws", () => {
    expect(() => viewOf(snap({ lights: "on" }))).not.toThrow();
    expect(viewOf(snap({ lights: "on" })).lightOn).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 9. Speed level (print_params)
// ---------------------------------------------------------------------------

describe("viewOf — speedLevel", () => {
  it("print_params.speed_mm_s → speedLevel", () => {
    expect(viewOf(snap({ print_params: { speed_mm_s: 150 } })).speedLevel).toBe(150);
  });

  it("print_params.speed_mm_s = 0 → speedLevel 0", () => {
    expect(viewOf(snap({ print_params: { speed_mm_s: 0 } })).speedLevel).toBe(0);
  });

  it("print_params absent → speedLevel null", () => {
    expect(viewOf(snap({ phase: "idle" })).speedLevel).toBeNull();
  });

  it("print_params.speed_mm_s null → speedLevel null", () => {
    expect(viewOf(snap({ print_params: { speed_mm_s: null } })).speedLevel).toBeNull();
  });

  it("print_params not an object → speedLevel null, never throws", () => {
    expect(() => viewOf(snap({ print_params: "fast" }))).not.toThrow();
    expect(viewOf(snap({ print_params: "fast" })).speedLevel).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 10. AMS — amsPresent flag
// ---------------------------------------------------------------------------

describe("viewOf — amsPresent", () => {
  it("ams.present = true → amsPresent true", () => {
    expect(viewOf(snap({ ams: { present: true, slots: [] } })).amsPresent).toBe(true);
  });

  it("ams.present = false → amsPresent false", () => {
    expect(viewOf(snap({ ams: { present: false, slots: [] } })).amsPresent).toBe(false);
  });

  it("ams.present absent → amsPresent false (safe default)", () => {
    expect(viewOf(snap({ ams: { slots: [] } })).amsPresent).toBe(false);
  });

  it("ams block absent → amsPresent false", () => {
    expect(viewOf(snap({ phase: "idle" })).amsPresent).toBe(false);
  });

  it("ams.present non-boolean truthy string → amsPresent false (strict === true)", () => {
    // The source reads: `ams?.present === true` — strict equality.
    expect(viewOf(snap({ ams: { present: "yes", slots: [] } })).amsPresent).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 11. AMS slots array
// ---------------------------------------------------------------------------

describe("viewOf — ams slots", () => {
  const LOADED_SLOT = {
    physical_slot: 1,
    type: "PLA",
    color: "FF0000",
    state: "loaded",
    remaining_pct: 80,
    memory: null,
  };

  const EMPTY_SLOT = {
    physical_slot: 2,
    type: null,
    color: null,
    state: "empty",
    remaining_pct: null,
    memory: null,
  };

  it("ams.slots absent → ams = []", () => {
    expect(viewOf(snap({ ams: { present: true } })).ams).toEqual([]);
  });

  it("ams.slots = [] → ams = [] (RFID re-scan in progress)", () => {
    // amsPresent=true + ams=[] = re-scan; must NOT show "no AMS" — the screen
    // is expected to hold the previous slot view.
    const v = viewOf(snap({ ams: { present: true, slots: [] } }));
    expect(v.amsPresent).toBe(true);
    expect(v.ams).toHaveLength(0);
  });

  it("loaded slot → name=type, color, remainingPercent, empty=false", () => {
    const v = viewOf(snap({ ams: { present: true, slots: [LOADED_SLOT] } }));
    expect(v.ams).toHaveLength(1);
    const s = v.ams[0];
    expect(s.physicalSlot).toBe(1);
    expect(s.empty).toBe(false);
    expect(s.name).toBe("PLA");
    expect(s.color).toBe("FF0000");
    expect(s.remainingPercent).toBe(80);
  });

  it("empty slot (state='empty') → empty=true", () => {
    const v = viewOf(snap({ ams: { present: true, slots: [EMPTY_SLOT] } }));
    const s = v.ams[0];
    expect(s.empty).toBe(true);
    expect(s.name).toBeNull();
    expect(s.color).toBeNull();
  });

  it("slot with null type → empty=true (tagless tray is considered empty)", () => {
    const slot = { physical_slot: 3, type: null, color: "00FF00", state: "loaded", remaining_pct: 50, memory: null };
    const v = viewOf(snap({ ams: { present: true, slots: [slot] } }));
    // type == null → empty per viewOf logic
    expect(v.ams[0].empty).toBe(true);
  });

  it("multiple slots → correct length and order", () => {
    const v = viewOf(snap({ ams: { present: true, slots: [LOADED_SLOT, EMPTY_SLOT] } }));
    expect(v.ams).toHaveLength(2);
    expect(v.ams[0].physicalSlot).toBe(1);
    expect(v.ams[1].physicalSlot).toBe(2);
  });

  it("slot.physical_slot absent → index+1 fallback", () => {
    const slotNoPos = { type: "PETG", color: "0000FF", state: "loaded", remaining_pct: 70, memory: null };
    const v = viewOf(snap({ ams: { present: true, slots: [slotNoPos] } }));
    expect(v.ams[0].physicalSlot).toBe(1); // index 0 → 1
  });

  it("ams.slots not an array → ams = [], never throws", () => {
    expect(() => viewOf(snap({ ams: { present: true, slots: "bad" } }))).not.toThrow();
    expect(viewOf(snap({ ams: { present: true, slots: "bad" } })).ams).toEqual([]);
  });

  it("slot with non-object entry in slots array → skip gracefully", () => {
    // null entry in the array — the code does `obj(row) ?? {}` so it shouldn't crash.
    const v = viewOf(snap({ ams: { present: true, slots: [null, LOADED_SLOT] } }));
    // null coerces to empty obj {}; empty → empty slot. Loaded one still appears.
    expect(v.ams).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// 12. AMS slot memory (slotMemory normalisation)
// ---------------------------------------------------------------------------

describe("viewOf — ams slot memory", () => {
  const BASE_SLOT = {
    physical_slot: 1,
    type: "PLA",
    color: "FF0000",
    state: "loaded",
    remaining_pct: 80,
  };

  it("memory with all fields → SlotMemory with trimmed strings", () => {
    const v = viewOf(snap({
      ams: {
        present: true,
        slots: [{ ...BASE_SLOT, memory: { make: "  Polymaker  ", model: "PolyLite ASA  ", profile: " Generic PLA" } }],
      },
    }));
    const m = v.ams[0].memory;
    expect(m).not.toBeNull();
    expect(m?.make).toBe("Polymaker");
    expect(m?.model).toBe("PolyLite ASA");
    expect(m?.profile).toBe("Generic PLA");
  });

  it("memory = null → memory null", () => {
    const v = viewOf(snap({ ams: { present: true, slots: [{ ...BASE_SLOT, memory: null }] } }));
    expect(v.ams[0].memory).toBeNull();
  });

  it("memory absent → memory null", () => {
    const v = viewOf(snap({ ams: { present: true, slots: [BASE_SLOT] } }));
    expect(v.ams[0].memory).toBeNull();
  });

  it("memory with all-empty strings → memory null (all-blank collapses to null)", () => {
    // slotMemory() guards: `if (!make && !model && !profile) return null`
    const v = viewOf(snap({
      ams: { present: true, slots: [{ ...BASE_SLOT, memory: { make: "", model: "", profile: "" } }] },
    }));
    expect(v.ams[0].memory).toBeNull();
  });

  it("memory with only make set → memory non-null", () => {
    const v = viewOf(snap({
      ams: { present: true, slots: [{ ...BASE_SLOT, memory: { make: "Bambu", model: "", profile: "" } }] },
    }));
    expect(v.ams[0].memory).not.toBeNull();
    expect(v.ams[0].memory?.make).toBe("Bambu");
    expect(v.ams[0].memory?.model).toBe("");
    expect(v.ams[0].memory?.profile).toBe("");
  });

  it("memory not an object (malformed) → memory null, never throws", () => {
    expect(() => viewOf(snap({
      ams: { present: true, slots: [{ ...BASE_SLOT, memory: "Bambu PLA" }] },
    }))).not.toThrow();
    expect(viewOf(snap({
      ams: { present: true, slots: [{ ...BASE_SLOT, memory: "Bambu PLA" }] },
    })).ams[0].memory).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 13. printError / printErrorFull
// ---------------------------------------------------------------------------

describe("viewOf — printError", () => {
  it("print_error with text → printError string", () => {
    const v = viewOf(snap({
      print_error: { code: "abc", hex: "1234", text: "Nozzle jam", category: "hardware", severity: "error", _raw: {} },
    }));
    expect(v.printError).toBe("Nozzle jam");
  });

  it("print_error absent → printError null", () => {
    expect(viewOf(snap({ phase: "idle" })).printError).toBeNull();
  });

  it("print_error null → printError null", () => {
    expect(viewOf(snap({ print_error: null })).printError).toBeNull();
  });

  it("print_error present → printErrorFull carries the raw object", () => {
    const printErr = { code: "abc", hex: "1234", text: "Nozzle jam", category: "hardware", severity: "error", _raw: {} };
    const v = viewOf(snap({ print_error: printErr }));
    expect(v.printErrorFull).not.toBeNull();
    expect(v.printErrorFull?.text).toBe("Nozzle jam");
  });

  it("print_error with wiki_url → printErrorFull.wiki_url present", () => {
    const printErr = {
      code: "abc", hex: "1234", text: "Error", category: "hardware", severity: "error",
      wiki_url: "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/1234",
      _raw: {},
    };
    const v = viewOf(snap({ print_error: printErr }));
    expect(v.printErrorFull?.wiki_url).toBe(
      "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/1234",
    );
  });

  it("print_error is not an object → printError null, never throws", () => {
    expect(() => viewOf(snap({ print_error: "error text" }))).not.toThrow();
    expect(viewOf(snap({ print_error: "error text" })).printError).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 14. Full realistic snapshot — end-to-end integration
// ---------------------------------------------------------------------------

describe("viewOf — full realistic printing snapshot", () => {
  const FULL_PRINTING_SNAP = snap({
    phase: "printing",
    headline: { title: "Printing benchy.3mf", subtitle: "Layer 72 of 180", indicator: "progress" },
    job: {
      subtask_name: "benchy.3mf",
      layer_num: 72,
      total_layer_num: 180,
      percent: 40,
      remaining_min: 54,
    },
    temps: {
      nozzle: { current_c: 256, target_c: 260 },
      bed: { current_c: 63, target_c: 65 },
      chamber: { current_c: 39 },
    },
    cooling: {
      part_fan: { percent: 100 },
      aux_fan: { percent: 0 },
      chamber_fan: { percent: 20 },
    },
    lights: { chamber_on: true },
    print_params: { speed_mm_s: 200 },
    ams: {
      present: true,
      slots: [
        { physical_slot: 1, type: "PLA", color: "FF0000", state: "loaded", remaining_pct: 70, memory: null },
        { physical_slot: 2, type: null, color: null, state: "empty", remaining_pct: null, memory: null },
        { physical_slot: 3, type: "PETG", color: "0000FF", state: "loaded", remaining_pct: 45,
          memory: { make: "Polymaker", model: "PolyLite PETG", profile: "Generic PETG" } },
        { physical_slot: 4, type: null, color: null, state: "empty", remaining_pct: null, memory: null },
      ],
    },
    stage: { id: 14, text: "Auto bed leveling" },
    hms: [],
    print_error: null,
  });

  it("phase → 'printing'", () => {
    expect(viewOf(FULL_PRINTING_SNAP).phase).toBe("printing");
  });

  it("headline populated", () => {
    expect(viewOf(FULL_PRINTING_SNAP).headline).toBe("Printing benchy.3mf");
    expect(viewOf(FULL_PRINTING_SNAP).subtitle).toBe("Layer 72 of 180");
    expect(viewOf(FULL_PRINTING_SNAP).indicator).toBe("progress");
  });

  it("job fields populated", () => {
    const v = viewOf(FULL_PRINTING_SNAP);
    expect(v.subtaskName).toBe("benchy.3mf");
    expect(v.layer).toBe(72);
    expect(v.totalLayers).toBe(180);
    expect(v.progress).toBe(40);
    expect(v.timeLeftMin).toBe(54);
  });

  it("temps populated", () => {
    const v = viewOf(FULL_PRINTING_SNAP);
    expect(v.nozzleActual).toBe(256);
    expect(v.nozzleTarget).toBe(260);
    expect(v.bedActual).toBe(63);
    expect(v.bedTarget).toBe(65);
    expect(v.chamberTemp).toBe(39);
  });

  it("fans populated", () => {
    const v = viewOf(FULL_PRINTING_SNAP);
    expect(v.fanPart).toBe(100);
    expect(v.fanAux).toBe(0);
    expect(v.fanChamber).toBe(20);
  });

  it("lights on", () => {
    expect(viewOf(FULL_PRINTING_SNAP).lightOn).toBe(true);
  });

  it("speedLevel populated", () => {
    expect(viewOf(FULL_PRINTING_SNAP).speedLevel).toBe(200);
  });

  it("AMS present with 4 slots", () => {
    const v = viewOf(FULL_PRINTING_SNAP);
    expect(v.amsPresent).toBe(true);
    expect(v.ams).toHaveLength(4);
  });

  it("AMS slot 1 loaded with PLA", () => {
    const s = viewOf(FULL_PRINTING_SNAP).ams[0];
    expect(s.empty).toBe(false);
    expect(s.name).toBe("PLA");
    expect(s.color).toBe("FF0000");
    expect(s.remainingPercent).toBe(70);
    expect(s.memory).toBeNull();
  });

  it("AMS slot 3 loaded with PETG and memory", () => {
    const s = viewOf(FULL_PRINTING_SNAP).ams[2];
    expect(s.empty).toBe(false);
    expect(s.name).toBe("PETG");
    expect(s.memory?.make).toBe("Polymaker");
    expect(s.memory?.model).toBe("PolyLite PETG");
  });

  it("statusLine = stage.text ('Auto bed leveling') during printing", () => {
    expect(viewOf(FULL_PRINTING_SNAP).statusLine).toBe("Auto bed leveling");
  });

  it("etaTimeStr is non-null and non-empty string", () => {
    const v = viewOf(FULL_PRINTING_SNAP);
    expect(v.etaTimeStr).not.toBeNull();
    expect(typeof v.etaTimeStr).toBe("string");
    expect((v.etaTimeStr as string).length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// 15. Preparing phase — the "no fake progress" contract
// ---------------------------------------------------------------------------

describe("viewOf — preparing phase (no fake data)", () => {
  // When phase is 'preparing', progress/layer/ETA are null — the bridge
  // nulls these per §6.1. The UI must show nothing, not invented zeros.
  it("preparing → progress null (bridge nulls it outside printing)", () => {
    const v = viewOf(snap({
      phase: "preparing",
      headline: { title: "Preparing", subtitle: "Slicing…", indicator: "indeterminate" },
      job: { percent: null, layer_num: null, total_layer_num: null, remaining_min: null },
    }));
    expect(v.progress).toBeNull();
    expect(v.layer).toBeNull();
    expect(v.totalLayers).toBeNull();
    expect(v.timeLeftMin).toBeNull();
    expect(v.etaTimeStr).toBeNull();
  });

  it("preparing → statusLine = headline title", () => {
    const v = viewOf(snap({
      phase: "preparing",
      headline: { title: "Preparing", subtitle: "", indicator: "indeterminate" },
    }));
    expect(v.statusLine).toBe("Preparing");
  });
});
