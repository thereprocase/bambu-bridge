import * as SecureStore from "expo-secure-store";
import { loadPairing, savePairing, clearPairing } from "../storage";
import type { PairedProfile } from "../protocol";

jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
  deleteItemAsync: jest.fn(),
  WHEN_UNLOCKED_THIS_DEVICE_ONLY: 6,
}));

const key = "bridge.pairedProfile.v1";
const oldProfile: PairedProfile = {
  version: 1,
  baseUrl: "https://paired.invalid:8443/api/v1",
  spki: "A".repeat(43) + "=",
  token: "bbd_" + "b".repeat(43),
  deviceId: "a".repeat(24),
  name: "Existing phone",
};

beforeEach(() => jest.resetAllMocks());

test("loads the existing version-one profile without changing its certificate pin or credential", async () => {
  jest.mocked(SecureStore.getItemAsync).mockResolvedValue(JSON.stringify(oldProfile));
  expect(await loadPairing()).toEqual({ ...oldProfile, remoteUrl: undefined });
  expect(SecureStore.getItemAsync).toHaveBeenCalledWith(key);
  expect(SecureStore.setItemAsync).not.toHaveBeenCalled();
  expect(SecureStore.deleteItemAsync).not.toHaveBeenCalled();
});

test("writes updates to the same secure-store identity with the existing pin intact", async () => {
  await savePairing(oldProfile);
  expect(SecureStore.setItemAsync).toHaveBeenCalledWith(key, JSON.stringify(oldProfile), {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
  expect(SecureStore.deleteItemAsync).not.toHaveBeenCalled();
});

test("a locked secure store does not delete the saved pairing", async () => {
  jest.mocked(SecureStore.getItemAsync).mockRejectedValue(new Error("Device locked"));
  await expect(loadPairing()).rejects.toThrow("Device locked");
  expect(SecureStore.deleteItemAsync).not.toHaveBeenCalled();
  expect(SecureStore.setItemAsync).not.toHaveBeenCalled();
});

test("only an explicit forget operation deletes the pairing", async () => {
  await clearPairing();
  expect(SecureStore.deleteItemAsync).toHaveBeenCalledTimes(1);
  expect(SecureStore.deleteItemAsync).toHaveBeenCalledWith(key);
});
