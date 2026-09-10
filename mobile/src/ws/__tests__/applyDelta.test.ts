/**
 * Unit tests for applyDelta — the deep-merge function that mirrors the
 * bridge's `_deep_merge` behaviour (src/ws/live.ts).
 *
 * Contract:
 *   - Nested plain objects merge recursively.
 *   - Arrays replace (not concat) — matching the bridge spec.
 *   - Scalars (strings, numbers, booleans, null) replace.
 *   - Keys present only in base are preserved.
 *   - Keys present only in delta are added.
 *   - Empty delta leaves base unchanged.
 *   - Empty base with non-empty delta produces a shallow copy of delta.
 *   - Delta key set to null replaces (not removes) the key.
 *   - Mutation: applyDelta must not mutate the input objects.
 */

import { applyDelta } from "../live";

// live.ts transitively imports the MMKV native module (via store/bridge →
// lib/kv); mock the kv module so this pure-logic suite runs in plain Node.
// babel-jest hoists jest.mock calls above the imports at compile time.
jest.mock("../../lib/kv", () => ({
  kv: {
    getString: () => undefined,
    getBoolean: () => undefined,
    set: jest.fn(),
    delete: jest.fn(),
  },
}));

// ---------------------------------------------------------------------------
// Basic merging
// ---------------------------------------------------------------------------

describe("applyDelta", () => {
  it("empty delta → base unchanged (new object)", () => {
    const base = { a: 1, b: "x" };
    const result = applyDelta(base, {});
    expect(result).toEqual({ a: 1, b: "x" });
    expect(result).not.toBe(base); // must be a new object
  });

  it("empty base + non-empty delta → copy of delta", () => {
    const result = applyDelta({}, { x: 42 });
    expect(result).toEqual({ x: 42 });
  });

  it("scalar replace: number", () => {
    const result = applyDelta({ n: 1 }, { n: 99 });
    expect(result.n).toBe(99);
  });

  it("scalar replace: string", () => {
    const result = applyDelta({ s: "old" }, { s: "new" });
    expect(result.s).toBe("new");
  });

  it("scalar replace: boolean", () => {
    const result = applyDelta({ v: true }, { v: false });
    expect(result.v).toBe(false);
  });

  it("scalar replace: null replaces non-null", () => {
    const result = applyDelta({ v: 42 }, { v: null });
    expect(result.v).toBeNull();
  });

  it("keys only in base are preserved", () => {
    const result = applyDelta({ keep: "yes", change: 1 }, { change: 2 });
    expect(result.keep).toBe("yes");
    expect(result.change).toBe(2);
  });

  it("keys only in delta are added", () => {
    const result = applyDelta({ a: 1 }, { b: 2 });
    expect(result).toEqual({ a: 1, b: 2 });
  });

  // ---------------------------------------------------------------------------
  // Nested object merge
  // ---------------------------------------------------------------------------

  it("nested object merges recursively", () => {
    const base = { temps: { nozzle: { current_c: 200, target_c: 220 }, bed: { current_c: 55 } } };
    const delta = { temps: { nozzle: { current_c: 215 } } };
    const result = applyDelta(base, delta);
    expect(result).toEqual({
      temps: {
        nozzle: { current_c: 215, target_c: 220 }, // current updated, target preserved
        bed: { current_c: 55 },                     // bed untouched
      },
    });
  });

  it("deeply nested merge — three levels", () => {
    const base = { a: { b: { c: 1, d: 2 } } };
    const delta = { a: { b: { c: 99 } } };
    const result = applyDelta(base, delta) as any;
    expect(result.a.b.c).toBe(99);
    expect(result.a.b.d).toBe(2);
  });

  it("delta replaces whole nested key with scalar (object → scalar)", () => {
    const base = { job: { name: "test", progress: 50 } };
    const delta = { job: "done" }; // replace the whole job sub-object
    const result = applyDelta(base, delta);
    expect(result.job).toBe("done");
  });

  it("delta replaces scalar with object (scalar → object)", () => {
    const base = { x: 1 };
    const delta = { x: { nested: true } };
    const result = applyDelta(base, delta) as any;
    expect(result.x.nested).toBe(true);
  });

  // ---------------------------------------------------------------------------
  // Array replacement (not concatenation)
  // ---------------------------------------------------------------------------

  it("array in delta replaces array in base", () => {
    const base = { slots: [1, 2, 3] };
    const delta = { slots: [4, 5] };
    const result = applyDelta(base, delta) as any;
    expect(result.slots).toEqual([4, 5]);
  });

  it("array in delta replaces scalar in base", () => {
    const base = { tags: "none" };
    const delta = { tags: ["a", "b"] };
    const result = applyDelta(base, delta) as any;
    expect(result.tags).toEqual(["a", "b"]);
  });

  it("arrays are NOT merged — the whole array is replaced", () => {
    // Bridge spec: arrays replace, not merge. Even if delta is shorter.
    const base = { items: [{ id: 1 }, { id: 2 }, { id: 3 }] };
    const delta = { items: [{ id: 99 }] };
    const result = applyDelta(base, delta) as any;
    expect(result.items).toHaveLength(1);
    expect(result.items[0].id).toBe(99);
  });

  it("array in base + object in delta → replace (object wins)", () => {
    const base = { v: [1, 2] };
    const delta = { v: { nested: true } };
    const result = applyDelta(base, delta) as any;
    expect(result.v.nested).toBe(true);
  });

  // ---------------------------------------------------------------------------
  // Immutability — applyDelta must not mutate its arguments
  // ---------------------------------------------------------------------------

  it("does not mutate base", () => {
    const base = { a: { n: 1 } };
    const frozen = JSON.parse(JSON.stringify(base)); // deep copy to compare
    applyDelta(base, { a: { n: 2 } });
    expect(base).toEqual(frozen);
  });

  it("does not mutate delta", () => {
    const delta = { a: { n: 99 } };
    const frozen = JSON.parse(JSON.stringify(delta));
    applyDelta({ a: { n: 1 } }, delta);
    expect(delta).toEqual(frozen);
  });

  // ---------------------------------------------------------------------------
  // Bridge snapshot shape — realistic representative case
  // ---------------------------------------------------------------------------

  it("realistic delta: nozzle temp + progress update", () => {
    const snapshot = {
      phase: "printing",
      job: { subtask_name: "cube.3mf", percent: 30, layer_num: 10, total_layer_num: 100 },
      temps: {
        nozzle: { current_c: 250, target_c: 260 },
        bed: { current_c: 60, target_c: 65 },
      },
    };
    const delta = {
      job: { percent: 45, layer_num: 15 },
      temps: { nozzle: { current_c: 258 } },
    };
    const result = applyDelta(snapshot, delta) as any;

    expect(result.phase).toBe("printing");
    expect(result.job.subtask_name).toBe("cube.3mf");
    expect(result.job.percent).toBe(45);
    expect(result.job.layer_num).toBe(15);
    expect(result.job.total_layer_num).toBe(100); // preserved
    expect(result.temps.nozzle.current_c).toBe(258);
    expect(result.temps.nozzle.target_c).toBe(260); // preserved
    expect(result.temps.bed.current_c).toBe(60);    // untouched
  });

  it("empty delta on real snapshot → snapshot unchanged", () => {
    const snapshot = { phase: "idle", job: null, temps: { nozzle: { current_c: 25 } } };
    const result = applyDelta(snapshot, {});
    expect(result).toEqual(snapshot);
  });
});
