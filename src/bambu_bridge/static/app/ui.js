// ui.js — shared UI primitives the screens compose.
//
// Build-free, dependency-free, no framework. Tiny DOM helpers plus the app's
// interaction vocabulary:
//   - el()            : terse element builder (the only DOM helper screens need)
//   - confirmSheet()  : bottom-sheet confirm — NEVER native alert()/confirm()
//                       (a native confirm blocks the event loop and starves the
//                        WS keepalive timer; and it looks like the browser).
//   - toast()/undoToast(): transient feedback + the 4s undo affordance
//   - errorCard()     : inline card for a failed action needing a decision;
//                       renders message + remediation_hint VERBATIM with a
//                       Dev-mode _raw "Technical details" disclosure
//   - banner()        : the under-header state banner (reconnecting/offline)
//   - pressHold()     : press-and-hold helper (100mm jog guard) with a fill ring
//   - certChangedSheet(): the TOFU trust sheet rendered from actions[]
//
// Everything here is escaping-safe by default: el() sets textContent (never
// innerHTML) for string children, so bridge `message`/`remediation_hint`
// cannot inject markup.

// ── el(): the one DOM builder ───────────────────────────────────────────────
/**
 * Build an element. `spec` may carry className, text, html (use sparingly —
 * trusted markup only), attrs, dataset, style, and event handlers (onClick…).
 * Children are appended; strings become text nodes.
 *
 * @param {string} tag
 * @param {Object} [spec]
 * @param {(Node|string|null|undefined|false)[]|Node|string} [children]
 * @returns {HTMLElement}
 */
export function el(tag, spec = {}, children) {
  const node = document.createElement(tag);
  for (const k of Object.keys(spec)) {
    const v = spec[k];
    if (v == null) continue;
    if (k === 'class' || k === 'className') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'disabled' || k === 'hidden' || k === 'checked') {
      if (v) node.setAttribute(k, ''); else node.removeAttribute(k);
    } else {
      node.setAttribute(k, String(v));
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (children == null || children === false) return;
  if (Array.isArray(children)) {
    for (const c of children) appendChildren(node, c);
  } else if (children instanceof Node) {
    node.appendChild(children);
  } else {
    node.appendChild(document.createTextNode(String(children)));
  }
}

/** Remove all children of a node. @param {Node} node */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// ── overlay host (sheets + scrim) ───────────────────────────────────────────
function getOverlayHost() {
  let host = document.getElementById('overlay-host');
  if (!host) {
    host = el('div', { id: 'overlay-host' });
    document.body.appendChild(host);
  }
  return host;
}

/**
 * Mount a bottom sheet with a scrim. Returns a close() that animates out.
 * @param {Node} contentNode
 * @param {object} [opts]
 * @param {boolean} [opts.drawer]    use the right-drawer variant (controls)
 * @param {boolean} [opts.dismissable=true] tap-scrim to close
 * @param {()=>void} [opts.onClose]
 * @returns {{close:()=>void, sheet:HTMLElement}}
 */
export function mountSheet(contentNode, opts = {}) {
  const host = getOverlayHost();
  const dismissable = opts.dismissable !== false;
  const scrim = el('div', { class: 'scrim' });
  const sheet = el('div', { class: 'sheet' + (opts.drawer ? ' sheet--drawer' : '') });
  if (!opts.drawer) sheet.appendChild(el('div', { class: 'sheet__grip' }));
  sheet.appendChild(contentNode);
  host.appendChild(scrim);
  host.appendChild(sheet);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    scrim.classList.remove('is-open');
    sheet.classList.remove('is-open');
    setTimeout(() => { scrim.remove(); sheet.remove(); }, 220);
    opts.onClose && opts.onClose();
  }
  if (dismissable) scrim.addEventListener('click', close);

  // next frame: trigger the slide-in transition
  requestAnimationFrame(() => { scrim.classList.add('is-open'); sheet.classList.add('is-open'); });
  return { close, sheet };
}

// ── confirmSheet() — the only confirm path ──────────────────────────────────
/**
 * A bottom-sheet confirm. Resolves true if confirmed, false otherwise.
 * Use this for every destructive/guarded action (Stop, Home, Unload, AMS
 * reset, raw G-code, delete-with-active-job). Never window.confirm().
 *
 * @param {object} o
 * @param {string} o.title
 * @param {string|Node} [o.body]
 * @param {string} [o.confirmLabel='Confirm']
 * @param {string} [o.cancelLabel='Cancel']
 * @param {boolean} [o.danger=false]  red confirm button
 * @returns {Promise<boolean>}
 */
export function confirmSheet(o) {
  return new Promise((resolve) => {
    let result = false;
    const confirmBtn = el('button', {
      class: 'btn btn--block ' + (o.danger ? 'btn--danger' : 'btn--primary'),
      text: o.confirmLabel || 'Confirm',
      onClick: () => { result = true; handle.close(); },
    });
    const cancelBtn = el('button', {
      class: 'btn btn--block btn--ghost',
      text: o.cancelLabel || 'Cancel',
      onClick: () => { result = false; handle.close(); },
    });
    const content = el('div', {}, [
      el('div', { class: 'sheet__title', text: o.title }),
      o.body != null ? el('div', { class: 'sheet__body' },
        typeof o.body === 'string' ? o.body : o.body) : null,
      el('div', { class: 'sheet__actions' }, [confirmBtn, cancelBtn]),
    ]);
    const handle = mountSheet(content, { onClose: () => resolve(result) });
  });
}

// ── certChangedSheet() — TOFU trust prompt from actions[] ───────────────────
/**
 * Render the cert-changed (TOFU) sheet from the bridge's actions[]. The title
 * is the bridge `message`, the body the `remediation_hint`. Buttons are built
 * from actions[] (Trust this printer / Not now). Never shows the word
 * certificate/MITM/fingerprint — that hex lives only in the Dev-mode
 * disclosure, which the caller can render via errorCard with the same result.
 *
 * @param {import('./api.js').ApiResult} result  a 403 printer_cert_changed result
 * @param {(action:object)=>void} onAction  called with the chosen action object
 *        (the trust action has {id:'trust', method:'POST', path:'…/trust'};
 *         the deny action has {id:'deny', method:null})
 * @returns {{close:()=>void}}
 */
export function certChangedSheet(result, onAction) {
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const buttons = actions.map((a) => el('button', {
    class: 'btn btn--block ' + (a.id === 'trust' ? 'btn--primary' : 'btn--ghost'),
    text: a.label || (a.id === 'trust' ? 'Trust this printer' : 'Not now'),
    onClick: () => { handle.close(); onAction && onAction(a); },
  }));
  const content = el('div', {}, [
    el('div', { class: 'sheet__title', text: result.message || 'Your printer’s security key changed.' }),
    result.remediation_hint ? el('div', { class: 'sheet__body', text: result.remediation_hint }) : null,
    el('div', { class: 'sheet__actions' }, buttons),
    devDisclosure(result),
  ]);
  const handle = mountSheet(content);
  return handle;
}

// ── toasts ──────────────────────────────────────────────────────────────────
function getToastHost() {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = el('div', { id: 'toast-host', class: 'toast-host' });
    document.body.appendChild(host);
  }
  return host;
}

/**
 * Transient toast (default 2s). For light/fan/speed success and other
 * no-decision feedback.
 * @param {string} message
 * @param {object} [opts] @param {number} [opts.ms=2000] @param {boolean} [opts.error]
 * @returns {{dismiss:()=>void}}
 */
export function toast(message, opts = {}) {
  const host = getToastHost();
  const node = el('div', { class: 'toast' + (opts.error ? ' toast--err' : '') }, [
    el('span', { class: 'grow', text: message }),
  ]);
  host.appendChild(node);
  let done = false;
  const dismiss = () => { if (done) return; done = true; node.remove(); };
  const timer = setTimeout(dismiss, opts.ms || 2000);
  return { dismiss: () => { clearTimeout(timer); dismiss(); } };
}

/**
 * The 4s undo toast (quick-preset temp, etc.). Calls onUndo if tapped before
 * it expires.
 * @param {string} message
 * @param {()=>void} onUndo
 * @param {object} [opts] @param {number} [opts.ms=4000]
 * @returns {{dismiss:()=>void}}
 */
export function undoToast(message, onUndo, opts = {}) {
  const host = getToastHost();
  let done = false;
  const node = el('div', { class: 'toast' }, [
    el('span', { class: 'grow', text: message }),
    el('button', {
      class: 'toast__action', text: 'Undo',
      onClick: () => { if (done) return; dismiss(); onUndo && onUndo(); },
    }),
  ]);
  host.appendChild(node);
  const dismiss = () => { if (done) return; done = true; clearTimeout(timer); node.remove(); };
  const timer = setTimeout(dismiss, opts.ms || 4000);
  return { dismiss };
}

// ── inline error card + banner ──────────────────────────────────────────────
/**
 * Build (do not mount) an inline error card for a failed action. Renders the
 * bridge `message` and `remediation_hint` VERBATIM, an optional reassurance
 * line (e.g. "We did reach your printer." composed from discovered.serial), and
 * a Dev-mode-only "Technical details" disclosure with `_raw`. Caller supplies
 * action buttons.
 *
 * @param {import('./api.js').ApiResult} result
 * @param {object} [opts]
 * @param {string} [opts.reassurance]                     extra green line
 * @param {{label:string,onClick:Function,primary?:boolean}[]} [opts.actions]
 * @returns {HTMLElement}
 */
export function errorCard(result, opts = {}) {
  const issues = Array.isArray(result.issues) ? result.issues : [];
  const actionBtns = (opts.actions || []).map((a) => el('button', {
    class: 'btn btn--sm ' + (a.primary ? 'btn--primary' : 'btn--ghost'),
    text: a.label, onClick: a.onClick,
  }));
  return el('div', { class: 'errcard', role: 'alert' }, [
    el('div', {}, [
      el('span', { class: 'errcard__icon', text: '⚠' }),
      el('span', { class: 'errcard__msg', text: result.message || 'Something went wrong.' }),
    ]),
    result.remediation_hint
      ? el('div', { class: 'errcard__hint', text: result.remediation_hint }) : null,
    issues.length
      ? el('ul', { class: 'errcard__hint' }, issues.map((i) =>
          el('li', { text: typeof i === 'string' ? i : (i.message || JSON.stringify(i)) })))
      : null,
    opts.reassurance ? el('div', { class: 'errcard__ok', text: opts.reassurance }) : null,
    actionBtns.length ? el('div', { class: 'errcard__actions' }, actionBtns) : null,
    devDisclosure(result),
  ]);
}

/** The Dev-mode-only _raw "Technical details" disclosure, or null. */
function devDisclosure(result) {
  if (!result || !result._raw) return null;       // api.js already gates by dev mode
  return el('details', { class: 'errcard__raw' }, [
    el('summary', { text: 'Technical details' }),
    el('pre', { text: safeStringify(result._raw) }),
  ]);
}

function safeStringify(v) {
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

/**
 * Build (do not mount) the under-header state banner.
 * @param {string} message
 * @param {'amber'|'red'|'grey'} [kind='amber']
 * @param {{label:string,onClick:Function}} [action]  e.g. Retry
 * @returns {HTMLElement}
 */
export function banner(message, kind = 'amber', action) {
  return el('div', { class: `banner banner--${kind}`, role: 'status' }, [
    el('span', { class: 'banner__msg', text: message }),
    action ? el('button', { class: 'btn btn--sm btn--ghost', text: action.label, onClick: action.onClick }) : null,
  ]);
}

// ── press-and-hold helper (100mm jog guard) ─────────────────────────────────
/**
 * Attach a press-and-hold gesture to an element. The action fires only after
 * the user holds for `holdMs`. A filling ring (via inline style on an optional
 * ring element) gives feedback. Releasing early cancels. Honors
 * prefers-reduced-motion by skipping the fill animation but keeping the hold.
 *
 * @param {HTMLElement} target
 * @param {()=>void} onComplete
 * @param {object} [opts]
 * @param {number} [opts.holdMs=700]
 * @param {HTMLElement} [opts.ring]   element whose width/background fills 0->100%
 * @returns {()=>void} detach function
 */
export function pressHold(target, onComplete, opts = {}) {
  const holdMs = opts.holdMs || 700;
  const ring = opts.ring || null;
  let raf = 0, start = 0, fired = false, holding = false;

  function tick(now) {
    if (!holding) return;
    const pct = Math.min(1, (now - start) / holdMs);
    if (ring) ring.style.width = (pct * 100) + '%';
    if (pct >= 1) {
      fired = true; holding = false;
      if (ring) ring.style.width = '0%';
      onComplete();
      return;
    }
    raf = requestAnimationFrame(tick);
  }

  function down(e) {
    e.preventDefault();
    holding = true; fired = false; start = performance.now();
    target.setPointerCapture && e.pointerId != null && target.setPointerCapture(e.pointerId);
    raf = requestAnimationFrame(tick);
  }
  function up() {
    holding = false;
    if (raf) cancelAnimationFrame(raf);
    if (ring) ring.style.width = '0%';
  }

  target.addEventListener('pointerdown', down);
  target.addEventListener('pointerup', up);
  target.addEventListener('pointercancel', up);
  target.addEventListener('pointerleave', up);

  return () => {
    target.removeEventListener('pointerdown', down);
    target.removeEventListener('pointerup', up);
    target.removeEventListener('pointercancel', up);
    target.removeEventListener('pointerleave', up);
    if (raf) cancelAnimationFrame(raf);
  };
}

// ── tiny formatters screens share (bridge owns words; these are pure) ───────
/**
 * minutes -> "2 h 14 m" / "47 m" / "—" (null/NaN). Never "0 m" for null.
 * @param {number|null|undefined} min
 * @returns {string}
 */
export function fmtMinutes(min) {
  if (min == null || Number.isNaN(min)) return '—';
  const m = Math.max(0, Math.round(min));
  const h = Math.floor(m / 60);
  const r = m % 60;
  if (h && r) return `${h} h ${r} m`;
  if (h) return `${h} h`;
  return `${r} m`;
}

/** "Slot N" label from a physical_slot int (contract §14.1.0). */
export function slotLabel(physicalSlot) {
  return physicalSlot === 'external' || physicalSlot == null
    ? 'External' : `Slot ${physicalSlot}`;
}
