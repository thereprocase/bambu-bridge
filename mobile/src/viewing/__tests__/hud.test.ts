import { CAMERA_HUD_INTERVAL_MS, shouldPublishCameraHud } from "../hud";

const check = (overrides: Partial<Parameters<typeof shouldPublishCameraHud>[0]> = {}) =>
  shouldPublishCameraHud({ previousState: "frame", nextState: "frame", publishedAt: 1000,
    now: 1000 + CAMERA_HUD_INTERVAL_MS, hadFrame: true, ...overrides });

test("publishes at the cadence boundary but not just before it", () => {
  expect(check({ now: 1000 + CAMERA_HUD_INTERVAL_MS - 1 })).toBe(false);
  expect(check()).toBe(true);
});

test("publishes the first frame and recovery immediately", () => {
  expect(check({ previousState: "connecting", hadFrame: false, now: 1001 })).toBe(true);
  expect(check({ previousState: "network", hadFrame: true, now: 1001 })).toBe(true);
});

test("publishes terminal and transport errors immediately on transition", () => {
  expect(check({ nextState: "auth", now: 1001 })).toBe(true);
  expect(check({ nextState: "identity", now: 1001 })).toBe(true);
  expect(check({ nextState: "network", now: 1001 })).toBe(true);
  expect(check({ nextState: "unavailable", now: 1001 })).toBe(true);
  expect(check({ nextState: "network", previousState: "network", now: 1001 })).toBe(false);
});

test("idle stale checks still publish on the next cadence", () => {
  expect(check({ nextState: "frame", now: 1999 })).toBe(false);
  expect(check({ nextState: "frame", now: 2000 })).toBe(true);
});
