/** Browser drafts must reach persistent storage before a mutation is sent.
 * Server rendering may read defaults, but may never pretend to persist intent. */
const prefix = "bambu-bridge-app\\\\"; // Same namespace as MMKV's web implementation.
function storage(): Storage {
  if (typeof window === "undefined")
    throw new Error("Browser storage is unavailable during server rendering.");
  return window.localStorage; // Access/quota failures deliberately propagate; no memory fallback.
}
export const kv = {
  getString(key: string): string | undefined {
    if (typeof window === "undefined") return undefined;
    return storage().getItem(prefix + key) ?? undefined;
  },
  getBoolean(key: string): boolean | undefined {
    const value = this.getString(key);
    return value === undefined ? undefined : value === "true";
  },
  set(key: string, value: string | boolean | number): void {
    if (!key || key.includes("\\"))
      throw new Error("Invalid browser storage key.");
    const disk = storage();
    disk.setItem(prefix + key, String(value));
    if (disk.getItem(prefix + key) !== String(value))
      throw new Error("The request could not be saved. Nothing has been sent.");
  },
  remove(key: string): void {
    storage().removeItem(prefix + key);
  },
};
