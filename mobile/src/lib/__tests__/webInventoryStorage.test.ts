import { kv } from "../kv.web";
import { clearBearer, loadBearer, saveBearer } from "../secrets.web";

let original: PropertyDescriptor | undefined;
const disk = new Map<string, string>();
function browser() {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: {
        getItem: (key: string) => disk.get(key) ?? null,
        setItem: (key: string, value: string) => disk.set(key, value),
        removeItem: (key: string) => disk.delete(key),
      },
    },
  });
}
beforeEach(() => {
  original = Object.getOwnPropertyDescriptor(globalThis, "window");
  disk.clear();
});
afterEach(() => {
  if (original) Object.defineProperty(globalThis, "window", original);
  else delete (globalThis as any).window;
});
test("static rendering reads defaults but cannot pretend to save a request", () => {
  delete (globalThis as any).window;
  expect(kv.getString("pending")).toBeUndefined();
  expect(kv.getBoolean("pending")).toBeUndefined();
  expect(() => kv.set("pending", "request")).toThrow("server rendering");
});
test("web request state reaches browser storage and round-trips exact zero/false/string values", () => {
  browser();
  kv.set("pending", "exact request");
  kv.set("quantity", 0);
  kv.set("disabled", false);
  expect(disk.size).toBe(3);
  expect(kv.getString("pending")).toBe("exact request");
  expect(kv.getString("quantity")).toBe("0");
  expect(kv.getBoolean("disabled")).toBe(false);
  kv.remove("pending");
  expect(kv.getString("pending")).toBeUndefined();
});
test("unavailable persistent browser storage never silently falls back to memory", () => {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      get localStorage() {
        throw new Error("blocked");
      },
    },
  });
  expect(() => kv.set("pending", "request")).toThrow("blocked");
});
test("browser credentials stay in memory and never enter durable draft storage", async () => {
  browser();
  await saveBearer("fixture-credential");
  expect(await loadBearer()).toBe("fixture-credential");
  expect(disk.size).toBe(0);
  await clearBearer();
  expect(await loadBearer()).toBeNull();
});
