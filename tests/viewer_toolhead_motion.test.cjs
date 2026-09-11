const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync(
  "src/bambu_bridge/static/viewer.html",
  "utf8",
);
const block = html.match(/\/\/ BEGIN TOOLHEAD MOTION[\s\S]*?\/\/ END TOOLHEAD MOTION/)[0];
const sandbox = {};
vm.runInNewContext(`${block}\nthis.buildMotionPath=buildMotionPath;this.sampleMotionPath=sampleMotionPath;`, sandbox);
const { buildMotionPath, sampleMotionPath } = sandbox;

const tp = (pos) => ({ pos: new Float32Array(pos) });
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-5, `${actual} != ${expected}`);

{
  const path = buildMotionPath(tp([0,0,0, 10,0,0, 20,0,0, 20,0,10]), {v0:0,v1:3});
  assert.equal(path.segments.length, 3);
  assert.equal(path.segments[1].travel, true);
  near(path.total, 10 + 10 / 3 + 10);
  assert.deepEqual(Array.from(sampleMotionPath(path, 0).position), [0,0,0]);
  assert.deepEqual(Array.from(sampleMotionPath(path, 1).position), [20,0,10]);
  const crossing = sampleMotionPath(path, (10 + 1) / path.total);
  assert.equal(crossing.travel, true);
  assert.equal(crossing.reveal, 1);
  near(crossing.position[0], 13);
  near(crossing.position[2], 0);
  const extrusion = sampleMotionPath(path, (10 + 10/3 + 5) / path.total);
  assert.equal(extrusion.travel, false);
  near(extrusion.reveal, 2.5);
}

{
  const path = buildMotionPath(tp([0,0,0, 1,0,0, 100,100,100, 101,100,100]), {v0:0,v1:1});
  assert.equal(path.segments.length, 1);
  assert.deepEqual(Array.from(sampleMotionPath(path, -2).position), [0,0,0]);
  assert.deepEqual(Array.from(sampleMotionPath(path, 2).position), [1,0,0]);
}

{
  const path = buildMotionPath(tp([0,0,0, 1,0,0, 10,0,0, 12,0,0]), {v0:1,v1:3});
  assert.equal(path.segments.length, 1);
  assert.equal(path.segments[0].reveal0, 2);
}

for (const span of [{v0:0,v1:-1}, {v0:99,v1:2}, null]) {
  const path = buildMotionPath(tp([]), span);
  const sample = sampleMotionPath(path, 0.5);
  assert.equal(sample.position, null);
  assert.equal(sample.reveal, 0);
  assert.equal(sample.travel, false);
}
{
  const path = buildMotionPath(tp([0,0,0, 1,0,0, NaN,0,0, 2,0,0]), {v0:0,v1:3});
  assert.equal(path.segments.length, 1);
  assert.doesNotThrow(() => sampleMotionPath(path, NaN));
}

console.log("viewer_toolhead_motion: ok");
