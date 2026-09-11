import { viewerFailureReducer as update } from "../viewerFailure";

test("a successful fallback removes a recoverable page error", () => {
  const failed = update(null, { type: "failed", message: "Could not load toolpath" });
  expect(update(failed, { type: "ready" })).toBeNull();
});

test("page readiness cannot clear native identity or authentication failures", () => {
  for (const message of ["Identity changed", "Access rejected"]) {
    const blocked = update(null, { type: "failed", message, fatal: true });
    expect(update(blocked, { type: "ready" })).toEqual(blocked);
    expect(update(blocked, { type: "failed", message: "Network issue" })).toEqual(blocked);
    const reloaded = update(blocked, { type: "reload" });
    expect(update(reloaded, { type: "ready" })).toBeNull();
  }
});
