import { Alert } from "react-native";

import { confirmUnload } from "../../components/confirmUnload";
import { unloadAvailability } from "../unloadRule";

type AlertButton = { text?: string; style?: string; onPress?: () => void };
const buttonsOf = (call: unknown[]) => call[2] as AlertButton[];

afterEach(() => jest.restoreAllMocks());

it("sends the unload only after the person confirms, and names what is loaded", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  const unload = jest.fn(async () => {});
  confirmUnload(unload, "AMS slot 2");
  expect(unload).not.toHaveBeenCalled();
  const [title, body] = alert.mock.calls[0] as [string, string];
  expect(title).toBe("Unload filament?");
  expect(body).toMatch(/^Unloads AMS slot 2\. The nozzle heats up/);
  expect(buttonsOf(alert.mock.calls[0]).find((b) => b.style === "cancel")!.onPress).toBeUndefined();
  buttonsOf(alert.mock.calls[0]).find((b) => b.text === "Unload")!.onPress!();
  expect(unload).toHaveBeenCalledTimes(1);
  await Promise.resolve();
});

it("ignores a second unload while the first is being sent", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  let finish!: () => void;
  const unload = jest.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  confirmUnload(unload);
  buttonsOf(alert.mock.calls[0]).find((b) => b.text === "Unload")!.onPress!();
  confirmUnload(unload);
  expect(alert).toHaveBeenCalledTimes(1);
  finish();
  await new Promise((r) => setTimeout(r, 0));
  confirmUnload(unload);
  expect(alert).toHaveBeenCalledTimes(2);
});

describe("unloadAvailability matches the bridge's rule", () => {
  const snap = (gcode_state: string, engaged_slot: unknown) => ({ _raw: { gcode_state }, ams: { engaged_slot } });
  it.each([
    ["IDLE", 2, true], ["FINISH", "external", true], ["FAILED", null, true],
    ["RUNNING", 2, false], ["PREPARE", "external", false], ["SLICING", null, false],
    ["PAUSE", 2, false], ["PAUSE", null, false], ["PAUSE", "external", true],
  ])("%s with %p loaded → allowed %p", (state, engaged, allowed) => {
    const result = unloadAvailability(snap(state, engaged));
    expect(result.allowed).toBe(allowed);
    expect(result.reason === null).toBe(allowed);
  });
  it("names the loaded source", () => {
    expect(unloadAvailability(snap("IDLE", 3)).loaded).toBe("AMS slot 3");
    expect(unloadAvailability(snap("IDLE", "external"))).toMatchObject({ loaded: "the external spool", externalLoaded: true });
    expect(unloadAvailability(null)).toMatchObject({ allowed: true, loaded: null, externalLoaded: false });
  });
});
