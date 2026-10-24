import { inventoryReady, type InventoryStatus } from "../../api/inventory";

// Hoisted above the import by babel-jest.
jest.mock("../../api/client", () => ({ request: jest.fn() }));

const status: InventoryStatus = {
  configured: true, api_version: 2, accounting_mode: "disabled", native_overlay_enabled: false,
};
test("spool workflows need Spoolman configured and this app's inventory API", () => {
  expect(inventoryReady(status)).toBe(true);
  expect(inventoryReady(null)).toBe(false);
  expect(inventoryReady({ ...status, configured: false })).toBe(false);
  expect(inventoryReady({ ...status, api_version: 1 })).toBe(false);
});
