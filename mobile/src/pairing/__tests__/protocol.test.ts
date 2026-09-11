import { parseInvitation, parseProfile } from "../protocol";
import { decodeBase64 } from "../native";

const invitation = { type: "bambu-bridge-pair", version: 1,
  base_url: "https://bridge.invalid:8443/api/v1", spki: "A".repeat(43) + "=",
  secret: "b".repeat(43), expires: 10600 };
const parse = (changes = {}) => parseInvitation(JSON.stringify({ ...invitation, ...changes }), 10_000_000);
test("accepts an HTTPS QR without importing credentials from a URL", () => {
  expect(parse().base_url).toBe(invitation.base_url);
});
test.each(["http://bridge.invalid/api/v1", "https://user:pass@bridge.invalid/api/v1",
  "https://bridge.invalid/api/v1?token=secret", "https://bridge.invalid/api/v1#x",
  "https://bridge.invalid/app"])("rejects unsafe base %s", (base_url) => {
  expect(() => parse({ base_url })).toThrow();
});
test("rejects expired, wrong-version, and malformed identity codes", () => {
  for (const changes of [{ expires: 9999 }, { expires: 20000 }, { version: 2 },
    { spki: "unknown" }, { secret: "123456" }, { type: "another-app" }]) {
    expect(() => parse(changes)).toThrow();
  }
});
test("stored paired credentials cannot acquire an HTTP remote fallback", () => {
  expect(() => parseProfile(JSON.stringify({ version: 1, baseUrl: invitation.base_url,
    spki: invitation.spki, token: "bbd_" + "x".repeat(43), deviceId: "a".repeat(24), name: "Phone",
    remoteUrl: "http://bridge.invalid/api/v1" }))).toThrow();
});
test.each(["", "a", "ab", "abc", "binary\x00\xff"])("camera binary survives native transport", (text) => {
  const original = Buffer.from(text, "latin1");
  expect(Buffer.from(decodeBase64(original.toString("base64")))).toEqual(original);
});
