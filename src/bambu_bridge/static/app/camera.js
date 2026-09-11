// Continuous, Bearer-authenticated MJPEG. Render every arriving frame, with
// bounded reconnects and a freshness watchdog independent of HTTP success.
import { el, clear } from './ui.js';
import { api, tokenUrl } from './api.js';
import { jpegFrames } from './camera-stream.js';

export function mountInto(container, app, opts = {}) {
  const pid = app.currentPrinterId();
  const img = el('img', { class: 'camera__img', alt: 'Printer camera', hidden: true });
  const overlay = el('div', { class: 'camera__overlay' });
  const label = el('div', { class: 'camera__label', text: 'Live · full camera rate' });
  const frame = el('div', { class: 'camera' }, [img, overlay, label]);
  if (opts.onTapFrame) {
    frame.style.cursor = 'pointer';
    frame.addEventListener('click', e => { if (!e.target.closest('.camera__3d')) opts.onTapFrame(); });
  }
  if (opts.onOpen3d) frame.appendChild(el('button', {
    class: 'btn btn--sm btn--ghost camera__3d', text: '3D ⤢', title: 'Open the 3D build viewer',
    onClick: e => { e.stopPropagation(); opts.onOpen3d(); },
  }));
  container.appendChild(frame);
  let destroyed = false, controller = null, retryTimer = null, retryMs = 1000;
  let lastFrameAt = 0, openedAt = 0, curObjUrl = null;
  const arrivals = [];

  function show(message) {
    frame.classList.toggle('is-stale', !!message && !img.hidden);
    clear(overlay); overlay.hidden = !message;
    if (message) overlay.appendChild(el('div', { text: message }));
  }

  async function connect() {
    if (destroyed) return;
    const active = new AbortController(); controller = active;
    openedAt = performance.now(); lastFrameAt = 0; arrivals.length = 0;
    show(img.hidden ? 'Connecting to camera…' : 'Camera reconnecting…');
    try {
      const response = await api(`/printers/${encodeURIComponent(pid)}/camera/stream.mjpeg`, {
        raw: true, cache: 'no-store', signal: active.signal,
      });
      if (destroyed) return;
      if (!response.ok) {
        show(response.status === 401 ? 'Camera access expired. Reconnect to the bridge.' : 'Camera unavailable. Retrying…');
        return;
      }
      for await (const jpeg of jpegFrames(response)) {
        if (destroyed || active.signal.aborted) break;
        const next = URL.createObjectURL(new Blob([jpeg], { type: 'image/jpeg' }));
        const previous = curObjUrl; curObjUrl = next; img.src = next;
        try { await img.decode(); }
        finally { if (previous) URL.revokeObjectURL(previous); }
        if (destroyed || active.signal.aborted) break;
        lastFrameAt = performance.now(); img.hidden = false; retryMs = 1000;
        arrivals.push(lastFrameAt); if (arrivals.length > 12) arrivals.shift();
        label.textContent = arrivals.length > 1
          ? `Live · ${((arrivals.length-1)*1000/(lastFrameAt-arrivals[0])).toFixed(2)} fps`
          : 'Live · full camera rate';
        show(null);
      }
    } catch {
      if (!destroyed) show('Camera reconnecting…');
    } finally {
      active.abort();
      if (controller === active) controller = null;
      if (!destroyed) {
        retryTimer = setTimeout(connect, retryMs);
        retryMs = Math.min(15000, retryMs * 2);
      }
    }
  }

  const watchdog = setInterval(() => {
    if (destroyed || !controller) return;
    const age = performance.now() - (lastFrameAt || openedAt);
    if (age > 5000) show(img.hidden ? 'Waiting for camera…' : 'Camera reconnecting…');
    if (age > 12000) controller.abort();
  }, 1000);
  if (pid) connect();
  else { show('No printer selected.'); label.hidden = true; }
  return {
    root: frame,
    destroy() {
      destroyed = true; controller?.abort();
      clearTimeout(retryTimer); clearInterval(watchdog);
      img.removeAttribute('src');
      if (curObjUrl) URL.revokeObjectURL(curObjUrl);
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
