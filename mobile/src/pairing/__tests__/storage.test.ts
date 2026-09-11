import { useBridgeStore } from "../../store/bridge";
import { configureTransport } from "../native";
import { loadPairing, savePairing, clearPairing } from "../storage";
import { loadBearer, saveBearer } from "../../lib/secrets";
import type { PairedProfile } from "../protocol";

jest.mock("../native", () => ({ configureTransport: jest.fn().mockResolvedValue(undefined) }));
jest.mock("../storage", () => ({ loadPairing: jest.fn(), savePairing: jest.fn(), clearPairing: jest.fn() }));
jest.mock("../../lib/secrets", () => ({ loadBearer: jest.fn(), saveBearer: jest.fn(), clearBearer: jest.fn() }));
jest.mock("../../lib/kv", () => ({ kv: {
  getString: (key: string) => key === "bridge.baseUrl" ? "http://manual.invalid/api/v1" : undefined,
  set: jest.fn(), remove: jest.fn(),
} }));

const profile: PairedProfile = { version: 1, baseUrl: "https://paired.invalid:8443/api/v1",
  spki: "A".repeat(43) + "=", token: "bbd_" + "b".repeat(43), deviceId: "a".repeat(24), name: "Phone" };
beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(configureTransport).mockResolvedValue(undefined);
  jest.mocked(savePairing).mockResolvedValue(undefined);
  jest.mocked(clearPairing).mockResolvedValue(undefined);
  jest.mocked(loadBearer).mockResolvedValue("old-manual-key");
  useBridgeStore.setState({ pairing: null, bearer: null, bootstrapped: false });
});
test("pairing keeps the manual credential separate and stores one complete secure profile", async () => {
  await useBridgeStore.getState().activatePairing(profile);
  expect(savePairing).toHaveBeenCalledWith(profile);
  expect(saveBearer).not.toHaveBeenCalled();
  expect(useBridgeStore.getState().bearer).toBe(profile.token);
});
test("a failed secure write rolls native trust back and leaves the previous configuration", async () => {
  jest.mocked(savePairing).mockRejectedValueOnce(new Error("locked"));
  await expect(useBridgeStore.getState().activatePairing(profile)).rejects.toThrow();
  expect(configureTransport).toHaveBeenLastCalledWith(null);
  expect(useBridgeStore.getState().pairing).toBeNull();
});
test("unreadable pairing fails closed instead of using an old HTTP credential", async () => {
  jest.mocked(loadPairing).mockRejectedValueOnce(new Error("locked"));
  await useBridgeStore.getState().bootstrap();
  expect(loadBearer).not.toHaveBeenCalled();
  expect(useBridgeStore.getState().bearer).toBeNull();
  expect(useBridgeStore.getState().bootstrapped).toBe(true);
});
test("forgetting a paired bridge restores the preserved manual configuration", async () => {
  useBridgeStore.setState({ pairing: profile, bearer: profile.token });
  await useBridgeStore.getState().forgetPairing();
  expect(clearPairing).toHaveBeenCalled();
  expect(useBridgeStore.getState().baseUrl).toBe("http://manual.invalid/api/v1");
  expect(useBridgeStore.getState().bearer).toBe("old-manual-key");
});
