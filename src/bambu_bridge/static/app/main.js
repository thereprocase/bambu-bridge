// main.js — bootstrap + hash router + the Stage-2 screen contract.
//
// Build-free, dependency-free. No minify, no transpile. A line you cannot read
// in the repo is a line you cannot trust. The browser loads this directly via
// <script type="module" src="main.js"> in index.html.
//
// ============================================================================
//  STAGE-2 SCREEN CONTRACT  (authoritative — screen agents conform to this)
// ============================================================================
//
//  Each screen module (onboarding, tour, dashboard, controls, camera, submit,
//  settings) is an ES module that exports a single function:
//
//      export function mount(root, app) { ...; return function unmount() {} }
//
//    - `root`  : the live #app element to render into. The router has already
//                cleared it. The screen builds its DOM here.
//    - `app`   : the shared services object (shape below). Stable for the
//                lifetime of the SPA.
//    - returns : an `unmount()` function (or nothing). The router calls it
//                before mounting the next screen. Use it to close WebSockets,
//                clear intervals, and remove global listeners. Returning
//                nothing is allowed for static screens.
//
//  THE `app` SERVICES OBJECT (passed to every mount):
//
//    app = {
//      // --- modules (import these directly too; this is for convenience) ---
//      store,            // ./store.js   namespace (subscribe, current, viewModel, …)
//      api,              // ./api.js     namespace (api, postJson, testConnection, tokenUrl, …)
//      ws,               // ./ws.js      namespace (connectStatus)
//      ui,               // ./ui.js      namespace (el, confirmSheet, toast, errorCard, …)
//
//      // --- navigation ---
//      navigate(hash),   // set location.hash (e.g. app.navigate('#/print'))
//      route,            // { name, params } the currently-matched route
//
//      // --- auth / identity ---
//      getKey(),         // current bridge key ('' if none)  [= api.getKey]
//      setKey(k),        // persist/clear the bridge key      [= api.setKey]
//      baseUrl(),        // configured base URL ('' = same origin) [= api.getBaseUrl]
//
//      // --- the selected printer (single-printer v0; picker-ready) ---
//      currentPrinterId(),       // string|null  (also store.getState().currentPrinterId)
//      setCurrentPrinterId(id),  // select a printer; persists to localStorage (bbl.currentPrinter)
//
//      // --- preferences (localStorage-backed, see prefs() ) ---
//      prefs,            // live prefs object: {theme,defaultUnloadC,devMode,updateManifestUrl}
//      setPref(k,v),     // update one pref, persist, re-apply theme if needed
//      isDevMode(),      // boolean (gates _raw rendering)
//
//      // --- lifecycle hooks the foundation provides ---
//      applyTheme(),     // re-apply data-theme from prefs + prefers-color-scheme
//      startTour(),      // launch the post-onboarding coachmark tour (tour.js)
//      showNav(show),    // show/hide the bottom-tab/left-rail nav chrome
//      requireKeyScreen(),   // navigate to onboarding/Key screen (auth bounce)
//    }
//
//  ROUTES (hash-based; the bridge needs no server rewrites):
//    #/            -> dashboard   (home; the glance/intervene screen)
//    #/print       -> submit      (file -> AMS map -> confirm -> upload)
//    #/settings    -> settings
//    #/camera      -> camera      (fullscreen camera)
//    #/controls    -> controls    (also openable as an overlay from dashboard)
//    #/onboarding  -> onboarding  (pre-auth wizard; nav hidden)
//    #/setup       -> onboarding  (alias used by "Re-run setup")
//    Unknown hash  -> '#/'.
//
//  AUTH GATING: every route except onboarding requires a stored key. With no
//  key, the router redirects to #/onboarding. The auth interceptor in api.js
//  bounces true bridge-key failures (401) back here via app.requireKeyScreen().
//
//  THEME: data-theme is set on <html> from prefs.theme ('system'|'dark'|
//  'light'); 'system' follows prefers-color-scheme live.
//
// ============================================================================

import * as api from './api.js';
import * as store from './store.js';
import * as ws from './ws.js';
import * as ui from './ui.js';

import * as onboarding from './onboarding.js';
import * as tour from './tour.js';
import * as dashboard from './dashboard.js';
import * as controls from './controls.js';
import * as camera from './camera.js';
import * as submit from './submit.js';
import * as settings from './settings.js';

// ── prefs (localStorage-backed) ─────────────────────────────────────────────
const LS_PREFS = 'bbl.prefs';
const LS_CURRENT = 'bbl.currentPrinter';
const LS_TOUR_SEEN = 'bbl.tourSeen';

const DEFAULT_PREFS = {
  theme: 'system',          // 'system' | 'dark' | 'light'
  defaultUnloadC: 220,      // 180–280
  devMode: false,           // gates _raw rendering
  updateManifestUrl: '',    // off by default (airgap-friendly)
};

function loadPrefs() {
  try {
    const raw = localStorage.getItem(LS_PREFS);
    return raw ? { ...DEFAULT_PREFS, ...JSON.parse(raw) } : { ...DEFAULT_PREFS };
  } catch { return { ...DEFAULT_PREFS }; }
}

const prefs = loadPrefs();

function setPref(k, v) {
  prefs[k] = v;
  try { localStorage.setItem(LS_PREFS, JSON.stringify(prefs)); } catch { /* */ }
  if (k === 'theme') applyTheme();
}

// ── theme ───────────────────────────────────────────────────────────────────
const mql = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

function applyTheme() {
  let theme = prefs.theme;
  if (theme === 'system') theme = (mql && mql.matches) ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#FAFAFA' : '#101113');
}
if (mql) {
  const onChange = () => { if (prefs.theme === 'system') applyTheme(); };
  if (mql.addEventListener) mql.addEventListener('change', onChange);
  else if (mql.addListener) mql.addListener(onChange);  // older Safari
}

// ── nav chrome ──────────────────────────────────────────────────────────────
const navEl = document.getElementById('nav');

function showNav(show) {
  if (!navEl) return;
  navEl.hidden = !show;
}

function setNavActive(hash) {
  if (!navEl) return;
  const base = '#' + (hash.replace(/^#/, '').split('/')[0] === ''
    ? '/' : '/' + hash.replace(/^#\//, '').split('/')[0]);
  navEl.querySelectorAll('.nav__item').forEach((a) => {
    const r = a.getAttribute('data-route');
    const active = r === base || (base === '#/' && r === '#/');
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

// ── current printer ─────────────────────────────────────────────────────────
function currentPrinterId() {
  return store.getState().currentPrinterId
    || localStorage.getItem(LS_CURRENT)
    || null;
}

function setCurrentPrinterId(id) {
  store.setCurrentPrinter(id);
  try {
    if (id) localStorage.setItem(LS_CURRENT, id);
    else localStorage.removeItem(LS_CURRENT);
  } catch { /* */ }
}

// seed the store's current printer from localStorage at boot
store.setCurrentPrinter(currentPrinterId());

// ── the shared `app` services object ────────────────────────────────────────
/** @type {any} */
const app = {
  store, api, ws, ui,
  navigate(hash) { location.hash = hash; },
  route: { name: 'dashboard', params: {} },
  getKey: api.getKey,
  setKey: api.setKey,
  baseUrl: api.getBaseUrl,
  currentPrinterId,
  setCurrentPrinterId,
  prefs,
  setPref,
  isDevMode: () => !!prefs.devMode,
  applyTheme,
  startTour,
  showNav,
  requireKeyScreen,
  tourSeen: () => localStorage.getItem(LS_TOUR_SEEN) === '1',
  markTourSeen: () => { try { localStorage.setItem(LS_TOUR_SEEN, '1'); } catch { /* */ } },
};

// ── auth interceptor wiring ─────────────────────────────────────────────────
api.setAuthHandlers({
  onKeyFailure: requireKeyScreen,
  onAuthNotConfigured: () => { location.hash = '#/onboarding'; },
  onDevMode: () => !!prefs.devMode,
});

function requireKeyScreen() {
  // a true bridge-key failure: send the user to the Key screen. We do NOT wipe
  // the stored key here — onboarding shows it (masked) and only overwrites on a
  // successful Test, avoiding a clear-on-blip loop.
  if (location.hash !== '#/onboarding' && location.hash !== '#/setup') {
    location.hash = '#/onboarding';
  }
}

// ── router ──────────────────────────────────────────────────────────────────
const SCREENS = {
  dashboard,
  submit,
  settings,
  camera,
  controls,
  onboarding,
};

// hash -> {name, module-key}. Order doesn't matter; first segment decides.
const ROUTES = {
  '': { name: 'dashboard', screen: 'dashboard' },
  'print': { name: 'submit', screen: 'submit' },
  'settings': { name: 'settings', screen: 'settings' },
  'camera': { name: 'camera', screen: 'camera' },
  'controls': { name: 'controls', screen: 'controls' },
  'onboarding': { name: 'onboarding', screen: 'onboarding' },
  'setup': { name: 'onboarding', screen: 'onboarding' },
};

const PUBLIC_ROUTES = new Set(['onboarding', 'setup']);

const root = document.getElementById('app');
let unmountCurrent = null;

function parseHash() {
  const hash = location.hash || '#/';
  const path = hash.replace(/^#\/?/, '');        // strip leading "#/" or "#"
  const segs = path.split('/').filter(Boolean);
  const head = segs[0] || '';
  return { head, params: { rest: segs.slice(1) }, raw: hash };
}

function renderRoute() {
  const { head, params, raw } = parseHash();
  let routeDef = ROUTES[head];

  // unknown route -> home
  if (!routeDef) { location.hash = '#/'; return; }

  // auth gate: non-public routes require a stored key
  if (!PUBLIC_ROUTES.has(head) && !api.getKey()) {
    if (head !== '') { /* preserve nothing fancy; just go to onboarding */ }
    location.hash = '#/onboarding';
    return;
  }

  // first-run: a stored key but landing on onboarding is fine (Re-run setup).
  app.route = { name: routeDef.name, params };

  // tear down the previous screen
  if (typeof unmountCurrent === 'function') {
    try { unmountCurrent(); } catch (e) { console.error('unmount threw', e); }
  }
  unmountCurrent = null;
  ui.clear(root);

  // nav chrome: hidden during the wizard, shown otherwise
  const isWizard = PUBLIC_ROUTES.has(head);
  showNav(!isWizard);
  setNavActive(raw);

  const mod = SCREENS[routeDef.screen];
  try {
    const ret = mod.mount(root, app);
    if (typeof ret === 'function') unmountCurrent = ret;
  } catch (e) {
    console.error('screen mount threw', e);
    ui.clear(root);
    root.appendChild(ui.el('div', { class: 'placeholder' }, [
      ui.el('div', { text: 'This screen failed to load.' }),
      ui.el('div', { class: 't-caption mt-2', text: String(e && e.message || e) }),
    ]));
  }
}

window.addEventListener('hashchange', renderRoute);

// ── tour trigger ────────────────────────────────────────────────────────────
function startTour() {
  // tour.js owns its overlay lifecycle; it reads/sets bbl.tourSeen via app.
  try { tour.start(app); } catch (e) { console.error('tour failed', e); }
}

// ── boot ────────────────────────────────────────────────────────────────────
function boot() {
  applyTheme();

  // first-run detection: no stored key -> onboarding.
  if (!api.getKey()) {
    if (location.hash !== '#/onboarding') location.hash = '#/onboarding';
  } else if (!location.hash || location.hash === '#') {
    location.hash = '#/';
  }

  renderRoute();
}

boot();
