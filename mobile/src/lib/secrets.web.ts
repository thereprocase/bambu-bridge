/** Manual web sessions keep the API key only in memory. Re-enter it after reload.
 * Android continues using its separate Keystore-backed implementation. */
let bearer: string | null = null;
export async function loadBearer(): Promise<string | null> {
  return bearer;
}
export async function saveBearer(value: string): Promise<void> {
  bearer = value;
}
export async function clearBearer(): Promise<void> {
  bearer = null;
}
