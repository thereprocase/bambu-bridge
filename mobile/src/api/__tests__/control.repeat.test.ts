import { request } from "../client";
import { renewHeldJog, startHeldJog, stopHeldJog } from "../control";

jest.mock("../client", () => ({ request: jest.fn(async () => ({})) }));
jest.mock("../../lib/qalog", () => ({ qaLog: jest.fn() }));

const optsOf = (n: number) => jest.mocked(request).mock.calls[n][1] as { repeat?: boolean };

it("sends held-jog start and renew once; stop may be repeated", async () => {
  await startHeldJog("p", { axes: "Z", direction_z: 1 });
  await renewHeldJog("p", "lease");
  await stopHeldJog("p", "lease");
  // A late start or renewal would move the toolhead after the finger lifted.
  expect(optsOf(0).repeat).toBe(false);
  expect(optsOf(1).repeat).toBe(false);
  expect(optsOf(2).repeat).toBeUndefined();
});
