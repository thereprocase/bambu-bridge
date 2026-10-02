// controls.js — the controls surface (frontend-ux-spec §4).
//
// Bottom sheet on phone / right drawer on desktop (ui.mountSheet w/ the
// 'sheet--drawer' variant). Sub-panels: PRINT (pause/resume/stop-confirm),
// Light, Temperature (§4.1), Move/Home (§4.2), AMS (§4.3), Fans (§4.4),
// Speed (§4.5), and the gated raw-gcode console (§4.6).
//
// Two entry points, one body:
//   - mount(root, app)        — the standalone #/controls route. Opens its OWN
//                               status WS (the dashboard may not be mounted),
//                               renders the controls sheet, and navigates back
//                               to '#/' when the sheet is dismissed. Returns an
//                               unmount() that closes the WS + the sheet.
//   - open(app, opts)         — the opener the dashboard's "More" button calls.
//                               Reuses the WS the dashboard already has live
//                               (reads from the store; does NOT open a second
//                               connection), mounts the sheet as an overlay, and
//                               returns {close}.
//
// Rendering: the sheet is built ONCE. Store updates (one per telemetry delta,
// every 1-4 s while printing) only run small updaters that patch text,
// enablement and values in place, so a command error, a half-typed value, a
// focused field or a dragged slider survives telemetry. Only the Move and AMS
// sections rebuild, and only when the print state changes what they offer.
//
// Behaviour follows OrcaSlicer's device panel (StatusPanel.cpp):
//   - Disconnected → everything disabled (show_printing_status(false,false)).
//   - While a print runs, motion and AMS load/unload are refused; a paused
//     print allows motion and the external spool only (show_printing_status,
//     update_ams_control_state). Heaters, fans, speed and light stay enabled.
//   - Jog steps are 1 and 10 mm, XY at F3000 and Z at F900 (on_axis_ctrl_*).
//   - A heater/fan/speed control is left alone while it has focus and for a
//     few updates after a Set (set_hold_count, COMMAND_TIMEOUT), so stale
//     telemetry cannot snap it back. Out-of-range targets clamp to the max.
//   - The speed level shown is the printer's reported spd_lvl.
//
// Other rules:
//   - Skip objects opens skip-objects.js (Orca's PartSkipDialog).
//   - Stop is the only confirming PRINT action; after confirm it shows
//     "Stopping…" until the phase leaves the active print.
//   - AMS labels are "Slot 1..4" (physical_slot); /ams/change target_tray is
//     0-based (we subtract 1 from the picked physical slot).
//   - Raw G-code always confirms with the literal line; a 403 raw_gcode_disabled
//     locks the console with the enable hint.
//   - Bridge `message` + `remediation_hint` render VERBATIM via ui.errorCard in
//     one sheet-level slot under the header.

import {
  el, clear, mountSheet, confirmSheet, toast, undoToast, errorCard, slotLabel,
} from './ui.js';
import { printerStateLabels } from './printer-state.js';
import * as skipObjects from './skip-objects.js';

// OrcaSlicer's jog steps (mm) and feed rates (mm/min). The bridge's
// api/control.py ALLOWED_STEPS refuses any other step; a parity test keeps
// this list, the app's STEP_MM and ALLOWED_STEPS equal.
export const JOG_STEPS = [1, 10];
const JOG_FEED = { X: 3000, Y: 3000, Z: 900 };
// Store updates a control ignores after the user sets it (Orca COMMAND_TIMEOUT).
const HOLD_UPDATES = 5;
const NOZZLE_MAX_C = 300;   // Orca's default; a reported nozzle_temp_range wins
const BED_MAX_C = 100;      // P1S; a reported bed_temp_range wins
const BUSY_MESSAGE = 'The printer is busy with another print job.';
const SKIP_STATES = new Set(['RUNNING', 'PAUSE']);
const PAUSED_FILAMENT_MESSAGE =
  'When printing is paused, filament loading and unloading are only supported for external slots.';

// ── public: the standalone #/controls route ─────────────────────────────────
/**
 * @param {HTMLElement} root
 * @param {any} app
 * @returns {() => void}
 */
export function mount(root, app) {
  const pid = app.currentPrinterId();

  if (!pid) {
    root.appendChild(el('div', { class: 'placeholder' }, [
      el('div', { text: 'No printer selected.' }),
      el('button', {
        class: 'btn btn--ghost mt-3', text: 'Go to Settings',
        onClick: () => app.navigate('#/settings'),
      }),
    ]));
    return function unmount() {};
  }

  // The route owns the full viewport but the controls live in a sheet/drawer.
  root.appendChild(el('div', { class: 'placeholder' }, [
    el('div', { text: 'Controls' }),
    el('div', { class: 't-caption mt-2', text: 'Close to return to the dashboard.' }),
  ]));

  // Open our own WS so the controls work standalone. Seed from cache so the
  // first paint isn't blank.
  app.store.loadCachedSnapshot(pid);
  const conn = app.ws.connectStatus(pid, {
    onUnauthorized: () => app.requireKeyScreen(),
    onUnknownPrinter: () => app.navigate('#/settings'),
  });

  const handle = openControlsSheet(app, pid, {
    onClose: () => { if (location.hash === '#/controls') app.navigate('#/'); },
  });

  return function unmount() {
    handle.close();
    conn.close();
  };
}

// ── public: the dashboard "More" opener (overlay; reuses the live WS) ────────
/**
 * @param {any} app
 * @param {object} [opts]
 * @returns {{close:()=>void}}
 */
export function open(app, opts = {}) {
  const pid = app.currentPrinterId();
  if (!pid) {
    toast('No printer selected.', { error: true });
    return { close() {} };
  }
  return openControlsSheet(app, pid, opts);
}

// ── the sheet: built once, patched on every store change ────────────────────
function openControlsSheet(app, pid, opts = {}) {
  const local = { stopping: false, step: 1, gcodeDisabled: false };
  const errSlot = el('div', { class: 'controls-err' });
  const titleEl = el('div', { class: 'sheet__title', style: { margin: '0' } });
  const stack = el('div', { class: 'stack' });
  const updaters = [];
  let sheetHandle = null;

  const ctx = {
    app, pid, local,
    state: () => sheetState(app, pid),
    post: (path, body) => app.api.postJson(`/printers/${pid}${path}`, body),
    /** Show a failed result (or clear the slot with null). */
    report: (r) => { clear(errSlot); if (r) errSlot.appendChild(errorCard(r)); },
    update: (fn) => updaters.push(fn),
  };

  const header = el('div', { class: 'row row--between mb-4' }, [
    titleEl,
    el('button', {
      class: 'input-eye', 'aria-label': 'Close controls', text: '✕',
      style: { position: 'static', width: '36px', height: '36px' },
      onClick: () => sheetHandle.close(),
    }),
  ]);

  stack.appendChild(printPanel(ctx));
  stack.appendChild(lightPanel(ctx));
  stack.appendChild(section('TEMPERATURE', temperaturePanel(ctx)));
  stack.appendChild(section('MOVE / HOME', dynamicSection(ctx, (st) => (st.busy ? 'busy' : 'free'), movePanel)));
  stack.appendChild(section('AMS', dynamicSection(ctx, amsShapeKey, amsPanel)));
  stack.appendChild(section('FANS', fansPanel(ctx)));
  stack.appendChild(section('SPEED', speedPanel(ctx)));
  stack.appendChild(section('ADVANCED', gcodePanel(ctx)));

  let unsub = null;
  function render() {
    const st = ctx.state();
    if (local.stopping && st.idle) local.stopping = false;
    titleEl.textContent = st.enabled ? 'Controls' : 'Disconnected — controls disabled';
    stack.style.opacity = st.enabled ? '' : '.5';
    stack.style.pointerEvents = st.enabled ? '' : 'none';
    for (const fn of updaters) fn(st);
  }
  ctx.render = render;

  sheetHandle = mountSheet(el('div', {}, [header, errSlot, stack]), {
    drawer: true,
    onClose: () => { if (unsub) { unsub(); unsub = null; } if (opts.onClose) opts.onClose(); },
  });
  render();
  unsub = app.store.subscribe(render);
  return { close: () => sheetHandle.close() };
}

/** Everything the panels need from the store, computed once per update. */
function sheetState(app, pid) {
  const vm = app.store.viewModel(pid);
  const snap = app.store.current(pid);
  const live = printerStateLabels(snap, vm.connected).live;
  const active = vm.phase === 'printing' || vm.phase === 'preparing' || vm.phase === 'paused';
  const paused = vm.phase === 'paused';
  return {
    vm,
    job: (snap && snap.job) || {},
    raw: (snap && snap._raw) || {},
    enabled: live && vm.phase !== 'unknown',
    active,
    paused,
    busy: active && !paused,   // Orca: is_in_printing() && !can_resume()
    idle: !active,
  };
}

/**
 * A section whose content is rebuilt only when keyOf(state) changes. Its
 * updaters run on every store change, like the static panels'.
 */
function dynamicSection(ctx, keyOf, build) {
  const host = el('div');
  let key = null;
  let own = [];
  ctx.update((st) => {
    const k = keyOf(st);
    if (k !== key) {
      key = k;
      own = [];
      clear(host);
      host.appendChild(build({ ...ctx, update: (fn) => own.push(fn) }, st));
    }
    for (const fn of own) fn(st);
  });
  return host;
}

// ── small helpers ────────────────────────────────────────────────────────────
function section(title, ...children) {
  return el('div', { class: 'section', style: { marginBottom: 'var(--space-4)' } }, [
    el('div', { class: 't-section', style: { marginBottom: 'var(--space-2)' }, text: title }),
    ...children,
  ]);
}

/** Run a command; toast on success, show the error card on failure. */
async function run(ctx, path, body, okMsg) {
  ctx.report(null);
  const r = await ctx.post(path, body);
  if (r.ok) { if (okMsg) toast(okMsg); return true; }
  ctx.report(r);
  return false;
}

/** The printer-reported range maximum (e.g. nozzle_temp_range), else fallback. */
function reportedMax(raw, key, fallback) {
  const range = raw[key];
  const top = Array.isArray(range) ? Number(range[1]) : NaN;
  return Number.isInteger(top) && top > 0 ? top : fallback;
}

function clampInt(v, lo, hi) {
  let n = parseInt(v, 10);
  if (Number.isNaN(n)) n = lo;
  return Math.max(lo, Math.min(hi, n));
}

function isFocused(node) {
  return typeof document !== 'undefined' && document.activeElement === node;
}

// ── PRINT (§4 — pause/resume/stop) ───────────────────────────────────────────
function printPanel(ctx) {
  const { local } = ctx;
  const pauseResumeBtn = el('button', {
    class: 'btn',
    onClick: () => (ctx.state().paused
      ? run(ctx, '/print/resume', undefined, 'Resuming print')
      : run(ctx, '/print/pause', undefined, 'Pausing print')),
  });

  const stopBtn = el('button', {
    class: 'btn btn--danger',
    onClick: async () => {
      const vm = ctx.state().vm;
      const ok = await confirmSheet({
        title: 'Stop this print?',
        body: (vm.subtaskName ? vm.subtaskName + ' · ' : '')
          + (vm.showPercent && vm.percent != null ? vm.percent + '% done\n' : '')
          + 'This can’t be undone. The print ends and the toolhead parks.',
        confirmLabel: 'Stop print',
        cancelLabel: 'Cancel',
        danger: true,
      });
      if (!ok) return;
      // command_accepted ≠ print_stopped: show "Stopping…" until the phase
      // leaves the active print.
      if (await run(ctx, '/print/stop', undefined, 'Stopping…')) {
        local.stopping = true;
        ctx.render();
      }
    },
  });

  // Orca's part-skip button: shown when the printer reports support (fun
  // bit 49, or an s_obj list on a printer that sends no fun), usable while RUNNING or PAUSE, labelled with the skipped count.
  const skipBtn = el('button', {
    class: 'btn', onClick: () => skipObjects.open(ctx.app, ctx.pid),
  });

  const hint = el('div', { class: 'field__hint', text: 'No active print.' });

  ctx.update((st) => {
    const skipped = (st.job.skipped_objects || []).length;
    skipBtn.hidden = !st.job.part_skip_supported;
    skipBtn.textContent = skipped ? `Skip objects (${skipped})` : 'Skip objects';
    skipBtn.disabled = !(st.enabled && SKIP_STATES.has(st.raw.gcode_state));
    pauseResumeBtn.textContent = st.paused ? 'Resume' : 'Pause';
    pauseResumeBtn.disabled = !(st.enabled && st.active);
    stopBtn.textContent = local.stopping ? 'Stopping…' : 'Stop';
    stopBtn.disabled = !(st.enabled && st.active) || local.stopping;
    hint.hidden = !(st.enabled && st.idle);
  });

  return el('div', {}, [
    el('div', { class: 't-section', style: { marginBottom: 'var(--space-2)' }, text: 'PRINT' }),
    el('div', { class: 'row gap-2' }, [pauseResumeBtn, stopBtn, skipBtn]),
    hint,
  ]);
}

// ── Light (instant toggle, no confirm) ───────────────────────────────────────
function lightPanel(ctx) {
  const btn = el('button', {
    class: 'btn', style: { minWidth: '88px' },
    onClick: () => {
      const on = ctx.state().vm.lightOn;
      run(ctx, '/light', { on: !on }, on ? 'Light off' : 'Light on');
    },
  });
  ctx.update((st) => {
    btn.textContent = st.vm.lightOn ? 'On' : 'Off';
    btn.className = 'btn ' + (st.vm.lightOn ? 'btn--primary' : 'btn--ghost');
  });
  return el('div', {}, [
    el('div', { class: 't-section', style: { marginBottom: 'var(--space-2)' }, text: 'CHAMBER' }),
    el('div', { class: 'row row--between' }, [el('div', { text: 'Light' }), btn]),
  ]);
}

// ── Temperature (§4.1) — actual→target, [-][field][+][Set], presets+undo ─────
const TEMP_PRESETS = {
  nozzle: [0, 200, 220, 250],
  bed: [0, 55, 60, 100],
};

function temperaturePanel(ctx) {
  return el('div', {}, [heaterRow(ctx, 'nozzle', 'Nozzle'), heaterRow(ctx, 'bed', 'Bed')]);
}

function heaterRow(ctx, key, label) {
  let hold = 0;
  const maxC = () => (key === 'nozzle'
    ? reportedMax(ctx.state().raw, 'nozzle_temp_range', NOZZLE_MAX_C)
    : reportedMax(ctx.state().raw, 'bed_temp_range', BED_MAX_C));

  const curEl = el('span', { class: 'temprow__val live-num' });
  const tgtEl = el('span', { class: 'temprow__target' });
  const field = el('input', {
    class: 'input', type: 'number', inputmode: 'numeric', 'aria-label': `${label} target`,
    style: { width: '72px', textAlign: 'center', minHeight: '40px', padding: '6px 8px' },
  });

  async function set(celsius, okMsg) {
    hold = HOLD_UPDATES;
    field.value = String(celsius);
    return run(ctx, '/temperature', { [key]: celsius }, okMsg);
  }
  const nudge = (delta) => { field.value = String(clampInt(Number(field.value) + delta, 0, maxC())); };

  const presetRow = el('div', { class: 'row gap-2', style: { flexWrap: 'wrap', marginTop: 'var(--space-2)' } },
    TEMP_PRESETS[key].map((p) => el('button', {
      class: 'btn btn--sm btn--ghost', text: `${p}°`,
      onClick: async () => {
        const t = ctx.state().vm.temps[key] || {};
        const prev = typeof t.target_c === 'number' ? Math.round(t.target_c) : 0;
        if (!(await set(p))) return;
        undoToast(`Setting ${label} to ${p} °C`, () => {
          set(prev).then((ok) => { if (ok) toast(`Reverted ${label}`); });
        });
      },
    })));

  ctx.update((st) => {
    const t = st.vm.temps[key] || {};
    const cur = typeof t.current_c === 'number' ? Math.round(t.current_c) : null;
    const tgt = typeof t.target_c === 'number' ? Math.round(t.target_c) : null;
    curEl.textContent = cur != null ? `${cur} °C` : '— °C';
    tgtEl.textContent = tgt != null ? ` → ${tgt} °C` : '';
    if (hold > 0) { hold -= 1; return; }
    if (tgt != null && !isFocused(field)) field.value = String(tgt);
  });

  return el('div', { style: { marginBottom: 'var(--space-3)' } }, [
    el('div', { class: 'row gap-2' }, [
      el('span', { class: 'temprow__label', text: label }),
      el('div', { class: 'temprow__label', style: { width: 'auto', flex: '1 1 auto' } }, [curEl, tgtEl]),
    ]),
    el('div', { class: 'row gap-2', style: { marginTop: 'var(--space-1)' } }, [
      el('button', { class: 'btn btn--sm btn--ghost', text: '−', 'aria-label': `Lower ${label}`, onClick: () => nudge(-5) }),
      field,
      el('button', { class: 'btn btn--sm btn--ghost', text: '+', 'aria-label': `Raise ${label}`, onClick: () => nudge(5) }),
      el('button', {
        class: 'btn btn--sm btn--primary', text: 'Set',
        onClick: () => {
          if (Number.isNaN(parseInt(field.value, 10))) return;   // nothing typed
          const c = clampInt(field.value, 0, maxC());   // Orca clamps to the max
          set(c, `Set ${label} to ${c} °C`);
        },
      }),
    ]),
    presetRow,
  ]);
}

// ── Move / Home (§4.2) — Orca's 1/10 mm jog pad; refused while printing ──────
function movePanel(ctx, st) {
  const { local } = ctx;
  if (st.busy) {
    return el('div', { class: 'field__hint', text: BUSY_MESSAGE });
  }

  function jog(axis, sign) {
    const dist = sign * local.step;
    run(ctx, '/move', { axis, distance_mm: dist, feed_mm_min: JOG_FEED[axis] },
      `Jog ${axis}${sign > 0 ? '+' : '−'} ${local.step} mm`);
  }

  const homeBtn = el('button', {
    class: 'btn btn--block', text: 'Home all axes',
    onClick: async () => {
      const ok = await confirmSheet({
        title: 'Home all axes?',
        body: 'The toolhead and bed will move to their home positions.',
        confirmLabel: 'Home all',
      });
      if (ok) run(ctx, '/home', undefined, 'Homing…');
    },
  });

  const chips = JOG_STEPS.map((s) => el('button', {
    class: 'btn btn--sm', text: `${s} mm`,
    onClick: () => { local.step = s; ctx.render(); },
  }));
  ctx.update(() => {
    chips.forEach((chip, i) => {
      chip.className = 'btn btn--sm ' + (JOG_STEPS[i] === local.step ? 'btn--primary' : 'btn--ghost');
    });
  });

  const jogBtn = (axis, sign, label) => el('button', { class: 'btn', text: label, onClick: () => jog(axis, sign) });
  const spacer = () => el('div');
  const grid = el('div', {
    style: {
      display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 'var(--space-2)',
      marginTop: 'var(--space-3)',
    },
  }, [
    spacer(), jogBtn('Y', 1, 'Y+'), spacer(),
    jogBtn('X', -1, 'X−'), jogBtn('Z', 1, 'Z+'), jogBtn('X', 1, 'X+'),
    spacer(), jogBtn('Y', -1, 'Y−'), spacer(),
    jogBtn('Z', -1, 'Z−'),
  ]);

  return el('div', {}, [
    homeBtn,
    el('div', { class: 'field__hint', style: { marginTop: 'var(--space-3)' }, text: 'Step size' }),
    el('div', { class: 'row gap-2', style: { flexWrap: 'wrap' } }, chips),
    grid,
  ]);
}

// ── AMS (§4.3) — Slot 1..4, change/unload/resume, rescan hold ────────────────
/** Filament moves allowed by Orca's rules: 'free', 'paused' (external only) or 'busy'. */
function filamentMode(st) {
  return st.busy ? 'busy' : st.paused ? 'paused' : 'free';
}

function amsShapeKey(st) {
  const ams = st.vm.ams || {};
  const slots = (ams.displaySlots || []).map((s) =>
    [s.physical_slot, s.type, s.color, s.remaining_pct, s.state].join('|'));
  return JSON.stringify([!!ams.present, !!ams.rescanning, ams.engaged_slot ?? null, filamentMode(st), slots]);
}

function amsPanel(ctx, st) {
  const ams = st.vm.ams || { present: false, displaySlots: [] };
  if (!ams.present) {
    return el('div', { class: 'field__hint', text: 'No AMS detected.' });
  }
  const slots = Array.isArray(ams.displaySlots) ? ams.displaySlots : [];
  const mode = filamentMode(st);
  const grid = el('div', { class: 'ams-grid' + (ams.rescanning ? ' is-rescan' : '') },
    slots.length
      ? slots.map((s) => slotCell(s))
      : [el('div', { class: 'field__hint', text: 'AMS re-scanning…' })]);

  // Orca: AMS slots cannot be loaded while a print runs or is paused; a paused
  // print may unload only the external spool.
  const changeBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Change to other tray',
    disabled: mode !== 'free',
    onClick: () => openChangePicker(ctx, slots, ams.engaged_slot),
  });
  const unloadBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Unload filament',
    disabled: mode === 'busy' || (mode === 'paused' && ams.engaged_slot !== 'external'),
    onClick: async () => {
      const ok = await confirmSheet({
        title: 'Unload filament?',
        body: 'The nozzle heats up to release the filament — it will get hot.',
        confirmLabel: 'Unload',
      });
      if (ok) run(ctx, '/filament/unload', undefined, 'Unloading…');
    },
  });
  const resumeBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Resume AMS',
    onClick: () => run(ctx, '/ams/control', { action: 'resume' }, 'AMS resumed'),
  });

  const hint = mode === 'busy' ? BUSY_MESSAGE : mode === 'paused' ? PAUSED_FILAMENT_MESSAGE : null;
  return el('div', {}, [
    grid,
    ams.rescanning ? el('div', { class: 'field__hint', text: 'AMS re-scanning…' }) : null,
    el('div', { class: 'row gap-2', style: { flexWrap: 'wrap', marginTop: 'var(--space-3)' } },
      [changeBtn, unloadBtn, resumeBtn]),
    hint ? el('div', { class: 'field__hint', text: hint }) : null,
  ]);
}

function slotCell(s) {
  const color = (s && typeof s.color === 'string' && /^#?[0-9a-fA-F]{3,8}$/.test(s.color))
    ? (s.color[0] === '#' ? s.color : '#' + s.color) : 'transparent';
  const remaining = typeof s.remaining_pct === 'number' ? `${s.remaining_pct}%` : '';
  return el('div', { class: 'ams-slot' }, [
    el('div', { class: 't-caption', text: slotLabel(s.physical_slot) }),
    el('div', { class: 'amsline', style: { justifyContent: 'center', marginTop: '4px' } }, [
      el('span', { class: 'swatch', style: { background: color } }),
      el('span', { class: 't-caption', text: s.type || '—' }),
    ]),
    remaining ? el('div', { class: 't-caption', text: remaining }) : null,
  ]);
}

function openChangePicker(ctx, slots, engaged) {
  // user picks a PHYSICAL slot (1..4); /ams/change wants 0-based target_tray.
  // Orca refuses an empty slot and the slot already loaded.
  const list = slots.length ? slots : [1, 2, 3, 4].map((n) => ({ physical_slot: n }));
  const buttons = list.map((s) => {
    const physical = s.physical_slot;
    const why = s.state === 'empty' ? ' · empty'
      : physical === engaged ? ' · loaded' : '';
    return el('button', {
      class: 'btn btn--block btn--ghost',
      text: `${slotLabel(physical)}${s.type ? ' · ' + s.type : ''}${why}`,
      disabled: !!why,
      onClick: () => {
        picker.close();
        run(ctx, '/ams/change', { target_tray: Number(physical) - 1, cur_temp: 220, tar_temp: 220 },
          `Changing to ${slotLabel(physical)}…`);
      },
    });
  });
  const picker = mountSheet(el('div', {}, [
    el('div', { class: 'sheet__title', text: 'Change to which tray?' }),
    el('div', { class: 'sheet__actions' }, buttons),
  ]));
}

// ── Fans (§4.4) — three sliders, no confirm ──────────────────────────────────
const FANS = [
  { key: 'part', label: 'Part', src: 'part_fan' },
  { key: 'aux', label: 'Aux', src: 'aux_fan' },
  { key: 'chamber', label: 'Chamber', src: 'chamber_fan' },
];

function fansPanel(ctx) {
  return el('div', {}, FANS.map((f) => fanRow(ctx, f)));
}

function fanRow(ctx, f) {
  let hold = 0;
  const valLabel = el('span', { class: 'temprow__target num', style: { width: '40px', textAlign: 'right' } });
  const slider = el('input', {
    type: 'range', min: '0', max: '100', step: '1', 'aria-label': `${f.label} fan`,
    style: { flex: '1 1 auto' },
    onInput: (e) => { hold = HOLD_UPDATES; valLabel.textContent = `${e.target.value}%`; },
    onChange: (e) => {
      hold = HOLD_UPDATES;
      const percent = clampInt(e.target.value, 0, 100);
      run(ctx, '/fan', { part: f.key, percent }, `${f.label} fan ${percent}%`);
    },
  });
  ctx.update((st) => {
    if (hold > 0) { hold -= 1; return; }
    if (isFocused(slider)) return;
    const c = st.vm.cooling[f.src];
    const cur = c && typeof c.percent === 'number' ? c.percent : 0;
    slider.value = String(cur);
    valLabel.textContent = `${cur}%`;
  });
  return el('div', { class: 'row gap-3', style: { marginBottom: 'var(--space-2)' } }, [
    el('span', { class: 'temprow__label', style: { width: '72px' }, text: f.label }),
    slider, valLabel,
  ]);
}

// ── Speed (§4.5) — 4 segments; selection is the printer's spd_lvl ────────────
const SPEED_LABELS = { 1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous' };

function speedPanel(ctx) {
  let hold = 0;
  let chosen = null;
  const segs = [1, 2, 3, 4].map((level) => el('button', {
    class: 'btn btn--sm', style: { flex: '1 1 0' }, text: SPEED_LABELS[level],
    onClick: async () => {
      hold = HOLD_UPDATES;
      chosen = level;
      ctx.render();
      await run(ctx, '/speed', { level }, `Speed: ${SPEED_LABELS[level]}`);
    },
  }));
  ctx.update((st) => {
    if (hold > 0) hold -= 1;
    else chosen = Number(st.raw.spd_lvl) || null;
    segs.forEach((seg, i) => {
      seg.className = 'btn btn--sm ' + (chosen === i + 1 ? 'btn--primary' : 'btn--ghost');
    });
  });
  return el('div', { class: 'row gap-2' }, segs);
}

// ── Raw G-code console (§4.6) — gated; always confirms ───────────────────────
function gcodePanel(ctx) {
  const { local } = ctx;
  const input = el('input', {
    class: 'input input--mono', type: 'text', placeholder: 'G28',
    'aria-label': 'Raw G-code line',
  });
  const sub = el('div', { class: 'listrow__sub' });
  const sendBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Send',
    onClick: async () => {
      const line = (input.value || '').trim();
      if (!line) return;
      const ok = await confirmSheet({ title: 'Send this G-code?', body: line, confirmLabel: 'Send', danger: true });
      if (!ok) return;
      ctx.report(null);
      const r = await ctx.post('/gcode/raw', { line });
      if (r.ok) { toast('Sent'); input.value = ''; return; }
      if (r.status === 403 && r.error === 'raw_gcode_disabled') {
        local.gcodeDisabled = true;   // lock the console read-only
        ctx.render();
      }
      ctx.report(r);
    },
  });
  ctx.update(() => {
    input.disabled = local.gcodeDisabled;
    sendBtn.disabled = local.gcodeDisabled;
    sub.textContent = local.gcodeDisabled
      ? 'Disabled on this bridge — set BRIDGE_ENABLE_RAW_GCODE in bridge.env and restart.'
      : 'Forwarded verbatim. Each send confirms the literal line.';
  });
  return el('div', {}, [
    el('div', { class: 'listrow', style: { cursor: 'default' } }, [
      el('div', { class: 'listrow__main' }, [el('div', { text: 'Send raw G-code' }), sub]),
    ]),
    el('div', { class: 'row gap-2', style: { marginTop: 'var(--space-2)' } }, [input, sendBtn]),
  ]);
}
