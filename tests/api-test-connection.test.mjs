// "Test connection" checks a candidate address/key without saving either and
// without the 401 interceptor (which would navigate to onboarding).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import './helpers/fakedom.mjs';

const api = await import(new URL('../src/bambu_bridge/static/app/api.js', import.meta.url));

let calls;
let keyFailures;
function respond(status, body) {
  return { ok: status < 300, status, json: async () => body, text: async () => '',
    headers: { get: (h) => (h.toLowerCase() === 'content-type' ? 'application/json' : null) } };
}

beforeEach(() => {
  localStorage.clear();
  api.setBaseUrl('https://saved.example');
  api.setKey('saved-key');
  calls = [];
  keyFailures = 0;
  api.setAuthHandlers({ onKeyFailure: () => { keyFailures += 1; }, onAuthNotConfigured: () => { keyFailures += 1; } });
});

test('a rejected key leaves the saved address and key alone and does not navigate', async () => {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, auth: init.headers.get('Authorization') });
    return url.endsWith('/health') ? respond(200, { ok: true })
      : respond(401, { error: 'auth_invalid', message: 'bad key' });
  };
  const res = await api.testConnection('https://typo.example/', 'wrong-key');
  assert.equal(res.outcome, 'key_rejected');
  assert.equal(calls[0].url, 'https://typo.example/api/v1/health');
  assert.equal(calls[0].auth, null, '/health is unauthenticated');
  assert.equal(calls[1].url, 'https://typo.example/api/v1/printers');
  assert.equal(calls[1].auth, 'Bearer wrong-key');
  assert.equal(api.getBaseUrl(), 'https://saved.example');
  assert.equal(api.getKey(), 'saved-key');
  assert.equal(keyFailures, 0);
});

test("an empty address tests this page's origin", async () => {
  globalThis.fetch = async (url) => { calls.push({ url }); return respond(200, []); };
  const res = await api.testConnection('', 'k');
  assert.equal(res.outcome, 'connected');
  assert.equal(calls[0].url, `${location.origin}/api/v1/health`);
});

test('ordinary calls still use the saved address and run the interceptor', async () => {
  globalThis.fetch = async (url) => { calls.push({ url }); return respond(401, { error: 'auth_invalid' }); };
  await api.api('/printers');
  assert.equal(calls[0].url, 'https://saved.example/api/v1/printers');
  assert.equal(keyFailures, 1);
});
