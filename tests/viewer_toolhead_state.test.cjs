const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(
  "src/bambu_bridge/static/viewer.html", "utf8",
);
function chunk(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start);
  assert.ok(start >= 0 && end >= 0, `missing ${startText}`);
  return source.slice(start, end);
}
function declaration(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  let open = source.indexOf("{", start), depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}

let now = 1000;
const context = {
  performance: { now: () => now },
  Date, Math, Number, JSON,
  STATUS_TTL_MS: 12000, TELEMETRY_TTL_MS: 65000,
  document: { hidden: false, getElementById: () => ({ classList: { toggle() {} } }) },
  window: {},
  updateLivePill() {}, showError() {},
  setDisplayedLayer() {},
  sampleMotionPath: (path, fraction) => ({
    position: path ? [fraction, 0, 0] : null, reveal: fraction, travel: false,
  }),
  buildMotionPath: () => ({ total: 1 }),
  snapRevealToLayer: () => {},
};
vm.createContext(context);
vm.runInContext(`${chunk("const STATE =", "/* ── 6. Networking")}
  ${declaration("trackingFresh")}
  ${declaration("updatePacing")}
  ${declaration("loseTracking")}
  ${declaration("markJobChanged")}
  ${declaration("acceptGeometryJob")}
  ${declaration("sampleHead")}
  ${declaration("advanceReveal")}
  ${declaration("acceptSnapshot")}
  ${declaration("intOr")}
  this.STATE=STATE; this.trackingFresh=trackingFresh; this.updatePacing=updatePacing;
  this.advanceReveal=advanceReveal; this.acceptSnapshot=acceptSnapshot; this.acceptGeometryJob=acceptGeometryJob; this.sampleHead=sampleHead;`, context);

function freshSnapshot({ layer = 1, total = 10, remaining = 90, phase = "printing", name = "job", start = "s1", telemetryAt } = {}) {
  return { phase, session: { connected: true, last_telemetry_at: telemetryAt || new Date(Date.now()).toISOString() },
    job: { layer_num: layer, total_layer_num: total, remaining_min: remaining, subtask_name: name, started_at: start } };
}
function reset() {
  const s = context.STATE;
  s.mode = "toolpath"; s.mesh = { totalLayers: 10 }; s.jobKey = null; s.jobChanged = false;
  s.following = true; s.phase = "unknown"; s.liveLayer = 0; s.lastLayerNum = null;
  s.remainingMin = null; s.totalLayers = 10; s.measuredLayerSec = null; s.estLayerSec = 30;
  s.layerElapsed = 0; s.measurementValid = false; s.layerSynced = false; s.motionPath = { total: 1 };
  s.motionFraction = 0; s.motionPoint = null; s.tickAt = now; s.freshUntil = now + 12000;
}

// Bootstrap is a layer-start estimate and must remain stable while the same layer's ETA changes.
reset(); context.acceptSnapshot(freshSnapshot({ remaining: 90 }));
const boot = context.STATE.estLayerSec;
assert.equal(boot, 540);
now += 3000; context.acceptSnapshot(freshSnapshot({ remaining: 40 }));
assert.equal(context.STATE.estLayerSec, boot);

// Paused time does not accrue and a long pause does not catch up on resume.
reset(); context.acceptSnapshot(freshSnapshot({ remaining: 20 }));
context.STATE.measurementValid = true; context.STATE.layerSynced = true;
now += 1000; context.advanceReveal(now); const beforePause = context.STATE.layerElapsed;
context.acceptSnapshot(freshSnapshot({ phase: "paused", remaining: 20 }));
assert.equal(context.STATE.measurementValid, false);
assert.equal(context.STATE.layerSynced, false);
now += 120000; context.advanceReveal(now); assert.equal(context.STATE.layerElapsed, beforePause);
context.acceptSnapshot(freshSnapshot({ phase: "printing", remaining: 20 }));
now += 1000; context.advanceReveal(now); assert.equal(context.STATE.layerElapsed, beforePause + 1);

// Stale telemetry invalidates motion and a >2s suspended interval is discarded.
reset(); context.STATE.measurementValid = true; context.STATE.layerSynced = true;
context.STATE.layerElapsed = 4; context.STATE.motionFraction = 0.25;
context.STATE.freshUntil = now - 1; context.advanceReveal(now + 1);
assert.equal(context.STATE.measurementValid, false); assert.equal(context.STATE.layerSynced, false);
assert.equal(context.STATE.layerElapsed, 4); assert.equal(context.STATE.motionFraction, 0.25);
reset(); context.STATE.measurementValid = true; context.STATE.layerSynced = true;
context.STATE.layerElapsed = 4; context.STATE.motionFraction = 0.25;
context.advanceReveal(now + 3000);
assert.equal(context.STATE.measurementValid, false); assert.equal(context.STATE.layerSynced, false);
assert.equal(context.STATE.layerElapsed, 4); assert.equal(context.STATE.motionFraction, 0.25);

reset(); context.acceptSnapshot(freshSnapshot({ telemetryAt: new Date(Date.now() - 65001).toISOString() }));
assert.equal(context.STATE.freshUntil, 0);
context.STATE.layerElapsed = 4; context.STATE.motionFraction = 0.25; context.advanceReveal(now + 1);
assert.equal(context.STATE.layerElapsed, 4); assert.equal(context.STATE.motionFraction, 0.25);

// Consecutive one-layer transitions establish then EMA the measured clock.
reset(); context.acceptSnapshot(freshSnapshot({ layer: 1 }));
context.STATE.measurementValid = true;
context.STATE.layerElapsed = 10; context.STATE.tickAt = now;
context.acceptSnapshot(freshSnapshot({ layer: 2 }));
assert.equal(context.STATE.measuredLayerSec, 10);
context.STATE.layerElapsed = 20; context.STATE.tickAt = now;
context.acceptSnapshot(freshSnapshot({ layer: 3 }));
assert.equal(context.STATE.measuredLayerSec, 13);

// An empty motion path must leave the head absent rather than inventing an origin.
reset(); context.STATE.motionPath = null; context.sampleHead();
assert.equal(context.STATE.motionPoint, null);

// A changed job is rejected before replacing the loaded geometry.
reset(); context.acceptSnapshot(freshSnapshot({ name: "first", start: "s1" }));
context.acceptSnapshot(freshSnapshot({ name: "second", start: "s2" }));
assert.equal(context.STATE.jobChanged, true);
assert.equal(context.STATE.freshUntil, 0);

reset(); context.acceptSnapshot(freshSnapshot({ name: null, start: null }));
context.acceptSnapshot(freshSnapshot({ name: "later", start: "s2" }));
assert.equal(context.STATE.jobChanged, false);

// A layer crossed during lost telemetry cannot establish an observed boundary.
reset(); context.acceptSnapshot(freshSnapshot({ layer: 1 }));
context.STATE.freshUntil = now - 1;
context.acceptSnapshot(freshSnapshot({ layer: 2 }));
assert.equal(context.STATE.layerSynced, false);
context.acceptSnapshot(freshSnapshot({ layer: 3 }));
assert.equal(context.STATE.layerSynced, true);

// An independently loaded file binds identity before the first status succeeds.
reset(); assert.equal(context.acceptGeometryJob("loaded-file"), true);
context.acceptSnapshot(freshSnapshot({ name: "different-file" }));
assert.equal(context.STATE.jobChanged, true);

// A late old-model response cannot replace the current job's geometry.
reset(); context.acceptSnapshot(freshSnapshot({ name: "current-file" }));
assert.equal(context.acceptGeometryJob("old-file"), false);
assert.equal(context.STATE.jobChanged, true);
reset(); context.acceptSnapshot(freshSnapshot({ name: "current-file" }));
assert.equal(context.acceptGeometryJob("current-file"), true);
assert.equal(context.STATE.jobKey.start, "s1");

console.log("viewer_toolhead_state: ok");
