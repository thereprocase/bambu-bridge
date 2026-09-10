/**
 * QA instrumentation log — emits structured lines to logcat under the
 * ReactNativeJS tag. Consumers reassemble split lines and parse the JSON
 * payload for assertion-driven verification without screenshots.
 *
 * SECURITY CONTRACT: qaLog MUST NEVER log tokens, bearer keys, full URLs,
 * or access codes. All call sites must strip or omit those fields before
 * passing the payload. This contract is enforced at the call sites, not here
 * — but this module is also the last line of defence: if a payload somehow
 * contains a field named `token`, `bearer`, `key`, or `access_code`, it is
 * silently redacted below.
 *
 * console.log reaches logcat under the ReactNativeJS tag in both debug and
 * release builds via Hermes/Metro. No babel transform strips console.* in
 * this project (no babel.config.js exists; Expo default has no
 * transform-remove-console preset). If that ever changes, add this module
 * to any exclusion list.
 *
 * Logcat line limit: Android truncates a single log line at ~4000 bytes.
 * Payloads exceeding MAX_CHUNK are split into numbered continuation lines:
 *   QA <event>#1 <first chunk>
 *   QA <event>#2 <next chunk>
 * Consumers concatenate the chunks in order (matching on seq) to recover the
 * full payload.
 */

// Monotonic counter — resets on JS reload (acceptable; seq is for ordering,
// not globally unique IDs).
let _seq = 0;

// Redacted sentinel so consumers know a field was stripped, not absent.
const REDACTED = "**redacted**";

// Sensitive field names — redacted recursively, including page-supplied payloads.
const SENSITIVE_KEYS = new Set([
  "token", "bearer", "key", "accesscode", "password", "secret", "apikey",
  "authorization", "headers", "url", "uri", "host", "hostname", "baseurl", "baseurllan",
  "detail", "message", "reason", "n", "name", "filename", "printer", "serial",
]);
const secrets = new Set<string>();

export function registerQaSecret(value: string | null): void {
  if (!value) return;
  secrets.add(value);
  secrets.add(encodeURIComponent(value));
}

function redactText(value: string): string {
  for (const secret of secrets) value = value.split(secret).join(REDACTED);
  return value
    .replace(/[a-z][a-z0-9+.-]*:\/\/[^\s"<>]+/gi, REDACTED)
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, REDACTED)
    .replace(/([?&](?:token|key|api_key|access_code)=)[^&\s]+/gi, "$1" + REDACTED)
    .replace(/\bBearer\s+[^\s"']+/gi, "Bearer " + REDACTED);
}

// Maximum byte length for a single logcat line's JSON body. We stay well
// under Android's ~4096-byte hard limit to leave headroom for the tag prefix
// and event name.
const MAX_CHUNK = 3800;

function redactSensitive(payload: unknown, seen = new WeakSet<object>(), depth = 0): unknown {
  if (typeof payload === "string") return redactText(payload);
  if (!payload || typeof payload !== "object") return payload;
  if (seen.has(payload) || depth > 8) return REDACTED;
  seen.add(payload);
  const safe = Array.isArray(payload)
    ? payload.map((value) => redactSensitive(value, seen, depth + 1))
    : Object.fromEntries(Object.entries(payload).map(([key, value]) => [
        redactText(key), SENSITIVE_KEYS.has(key.toLowerCase().replace(/[_-]/g, ""))
          ? REDACTED : redactSensitive(value, seen, depth + 1),
      ]));
  seen.delete(payload);
  return safe;
}

export function qaLog(event: string, payload: unknown): void {
  event = redactText(event);
  if (!/^[a-z][a-z0-9_.]{0,63}$/.test(event)) event = "unknown";
  const seq = ++_seq;
  const wrapped = {
    seq,
    t: Date.now(),
    ...(redactSensitive(payload) as object),
  };

  const json = JSON.stringify(wrapped);

  if (json.length <= MAX_CHUNK) {
    console.log("QA " + event + " " + json);
    return;
  }

  // Split into numbered chunks. Each chunk is a raw string slice of the
  // serialised JSON; consumers must concatenate chunks in order before parsing.
  let part = 1;
  let offset = 0;
  while (offset < json.length) {
    const slice = json.slice(offset, offset + MAX_CHUNK);
    console.log("QA " + event + "#" + part + " " + slice);
    offset += MAX_CHUNK;
    part++;
  }
}
