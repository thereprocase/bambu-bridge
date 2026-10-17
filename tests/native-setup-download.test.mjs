// Exercise the real guide handlers with synthetic setup material; no credentials,
// network or printer is used. Node provides Blob's actual UTF-8 byte encoding.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../src/bambu_bridge/static/app/native-guide.js', import.meta.url), 'utf8');

function harness(api) {
  const nodes = [], clicks = [], blobs = [], revoked = [], copied = [], timers = new Map();
  class Element {
    constructor(tag, attrs = {}, children = []) { this.tag = tag; Object.assign(this, attrs); this.textContent = attrs.text || ''; this.listeners = {}; this.children = children.flat(); this.isConnected = true; this.classList = { toggle() {} }; nodes.push(this); }
    addEventListener(event, handler) { this.listeners[event] = handler; }
    appendChild(child) { this.children.push(child); }
    append(...children) { this.children.push(...children); }
    remove() { this.isConnected = false; }
    click() { if (this.tag === 'a') clicks.push(this); return this.listeners.click?.(); }
    focus() { this.focused = true; }
    select() { this.selected = true; }
  }
  globalThis.__guideUI = { el: (...args) => new Element(...args), toast() {} };
  globalThis.document = { body: new Element('body') };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => copied.push(text) } } });
  URL.createObjectURL = blob => { blobs.push(blob); return `blob:fixture-${blobs.length}`; };
  URL.revokeObjectURL = url => revoked.push(url);
  globalThis.setTimeout = fn => { const id = timers.size + 1; timers.set(id, fn); return id; };
  globalThis.clearTimeout = id => timers.delete(id);
  const parent = new Element('div'), manual = new Element('details');
  return { nodes, clicks, blobs, revoked, copied, timers, parent, manual, app: { api: { api } }, button: label => nodes.find(n => n.tag === 'button' && n.textContent === label) };
}
const moduleSource = source.replace("import { el, toast } from './ui.js';", 'const { el, toast } = globalThis.__guideUI;');
let nonce = 0;
async function mount(h, current = () => true) {
  const m = await import('data:text/javascript;base64,' + Buffer.from(moduleSource + `\n// ${nonce++}`).toString('base64'));
  return m.mountNativeGuide(h.parent, h.app, current, h.manual);
}
const copyLabel = 'Copy Windows setup command', fileLabel = 'Download Windows setup (.ps1)';

test('download contains the entire same script, with UTF-8 BOM and CRLF for Windows', async () => {
  const template = await readFile(new URL('../src/bambu_bridge/templates/orca-windows.ps1', import.meta.url), 'utf8');
  const script = template.replace('__BRIDGE_SETUP_DATA__', 'SYNTHETIC-NOT-A-CREDENTIAL') + '\n# café and 日本語\r\n';
  const requests = [];
  const h = harness(async (...args) => { requests.push(args); return { ok: true, data: { script } }; });
  const stop = await mount(h);
  await h.button(fileLabel).click();
  assert.equal(h.clicks.length, 1);
  assert.equal(h.clicks[0].download, 'bambu-bridge-orca-setup.ps1');
  const bytes = new Uint8Array(await h.blobs[0].arrayBuffer());
  assert.deepEqual([...bytes.slice(0, 3)], [0xef, 0xbb, 0xbf]);
  assert.equal(new TextDecoder().decode(bytes), script.replace(/\r?\n/g, '\r\n'));
  assert.equal(h.revoked.length, 0, 'URL remains valid while the browser starts saving');
  await h.button(copyLabel).click();
  assert.deepEqual(h.copied, [script]);
  assert.deepEqual(requests, Array(2).fill(['/native/setup', { cache: 'no-store' }]));
  assert.equal(h.button(fileLabel).disabled, false);
  stop();
  assert.deepEqual(h.revoked, ['blob:fixture-1']);
  assert.equal(h.timers.size, 0);
  assert.equal(h.nodes.find(n => n.tag === 'textarea').value, '');
});

test('failed setup or network failure makes no downloadable file and allows retry', async () => {
  let failNetwork = false;
  const h = harness(async () => { if (failNetwork) throw Error('network'); return { ok: false, message: 'Enable Orca first.' }; });
  const stop = await mount(h);
  await h.button(fileLabel).click();
  assert.equal(h.blobs.length, 0); assert.equal(h.manual.open, true);
  assert.equal(h.button(fileLabel).disabled, false);
  failNetwork = true;
  await h.button(fileLabel).click();
  assert.equal(h.clicks.length, 0); assert.equal(h.button(copyLabel).disabled, false);
  stop();
});

test('late response after leaving the guide cannot download setup material', async () => {
  let resolve;
  const h = harness(() => new Promise(r => { resolve = r; }));
  const stop = await mount(h);
  const pending = h.button(fileLabel).click();
  assert.equal(h.button(copyLabel).disabled, true);
  stop(); resolve({ ok: true, data: { script: 'fixture' } });
  await pending;
  assert.equal(h.blobs.length, 0);
});

test('each download fetches fresh setup, and its URL expires', async () => {
  let serial = 0;
  const h = harness(async () => ({ ok: true, data: { script: `fixture-${++serial}` } }));
  const stop = await mount(h);
  await h.button(fileLabel).click(); await h.button(fileLabel).click();
  assert.equal(await h.blobs[0].text(), 'fixture-1');
  assert.equal(await h.blobs[1].text(), 'fixture-2');
  for (const expire of [...h.timers.values()]) expire();
  assert.deepEqual(h.revoked, ['blob:fixture-1', 'blob:fixture-2']);
  stop(); assert.equal(h.revoked.length, 2);
});
