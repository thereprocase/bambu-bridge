// camera.js — the camera surface (frontend-ux-spec §3.3, contract §12).
//
// Build-free, dependency-free. Two entry points:
//   - mount(root, app)        : the #/camera fullscreen route (router calls this).
//   - mountInto(container,app): the dashboard embeds a card via this (returns a
//                               controller with .destroy()).
//
// Strategy (v0): poll GET /printers/{id}/camera/snapshot.jpg?token=&t={ms} at
// the FPS preference (0.5 / 1 / 2) into a single <img>, NO fade (animation reads
// as jank at 1 fps — §3.3). A ~2.5s first-frame discard skips the stale buffered
// frame the P1S ships on connect (the bridge does server-side discard in v0.1;
// until then the client delays its first DISPLAYED frame). States: loading /
// loaded / stale (>5s since last good frame) / offline / 503 camera_no_frame
// (render the bridge's copy verbatim and keep retrying).
//
// Auth: <img> can't set headers, so the URL carries ?token= (api.tokenUrl).
// We probe the snapshot with fetch() (Bearer header) so we can read the 503
// envelope and decide loaded vs no-frame vs offline; the <img> then renders the
// same bytes via an object URL. This keeps the never-show-stale rule honest:
// a frame is shown only after a successful fetch.

import { el, clear } from './ui.js';
import { api, tokenUrl } from './api.js';

const FIRST_FRAME_DISCARD_MS = 2500;   // §3.3 mount-delay (skip stale buffered frame)
const STALE_AFTER_MS = 5000;           // >5s since last good frame -> "reconnecting"
const CONTRACT_NO_FRAME_MSG = "Camera isn't sending frames yet.";
const CONTRACT_NO_FRAME_HINT = 'The P1S camera takes ~3 seconds to wake up. Try again.';

/**
 * Create a camera view inside `container`. Owns its own poll timer and object
 * URLs; call .destroy() to stop polling and release everything.
 *
 * @param {HTMLElement} container  where the .camera frame mounts
 * @param {any} app                the shared services object
 * @param {object} [opts]
 * @param {boolean} [opts.fullscreen]  fullscreen route (no "3D" affordance dup)
 * @param {()=>void} [opts.onTapFrame] tap the frame (dashboard -> fullscreen)
 * @param {()=>void} [opts.onOpen3d]   tap the "3D" affordance (dashboard only)
 * @returns {{destroy:()=>void, root:HTMLElement}}
 */
export function mountInto(container, app, opts = {}) {
  const pid = app.currentPrinterId();

  const img = el('img', { class: 'camera__img', alt: 'Printer camera', hidden: true });
  const overlay = el('div', { class: 'camera__overlay' });
  const label = el('div', { class: 'camera__label', text: 'Live · updates ~1/sec' });

  const frame = el('div', { class: 'camera' }, [img, overlay, label]);

  // tap the frame -> fullscreen (dashboard only; the fullscreen view doesn't
  // re-open itself).
  if (opts.onTapFrame) {
    frame.style.cursor = 'pointer';
    frame.addEventListener('click', (e) => {
      // don't hijack the 3D button's own click
      if (e.target.closest('.camera__3d')) return;
      opts.onTapFrame();
    });
  }

  // the "3D" affordance: opens the existing WebGL viewer in a new tab (§3, §6.2).
  if (opts.onOpen3d) {
    frame.appendChild(el('button', {
      class: 'btn btn--sm btn--ghost camera__3d',
      text: '3D ⤢',
      title: 'Open the 3D build viewer',
      onClick: (e) => { e.stopPropagation(); opts.onOpen3d(); },
    }));
  }

  container.appendChild(frame);

  // ── state ────────────────────────────────────────────────────────────────
  let destroyed = false;
  let timer = null;
  let inFlight = false;
  let lastGoodAt = 0;            // performance.now() of the last shown frame
  let mountedAt = performance.now();
  let curObjUrl = null;         // current object URL backing the <img>
  let everShown = false;
  /** @type {'loading'|'loaded'|'stale'|'offline'|'no_frame'} */
  let view = 'loading';

  function fps() {
    const f = Number(app.prefs && app.prefs.cameraFps);
    return (f === 0.5 || f === 1 || f === 2) ? f : 1;
  }
  function pollMs() { return Math.round(1000 / fps()); }

  function setOverlay(node) {
    clear(overlay);
    if (node) { overlay.hidden = false; overlay.appendChild(node); }
    else overlay.hidden = true;
  }

  function renderState() {
    frame.classList.toggle('is-stale', view === 'stale');
    if (view === 'loaded') {
      img.hidden = false;
      setOverlay(null);
      return;
    }
    if (view === 'stale') {
      // keep the (dimmed) last frame visible behind the overlay
      img.hidden = !everShown;
      setOverlay(el('div', {}, [
        el('div', { class: 'spinner' }),
        el('div', { class: 'mt-2', text: 'Camera reconnecting…' }),
      ]));
      return;
    }
    if (view === 'no_frame') {
      img.hidden = !everShown;
      setOverlay(el('div', {}, [
        el('div', { text: noFrameMsg }),
        el('div', { class: 'mt-2 dim t-caption', text: noFrameHint }),
      ]));
      return;
    }
    if (view === 'offline') {
      img.hidden = true;
      setOverlay(el('div', { text: 'Camera offline' }));
      return;
    }
    // loading (first frame)
    img.hidden = !everShown;
    setOverlay(el('div', {}, [
      el('div', { class: 'spinner' }),
      el('div', { class: 'mt-2', text: 'Connecting to camera…' }),
    ]));
  }

  let noFrameMsg = CONTRACT_NO_FRAME_MSG;
  let noFrameHint = CONTRACT_NO_FRAME_HINT;

  function swapImage(blob) {
    const next = URL.createObjectURL(blob);
    img.src = next;
    if (curObjUrl) URL.revokeObjectURL(curObjUrl);
    curObjUrl = next;
    everShown = true;
    lastGoodAt = performance.now();
  }

  async function poll() {
    if (destroyed || inFlight || !pid) return;
    inFlight = true;
    try {
      // Bearer-authed fetch so we can read the 503 envelope; the bytes are then
      // rendered via an object URL. (tokenUrl is used only for the <img> fallback
      // / fullscreen <a>; here we want the status code.)
      const res = await api(
        `/printers/${encodeURIComponent(pid)}/camera/snapshot.jpg?t=${Date.now()}`,
        { raw: true },
      );
      if (destroyed) return;

      if (res.ok) {
        const ct = res.headers.get('content-type') || '';
        if (!ct.includes('image')) {
          // a JSON/other body on a 200 is not a frame; treat as no-frame-ish
          await markNotLoaded(res);
        } else {
          const blob = await res.blob();
          if (destroyed) return;
          // first-frame discard: hold the very first frame ~2.5s after mount so
          // we don't display the stale buffered frame (§3.3).
          const sinceMount = performance.now() - mountedAt;
          if (!everShown && sinceMount < FIRST_FRAME_DISCARD_MS) {
            // skip showing this one; keep the "Connecting…" state
            view = 'loading';
          } else {
            swapImage(blob);
            view = 'loaded';
          }
        }
      } else if (res.status === 503) {
        await markNotLoaded(res);
      } else if (res.status === 0) {
        view = 'offline';            // network / no bridge
      } else {
        // camera_unavailable or any other failure: offline placeholder.
        await markNotLoaded(res, /*offlineIfUnknown*/ true);
      }
    } catch {
      if (!destroyed) view = 'offline';
    } finally {
      inFlight = false;
      if (!destroyed) {
        // staleness check: a good frame older than 5s flips to reconnecting.
        if (view === 'loaded' && everShown
            && performance.now() - lastGoodAt > STALE_AFTER_MS) {
          view = 'stale';
        }
        renderState();
      }
    }
  }

  // Parse a non-image response body (503 camera_no_frame etc.). raw:true gives a
  // Response; we read JSON to surface the bridge's verbatim copy.
  async function markNotLoaded(res, offlineIfUnknown) {
    let body = null;
    try { body = await res.clone().json(); } catch { body = null; }
    const err = body && body.error;
    if (err === 'camera_no_frame' || res.status === 503) {
      noFrameMsg = (body && body.message) || CONTRACT_NO_FRAME_MSG;
      noFrameHint = (body && body.remediation_hint) || CONTRACT_NO_FRAME_HINT;
      view = 'no_frame';
    } else if (offlineIfUnknown) {
      view = 'offline';
    } else {
      view = 'no_frame';
      noFrameMsg = (body && body.message) || CONTRACT_NO_FRAME_MSG;
      noFrameHint = (body && body.remediation_hint) || CONTRACT_NO_FRAME_HINT;
    }
  }

  function loop() {
    poll();
    timer = setTimeout(loop, pollMs());
  }

  if (!pid) {
    view = 'offline';
    setOverlay(el('div', { text: 'No printer selected.' }));
    label.hidden = true;
  } else {
    renderState();
    loop();
  }

  return {
    root: frame,
    destroy() {
      destroyed = true;
      if (timer) { clearTimeout(timer); timer = null; }
      if (curObjUrl) { URL.revokeObjectURL(curObjUrl); curObjUrl = null; }
    },
  };
}

/**
 * The #/camera fullscreen route. A back affordance + a large camera view.
 * @param {HTMLElement} root
 * @param {any} app
 * @returns {()=>void} unmount
 */
export function mount(root, app) {
  const shell = el('div', { class: 'app-shell' });
  const main = el('div', { class: 'app-main' });

  const pid = app.currentPrinterId();

  main.appendChild(el('div', { class: 'topbar' }, [
    el('button', {
      class: 'btn btn--sm btn--ghost',
      text: '‹ Back',
      onClick: () => app.navigate('#/'),
    }),
    el('div', { class: 'topbar__title', text: 'Camera' }),
  ]));

  const camWrap = el('div', { class: 'card' });
  main.appendChild(camWrap);

  // The 3D affordance lives here too, for parity with the dashboard card.
  const cam = mountInto(camWrap, app, {
    onOpen3d: pid ? () => open3dViewer(app, pid) : undefined,
  });

  main.appendChild(el('div', { class: 't-caption mt-3 center',
    text: 'Look at the plate — is anything being printed?' }));

  shell.appendChild(main);
  root.appendChild(shell);

  return function unmount() { cam.destroy(); };
}

/**
 * Open the existing WebGL viewer in a new tab with ?token= appended (§6.2).
 * @param {any} app @param {string} pid
 */
export function open3dViewer(app, pid) {
  const url = tokenUrl(`/printers/${encodeURIComponent(pid)}/viz`);
  window.open(url, '_blank', 'noopener');
}
