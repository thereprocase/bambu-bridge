import { useBridgeStore } from "../../store/bridge";
import { loadBearer, clearBearer } from "../secrets";

jest.mock("../kv", () => ({ kv: { getString: () => undefined, set: jest.fn(), remove: jest.fn() } }));
jest.mock("../secrets", () => ({ loadBearer: jest.fn(), clearBearer: jest.fn(), saveBearer: jest.fn() }));

it("finishes bootstrap when the Keystore read fails without deleting the saved key", async () => {
  jest.mocked(loadBearer).mockRejectedValueOnce(new Error("locked"));
  await expect(useBridgeStore.getState().bootstrap()).resolves.toBeUndefined();
  expect(useBridgeStore.getState().bootstrapped).toBe(true);
  expect(useBridgeStore.getState().bearer).toBeNull();
  expect(useBridgeStore.getState().health.state).toBe("unreachable");
  expect(clearBearer).not.toHaveBeenCalled();
});
