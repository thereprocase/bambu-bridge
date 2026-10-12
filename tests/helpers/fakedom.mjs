// Minimal DOM for node --test: enough of document/Node for ui.js el(),
// mountSheet(), toast() and the screens built on them. No layout, no CSS.
// Import this module BEFORE any static/app module.

class FakeNode {
  constructor(tag) {
    this.tagName = (tag || '').toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this.attrs = {};
    this.listeners = {};
    this._text = '';
    this.className = '';
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) { this.children.splice(i, 1); c.parentNode = null; }
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(v) { this.textContent = v; }
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === 'id') this.id = v;
    if (k === 'disabled' || k === 'hidden') this[k] = true;
  }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) {
    delete this.attrs[k];
    if (k === 'disabled' || k === 'hidden') this[k] = false;
  }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  removeEventListener() {}
  /** Fire listeners of type t; returns their (possibly async) results. */
  fire(t, extra = {}) {
    return Promise.all((this.listeners[t] || []).map((f) =>
      f({ target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...extra })));
  }
  focus() { globalThis.document.activeElement = this; }
  blur() { if (globalThis.document.activeElement === this) globalThis.document.activeElement = null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === globalThis.document.body;
  }
}

class FakeText extends FakeNode {
  constructor(t) { super('#text'); this._text = String(t); }
}

const body = new FakeNode('body');
globalThis.Node = FakeNode;
globalThis.document = {
  body,
  activeElement: null,
  createElement: (t) => new FakeNode(t),
  createTextNode: (t) => new FakeText(t),
  getElementById(id) { return find(body, (n) => n.id === id)[0] || null; },
  addEventListener() {},
  removeEventListener() {},
};
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
  clear: () => { for (const k of Object.keys(store)) delete store[k]; },
};
if (!globalThis.location) globalThis.location = { hash: '#/', origin: 'http://bridge.test' };

/** Every node under root (inclusive) matching pred. */
export function find(root, pred) {
  const out = [];
  const walk = (n) => { if (pred(n)) out.push(n); n.children.forEach(walk); };
  walk(root);
  return out;
}

/** Buttons under root whose text is exactly `text`. */
export function buttons(root, text) {
  return find(root, (n) => n.tagName === 'BUTTON' && n.textContent === text);
}

/** Remove everything mounted so far (sheets, toasts). */
export function resetBody() {
  body.children.slice().forEach((c) => body.removeChild(c));
  globalThis.document.activeElement = null;
}

export const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
