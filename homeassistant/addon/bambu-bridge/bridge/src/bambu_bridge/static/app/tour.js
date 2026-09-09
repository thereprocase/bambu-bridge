// tour.js — the post-onboarding coachmark tour (frontend-ux-spec §1
// "Post-onboarding TOUR / coachmarks").
//
// CONTRACT NOTE: this module is NOT a screen — the router does not mount it.
// main.js calls `tour.start(app)` after onboarding lands on the dashboard. It
// exports:  export function start(app)
//
// A 4-stop dismissible overlay tour, driven by absolute-positioned tooltips
// anchored to real dashboard elements (no library). Each stop dims the rest of
// the screen (a full-viewport scrim) and outlines the anchored element via the
// .tour-highlight ring. Tour-seen persists as bbl.tourSeen through
// app.markTourSeen().
//
//   1. The glance      → the big status word + percent/time block
//   2. The action bar  → the sticky Pause/Stop/Light/More bar
//   3. The 3D viewer   → the camera card's "3D view" affordance
//   4. Settings        → the gear (nav item)
//
// Anchors: each stop has a prioritized selector list. The preferred anchor is a
// `data-tour="<id>"` attribute the dashboard can put on the right element; we
// fall back to the stable structural classes so the tour still works against
// the foundation. If a stop's element can't be found, that stop is skipped
// rather than pointing at nothing.

const STOPS = [
  {
    id: 'glance',
    selectors: ['[data-tour="glance"]', '.glance', '.glance__status', '.app-main .card'],
    text: 'This is your at-a-glance answer. Status, percent, and time left — the whole point.',
    place: 'below',
  },
  {
    id: 'actionbar',
    selectors: ['[data-tour="actionbar"]', '.actionbar'],
    text: 'Pause, Stop, and the light are always one tap away. Everything else lives under More.',
    place: 'above',
  },
  {
    id: 'viewer',
    selectors: ['[data-tour="viewer"]', '.camera__3d', '.camera'],
    text: 'See the model build layer-by-layer in 3D.',
    place: 'below',
  },
  {
    id: 'settings',
    selectors: ['[data-tour="settings"]', '#nav .nav__item[data-route="#/settings"]', '#nav'],
    text: 'Bridge address, your printers, and app preferences live here.',
    place: 'above',
  },
];

/**
 * Launch the coachmark tour. Safe to call when the dashboard is still settling:
 * it waits briefly for the first anchor, and no-ops gracefully (still marking
 * the tour seen) if the dashboard never produces anchorable elements.
 * @param {any} app  the shared services object (main.js header)
 */
export function start(app) {
  // already seen -> do nothing (defensive; main.js also guards with tourSeen()).
  if (app && typeof app.tourSeen === 'function' && app.tourSeen()) return;

  // build the live stop list (only those whose anchor is currently findable);
  // the dashboard mounts async after the route change, so poll a few frames.
  let cancelled = false;
  let attempts = 0;
  const MAX_ATTEMPTS = 30;      // ~30 * 100ms = 3s grace for the dashboard to paint

  const tryStart = () => {
    if (cancelled) return;
    const firstAnchor = findEl(STOPS[0].selectors);
    if (firstAnchor) { runTour(app); return; }
    if (++attempts >= MAX_ATTEMPTS) {
      // dashboard never produced an anchor (e.g. no printer / failed mount).
      // Mark seen so we don't nag on every load; the tour is a nicety, not a gate.
      markSeen(app);
      return;
    }
    setTimeout(tryStart, 100);
  };
  setTimeout(tryStart, 0);

  // expose a canceller in case a caller wants it later (none currently do)
  return () => { cancelled = true; };
}

function markSeen(app) {
  if (app && typeof app.markTourSeen === 'function') {
    try { app.markTourSeen(); } catch { /* */ }
  }
}

function findEl(selectors) {
  for (const sel of selectors) {
    let node;
    try { node = document.querySelector(sel); } catch { node = null; }
    if (node && isVisible(node)) return node;
  }
  return null;
}

function isVisible(node) {
  const r = node.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return false;
  const cs = getComputedStyle(node);
  return cs.display !== 'none' && cs.visibility !== 'hidden';
}

function runTour(app) {
  // resolve the stops that actually have anchors right now.
  const live = STOPS
    .map((s) => ({ ...s, el: findEl(s.selectors) }))
    .filter((s) => s.el);

  if (!live.length) { markSeen(app); return; }

  // overlay scaffolding
  const scrim = document.createElement('div');
  scrim.className = 'tour-scrim';
  const tip = document.createElement('div');
  tip.className = 'tour-tip';

  const textEl = document.createElement('div');
  textEl.className = 'tour-tip__text';

  const actions = document.createElement('div');
  actions.className = 'tour-tip__actions';

  const skipBtn = document.createElement('button');
  skipBtn.type = 'button';
  skipBtn.className = 'tour-tip__skip';
  skipBtn.textContent = 'Skip tour';

  const nextBtn = document.createElement('button');
  nextBtn.type = 'button';
  nextBtn.className = 'btn btn--primary btn--sm';

  actions.appendChild(skipBtn);
  actions.appendChild(nextBtn);
  tip.appendChild(textEl);
  tip.appendChild(actions);

  document.body.appendChild(scrim);
  document.body.appendChild(tip);

  let i = 0;
  let highlighted = null;

  function clearHighlight() {
    if (highlighted) {
      highlighted.classList.remove('tour-highlight');
      highlighted.style.removeProperty('z-index');
      highlighted = null;
    }
  }

  function position(anchor) {
    const r = anchor.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    const margin = 12;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // horizontally center on the anchor, clamped to the viewport
    let left = r.left + r.width / 2 - tr.width / 2;
    left = Math.max(margin, Math.min(left, vw - tr.width - margin));

    // place above or below depending on the stop hint and available room
    const stop = live[i];
    let top;
    const below = r.bottom + margin;
    const above = r.top - tr.height - margin;
    if (stop.place === 'above' && above >= margin) top = above;
    else if (stop.place === 'below' && below + tr.height <= vh - margin) top = below;
    else if (above >= margin) top = above;          // fall back to whichever fits
    else top = Math.min(below, vh - tr.height - margin);
    top = Math.max(margin, top);

    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function show() {
    const stop = live[i];
    // re-resolve in case layout/route changed under us
    const anchor = findEl(stop.selectors) || stop.el;
    if (!anchor || !document.body.contains(anchor)) {
      // anchor vanished; advance or finish
      if (i < live.length - 1) { i++; show(); return; }
      finish(); return;
    }

    clearHighlight();
    highlighted = anchor;
    anchor.classList.add('tour-highlight');
    // bring the anchor visually above the scrim (the class also does this; this
    // covers elements whose stacking context needs an explicit z-index)
    if (getComputedStyle(anchor).position === 'static') {
      anchor.style.position = 'relative';
    }
    anchor.style.zIndex = '71';
    anchor.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

    textEl.textContent = stop.text;
    nextBtn.textContent = i === live.length - 1 ? 'Done' : 'Next';

    // position after the tip has its final size (next frame)
    requestAnimationFrame(() => position(anchor));
  }

  let finished = false;
  function finish() {
    if (finished) return; finished = true;
    clearHighlight();
    window.removeEventListener('resize', onResize);
    window.removeEventListener('keydown', onKey);
    scrim.remove();
    tip.remove();
    markSeen(app);
  }

  function next() {
    if (i < live.length - 1) { i++; show(); }
    else finish();
  }

  function onResize() { if (!finished) { const a = highlighted; if (a) position(a); } }
  function onKey(e) {
    if (finished) return;
    if (e.key === 'Escape') { e.preventDefault(); finish(); }
    else if (e.key === 'Enter' || e.key === 'ArrowRight') { e.preventDefault(); next(); }
  }

  nextBtn.addEventListener('click', next);
  skipBtn.addEventListener('click', finish);
  // tapping the dimmed scrim advances (a common coachmark idiom)
  scrim.addEventListener('click', next);
  window.addEventListener('resize', onResize);
  window.addEventListener('keydown', onKey);

  show();
}
