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
// Load-bearing rules (frodo §9):
//   - Everything is gated on WS-connected && phase != 'unknown'. Disconnected →
//     the whole panel dims with "Disconnected — controls disabled". Idle →
//     Pause/Resume/Stop disabled with "no active print".
//   - Stop is the only confirming PRINT action; after confirm it shows
//     "Stopping…" until WS job_state_change → canceled (command-accepted ≠
//     stopped).
//   - AMS labels are "Slot 1..4" (physical_slot); /ams/change target_tray is
//     0-based (we subtract 1 from the picked physical slot). The §6.1.1 RFID
//     re-scan transient holds the last-known slots (the store's viewModel
//     already computes displaySlots/rescanning).
//   - Speed renders the labels the response echoes when present; falls back to
//     the canonical 1..4 labels otherwise (the bridge does not echo them today).
//   - Raw G-code always confirms with the literal line; a 403 raw_gcode_disabled
//     renders as the clear enable hint, and the console stays read-only until
//     enabled.
//   - Bridge `message` + `remediation_hint` render VERBATIM via ui.errorCard.

import {
  el, clear, mountSheet, confirmSheet, toast, undoToast, errorCard,
  pressHold, slotLabel,
} from './ui.js';

// ── public: the standalone #/controls route ─────────────────────────────────
/**
 * @param {HTMLElement} root
 * @param {any} app
 * @returns {() => void}
 */
export function mount(root, app) {
  const pid = app.currentPrinterId();

  // The route owns the full viewport but the controls live in a sheet/drawer.
  // Render a thin backdrop into root so there's a surface behind the sheet, and
  // mount the sheet immediately. Dismissing it returns to the dashboard.
  if (pid) {
    root.appendChild(el('div', { class: 'placeholder' }, [
      el('div', { text: 'Controls' }),
      el('div', { class: 't-caption mt-2', text: 'Close to return to the dashboard.' }),
    ]));
  }

  if (!pid) {
    clear(root);
    root.appendChild(el('div', { class: 'placeholder' }, [
      el('div', { text: 'No printer selected.' }),
      el('button', {
        class: 'btn btn--ghost mt-3', text: 'Go to Settings',
        onClick: () => app.navigate('#/settings'),
      }),
    ]));
    return function unmount() {};
  }

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
 * Open the controls as an overlay sheet over the current screen (dashboard).
 * Does NOT open a WS — the dashboard already has one live; the sheet reads the
 * store. Returns a handle with .close().
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

// ── the shared sheet builder ─────────────────────────────────────────────────
/**
 * Mount the controls sheet and wire it to the store. Re-renders the enablement
 * shell on every store change so connect/disconnect and phase transitions dim
 * the panel live.
 * @param {any} app
 * @param {string} pid
 * @param {object} [opts] @param {()=>void} [opts.onClose]
 * @returns {{close:()=>void}}
 */
function openControlsSheet(app, pid, opts = {}) {
  const body = el('div');
  let unsub = null;
  const sheetHandle = mountSheet(body, {
    drawer: true,
    onClose: () => { if (unsub) { unsub(); unsub = null; } if (opts.onClose) opts.onClose(); },
  });

  // local UI state that must survive a store-driven re-render
  const local = {
    stopping: false,        // Stop tapped, awaiting WS canceled
    tempDraft: {},          // { nozzle, bed } pending typed values
  };

  function closeSheet() { sheetHandle.close(); }

  function render() {
    clear(body);
    body.appendChild(buildBody(app, pid, local, render, closeSheet));
  }
  render();
  unsub = app.store.subscribe(render);

  // The store subscription drives a re-render on every WS snapshot/delta, so a
  // job_state_change → canceled (which flips phase to idle/failed) clears the
  // transient "Stopping…" state inside buildBody.
  return { close: closeSheet };
}

// ── the body (rebuilt on every store change) ─────────────────────────────────
function buildBody(app, pid, local, rerender, closeSheet) {
  const vm = app.store.viewModel(pid);
  const connected = !!vm.connected;
  const phaseKnown = vm.phase && vm.phase !== 'unknown';
  const enabled = connected && phaseKnown;
  const idle = vm.phase === 'idle' || vm.phase === 'completed' || vm.phase === 'failed';
  const active = vm.phase === 'printing' || vm.phase === 'preparing' || vm.phase === 'paused';

  // clear a stale "Stopping…" once the print is actually canceled/idle
  if (local.stopping && (idle)) local.stopping = false;

  const header = el('div', { class: 'row row--between mb-4' }, [
    el('div', { class: 'sheet__title', style: { margin: '0' },
      text: enabled ? 'Controls' : 'Disconnected — controls disabled' }),
    el('button', {
      class: 'input-eye', 'aria-label': 'Close controls', text: '✕',
      style: { position: 'static', width: '36px', height: '36px' },
      // Close the sheet directly. In the standalone #/controls route the
      // sheet's onClose navigates back to '#/'; in overlay mode it just
      // tears the sheet down. Either way the scrim tap does the same.
      onClick: () => closeSheet(),
    }),
  ]);

  const stack = el('div', { class: 'stack', style: enabled ? {} : { opacity: '.5', pointerEvents: 'none' } }, [
    printPanel(app, pid, vm, { idle, active, enabled }, local, rerender),
    lightPanel(app, pid, vm, enabled),
    section('TEMPERATURE', temperaturePanel(app, pid, vm, local, rerender)),
    section('MOVE / HOME', movePanel(app, pid, vm, { idle }, rerender)),
    section('AMS', amsPanel(app, pid, vm, rerender)),
    section('FANS', fansPanel(app, pid, vm)),
    section('SPEED', speedPanel(app, pid, vm, rerender)),
    section('ADVANCED', gcodePanel(app, pid, rerender)),
  ]);

  return el('div', {}, [header, stack]);
}

// ── small layout helpers ─────────────────────────────────────────────────────
function section(title, ...children) {
  return el('div', { class: 'section', style: { marginBottom: 'var(--space-4)' } }, [
    el('div', { class: 't-section', style: { marginBottom: 'var(--space-2)' }, text: title }),
    ...children,
  ]);
}

/** A place to mount an inline error card under a panel; replaces prior content. */
function inlineSlot() {
  return el('div', { class: 'controls-err' });
}

function showInline(slot, node) {
  clear(slot);
  if (node) slot.appendChild(node);
}

// ── PRINT (§4 — pause/resume/stop) ───────────────────────────────────────────
function printPanel(app, pid, vm, st, local, rerender) {
  const errSlot = inlineSlot();
  const paused = vm.phase === 'paused';
  const canPauseResume = st.enabled && st.active;
  const canStop = st.enabled && st.active;

  async function fire(path, okMsg) {
    showInline(errSlot, null);
    const r = await app.api.postJson(`/printers/${pid}${path}`);
    if (r.ok) { toast(okMsg); return; }
    showInline(errSlot, errorCard(r));
  }

  const pauseResumeBtn = el('button', {
    class: 'btn',
    text: paused ? 'Resume' : 'Pause',
    disabled: !canPauseResume,
    onClick: () => paused
      ? fire('/print/resume', 'Resuming print')
      : fire('/print/pause', 'Pausing print'),
  });

  const stopBtn = el('button', {
    class: 'btn btn--danger',
    text: local.stopping ? 'Stopping…' : 'Stop',
    disabled: !canStop || local.stopping,
    onClick: async () => {
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
      showInline(errSlot, null);
      const r = await app.api.postJson(`/printers/${pid}/print/stop`);
      if (r.ok) {
        // command_accepted ≠ print_stopped: show "Stopping…" until the WS
        // reports job_state_change → canceled (the store flips phase).
        local.stopping = true;
        toast('Stopping…');
        rerender();
      } else {
        showInline(errSlot, errorCard(r));
      }
    },
  });

  const hint = (st.enabled && st.idle)
    ? el('div', { class: 'field__hint', text: 'No active print.' })
    : null;

  return el('div', {}, [
    el('div', { class: 't-section', style: { marginBottom: 'var(--space-2)' }, text: 'PRINT' }),
    el('div', { class: 'row gap-2' }, [pauseResumeBtn, stopBtn]),
    hint,
    errSlot,
  ]);
}

// ── Light (instant toggle, no confirm) ───────────────────────────────────────
function lightPanel(app, pid, vm, enabled) {
  const errSlot = inlineSlot();
  const on = !!vm.lightOn;
  const btn = el('button', {
    class: 'btn ' + (on ? 'btn--primary' : 'btn--ghost'),
    disabled: !enabled,
    text: on ? 'On' : 'Off',
    style: { minWidth: '88px' },
    onClick: async () => {
      showInline(errSlot, null);
      const r = await app.api.postJson(`/printers/${pid}/light`, { on: !on });
      if (r.ok) toast(on ? 'Light off' : 'Light on');
      else showInline(errSlot, errorCard(r));
    },
  });
  return el('div', {}, [
    el('div', { class: 't-section', style: { marginBottom: 'var(--space-2)' }, text: 'CHAMBER' }),
    el('div', { class: 'row row--between' }, [
      el('div', { text: 'Light' }), btn,
    ]),
    errSlot,
  ]);
}

// ── Temperature (§4.1) — actual→target, [-][field][+][Set], presets+undo ─────
const TEMP_PRESETS = {
  nozzle: [0, 200, 220, 250],
  bed: [0, 55, 60, 100],
};

function temperaturePanel(app, pid, vm, local, rerender) {
  const errSlot = inlineSlot();

  async function post(patch, okMsg) {
    showInline(errSlot, null);
    const r = await app.api.postJson(`/printers/${pid}/temperature`, patch);
    if (r.ok) { if (okMsg) toast(okMsg); return true; }
    // 422 range message renders inline VERBATIM.
    showInline(errSlot, errorCard(r));
    return false;
  }

  function heaterRow(key, label) {
    const t = (vm.temps && vm.temps[key]) || {};
    const cur = typeof t.current_c === 'number' ? Math.round(t.current_c) : null;
    const tgt = typeof t.target_c === 'number' ? Math.round(t.target_c) : null;

    // typed draft: seed from target if not yet edited
    if (local.tempDraft[key] == null) local.tempDraft[key] = tgt != null ? tgt : 0;

    const readout = el('div', { class: 'temprow__label', style: { width: 'auto', flex: '1 1 auto' } }, [
      el('span', { class: 'temprow__val live-num', text: cur != null ? `${cur} °C` : '— °C' }),
      el('span', { class: 'temprow__target', text: tgt != null ? ` → ${tgt} °C` : '' }),
    ]);

    const field = el('input', {
      class: 'input', type: 'number', inputmode: 'numeric',
      value: String(local.tempDraft[key]),
      style: { width: '72px', textAlign: 'center', minHeight: '40px', padding: '6px 8px' },
      onInput: (e) => { local.tempDraft[key] = clampInt(e.target.value, 0, 300); },
    });
    const dec = el('button', {
      class: 'btn btn--sm btn--ghost', text: '−', 'aria-label': `Lower ${label}`,
      onClick: () => { local.tempDraft[key] = clampInt(local.tempDraft[key] - 5, 0, 300); field.value = String(local.tempDraft[key]); },
    });
    const inc = el('button', {
      class: 'btn btn--sm btn--ghost', text: '+', 'aria-label': `Raise ${label}`,
      onClick: () => { local.tempDraft[key] = clampInt(local.tempDraft[key] + 5, 0, 300); field.value = String(local.tempDraft[key]); },
    });
    const setBtn = el('button', {
      class: 'btn btn--sm btn--primary', text: 'Set',
      onClick: () => post({ [key]: clampInt(field.value, 0, 300) }, `Set ${label} to ${clampInt(field.value, 0, 300)} °C`),
    });

    const presetRow = el('div', { class: 'row gap-2', style: { flexWrap: 'wrap', marginTop: 'var(--space-2)' } },
      TEMP_PRESETS[key].map((p) => el('button', {
        class: 'btn btn--sm btn--ghost', text: `${p}°`,
        onClick: async () => {
          const prev = tgt; // previous target for Undo
          const ok = await post({ [key]: p });
          if (!ok) return;
          local.tempDraft[key] = p; field.value = String(p);
          undoToast(`Setting ${label} to ${p} °C`, () => {
            app.api.postJson(`/printers/${pid}/temperature`, { [key]: prev != null ? prev : 0 })
              .then((r) => { if (r.ok) toast(`Reverted ${label}`); });
          });
          rerender();
        },
      })));

    return el('div', { style: { marginBottom: 'var(--space-3)' } }, [
      el('div', { class: 'row gap-2' }, [
        el('span', { class: 'temprow__label', text: label }),
        readout,
      ]),
      el('div', { class: 'row gap-2', style: { marginTop: 'var(--space-1)' } }, [dec, field, inc, setBtn]),
      presetRow,
    ]);
  }

  return el('div', {}, [
    heaterRow('nozzle', 'Nozzle'),
    heaterRow('bed', 'Bed'),
    errSlot,
  ]);
}

// ── Move / Home (§4.2) — idle-only, jog pad, 100mm press-and-hold ────────────
const JOG_STEPS = [0.1, 1, 10, 100]; // §4.2 chips; default 1. Bridge whitelists
                                     // its own set and 422s the rest — rendered
                                     // inline via remediation_hint.
const PRESS_HOLD_STEP = 100;

// per-mount jog state lives on a module-scoped WeakMap keyed by the panel node
// would over-engineer; keep it simple with a closure cell instead.
function movePanel(app, pid, vm, st, rerender) {
  const errSlot = inlineSlot();
  if (!st.idle) {
    return el('div', {}, [
      el('div', { class: 'field__hint', text: 'Can’t move while printing.' }),
    ]);
  }

  // jog step selection persists across the panel's own re-render via a holder
  const stepHolder = { step: movePanel._step != null ? movePanel._step : 1 };

  async function jog(axis, sign) {
    showInline(errSlot, null);
    const dist = sign * stepHolder.step;
    const r = await app.api.postJson(`/printers/${pid}/move`, {
      axis, distance_mm: dist, feed_mm_min: 600,
    });
    if (r.ok) toast(`Jog ${axis}${sign > 0 ? '+' : '−'} ${stepHolder.step} mm`);
    else showInline(errSlot, errorCard(r)); // 409 jog_not_homed / out_of_envelope, 422 step
  }

  const homeBtn = el('button', {
    class: 'btn btn--block', text: 'Home all axes',
    onClick: async () => {
      const ok = await confirmSheet({
        title: 'Home all axes?',
        body: 'The toolhead and bed will move to their home positions.',
        confirmLabel: 'Home all',
      });
      if (!ok) return;
      showInline(errSlot, null);
      const r = await app.api.postJson(`/printers/${pid}/home`);
      if (r.ok) toast('Homing…');
      else showInline(errSlot, errorCard(r));
    },
  });

  // step chips
  const chips = el('div', { class: 'row gap-2', style: { flexWrap: 'wrap' } },
    JOG_STEPS.map((s) => {
      const isSel = stepHolder.step === s;
      return el('button', {
        class: 'btn btn--sm ' + (isSel ? 'btn--primary' : 'btn--ghost'),
        text: `${s} mm`,
        onClick: () => { movePanel._step = s; stepHolder.step = s; rerender(); },
      });
    }));

  // jog button: ordinary tap unless the step is the press-and-hold guard step.
  function jogBtn(axis, sign, label) {
    const btn = el('button', { class: 'btn', style: { position: 'relative', overflow: 'hidden' }, text: label });
    if (stepHolder.step === PRESS_HOLD_STEP) {
      // 100 mm requires press-and-hold (a real guard against a thumb-slip).
      const ring = el('div', {
        style: {
          position: 'absolute', left: '0', bottom: '0', height: '3px', width: '0%',
          background: 'var(--amber)', transition: 'none',
        },
      });
      btn.appendChild(ring);
      btn.setAttribute('title', 'Hold to jog 100 mm');
      pressHold(btn, () => jog(axis, sign), { holdMs: 700, ring });
    } else {
      btn.addEventListener('click', () => jog(axis, sign));
    }
    return btn;
  }

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
    chips,
    stepHolder.step === PRESS_HOLD_STEP
      ? el('div', { class: 'field__hint', text: 'Hold a jog button to move 100 mm.' }) : null,
    grid,
    errSlot,
  ]);
}

function spacer() { return el('div'); }

// ── AMS (§4.3) — Slot 1..4, change/unload/reset, rescan hold ─────────────────
function amsPanel(app, pid, vm, rerender) {
  const errSlot = inlineSlot();
  const ams = vm.ams || { present: false, slots: [], displaySlots: [], rescanning: false };

  if (!ams.present) {
    return el('div', {}, [el('div', { class: 'field__hint', text: 'No AMS detected.' })]);
  }

  const slots = Array.isArray(ams.displaySlots) ? ams.displaySlots : [];
  const grid = el('div', { class: 'ams-grid' + (ams.rescanning ? ' is-rescan' : '') },
    slots.length
      ? slots.map((s) => slotCell(s))
      : [el('div', { class: 'field__hint', text: 'AMS re-scanning…' })]);

  const rescanNote = ams.rescanning
    ? el('div', { class: 'field__hint', text: 'AMS re-scanning…' }) : null;

  // Change → picker over the 4 physical slots; convert to 0-based target_tray.
  const changeBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Change to other tray',
    onClick: () => openChangePicker(app, pid, slots, errSlot),
  });
  const unloadBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Unload filament',
    onClick: async () => {
      const ok = await confirmSheet({
        title: 'Unload filament?',
        body: 'The nozzle heats up to release the filament — it will get hot.',
        confirmLabel: 'Unload',
      });
      if (!ok) return;
      showInline(errSlot, null);
      const r = await app.api.postJson(`/printers/${pid}/filament/unload`);
      if (r.ok) toast('Unloading…');
      else showInline(errSlot, errorCard(r));
    },
  });

  async function amsControl(action, okMsg) {
    showInline(errSlot, null);
    const r = await app.api.postJson(`/printers/${pid}/ams/control`, { action });
    if (r.ok) toast(okMsg);
    else showInline(errSlot, errorCard(r));
  }
  const pauseBtn = el('button', { class: 'btn btn--sm btn--ghost', text: 'Pause AMS',
    onClick: () => amsControl('pause', 'AMS paused') });
  const resumeBtn = el('button', { class: 'btn btn--sm btn--ghost', text: 'Resume AMS',
    onClick: () => amsControl('resume', 'AMS resumed') });
  const resetBtn = el('button', { class: 'btn btn--sm btn--ghost', text: 'Reset AMS',
    onClick: async () => {
      const ok = await confirmSheet({
        title: 'Reset the AMS?',
        body: 'The AMS re-homes its feed mechanism. Do this only if a slot is stuck.',
        confirmLabel: 'Reset',
      });
      if (ok) amsControl('reset', 'Resetting AMS…');
    } });

  return el('div', {}, [
    grid,
    rescanNote,
    el('div', { class: 'row gap-2', style: { flexWrap: 'wrap', marginTop: 'var(--space-3)' } },
      [changeBtn, unloadBtn]),
    el('div', { class: 'row gap-2', style: { flexWrap: 'wrap', marginTop: 'var(--space-2)' } },
      [pauseBtn, resumeBtn, resetBtn]),
    errSlot,
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

function openChangePicker(app, pid, slots, errSlot) {
  // user picks a PHYSICAL slot (1..4); /ams/change wants 0-based target_tray.
  const buttons = (slots.length ? slots : [1, 2, 3, 4].map((n) => ({ physical_slot: n }))).map((s) => {
    const physical = s.physical_slot;
    return el('button', {
      class: 'btn btn--block btn--ghost',
      text: `${slotLabel(physical)}${s.type ? ' · ' + s.type : ''}`,
      onClick: async () => {
        picker.close();
        showInline(errSlot, null);
        const target_tray = Number(physical) - 1; // 1-based → 0-based
        const r = await app.api.postJson(`/printers/${pid}/ams/change`, {
          target_tray, cur_temp: 220, tar_temp: 220,
        });
        if (r.ok) toast(`Changing to ${slotLabel(physical)}…`);
        else showInline(errSlot, errorCard(r));
      },
    });
  });
  const content = el('div', {}, [
    el('div', { class: 'sheet__title', text: 'Change to which tray?' }),
    el('div', { class: 'sheet__actions' }, buttons),
  ]);
  const picker = mountSheet(content);
}

// ── Fans (§4.4) — three sliders, no confirm ──────────────────────────────────
const FANS = [
  { key: 'part', label: 'Part', src: 'part_fan' },
  { key: 'aux', label: 'Aux', src: 'aux_fan' },
  { key: 'chamber', label: 'Chamber', src: 'chamber_fan' },
];

function fansPanel(app, pid, vm) {
  const errSlot = inlineSlot();
  const cooling = vm.cooling || {};

  function fanRow(f) {
    const cur = cooling[f.src] && typeof cooling[f.src].percent === 'number'
      ? cooling[f.src].percent : 0;
    const valLabel = el('span', { class: 'temprow__target num', style: { width: '40px', textAlign: 'right' }, text: `${cur}%` });
    const slider = el('input', {
      type: 'range', min: '0', max: '100', step: '1', value: String(cur),
      style: { flex: '1 1 auto' },
      onInput: (e) => { valLabel.textContent = `${e.target.value}%`; },
      onChange: async (e) => {
        const percent = clampInt(e.target.value, 0, 100);
        const r = await app.api.postJson(`/printers/${pid}/fan`, { part: f.key, percent });
        if (r.ok) toast(`${f.label} fan ${percent}%`);
        else { showInline(errSlot, errorCard(r)); }
      },
    });
    return el('div', { class: 'row gap-3', style: { marginBottom: 'var(--space-2)' } }, [
      el('span', { class: 'temprow__label', style: { width: '72px' }, text: f.label }),
      slider, valLabel,
    ]);
  }

  return el('div', {}, [...FANS.map(fanRow), errSlot]);
}

// ── Speed (§4.5) — 4 segments; render echoed labels when present ─────────────
const SPEED_FALLBACK = { 1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous' };

function speedPanel(app, pid, vm, rerender) {
  const errSlot = inlineSlot();
  // current level, best-effort from print_params.speed_mm_s is not a level; the
  // snapshot doesn't expose the segment, so we don't pre-select one.
  const current = speedPanel._level || null;

  function seg(level, label) {
    const isSel = current === level;
    return el('button', {
      class: 'btn btn--sm ' + (isSel ? 'btn--primary' : 'btn--ghost'),
      style: { flex: '1 1 0' },
      text: label,
      onClick: async () => {
        showInline(errSlot, null);
        const r = await app.api.postJson(`/printers/${pid}/speed`, { level });
        if (r.ok) {
          speedPanel._level = level;
          speedPanel._lastData = r.data; // remember echoed labels if any
          // Render the label the response echoes if it carries one; otherwise
          // keep the fallback (the bridge does not echo labels today).
          const echoed = labelFromSpeedResponse(r.data, level);
          toast(`Speed: ${echoed || label}`);
          rerender();
        } else {
          showInline(errSlot, errorCard(r));
        }
      },
    });
  }

  // Prefer echoed labels for the segment text when the bridge provides them.
  const labels = labelsFromSpeedResponse(speedPanel._lastData) || SPEED_FALLBACK;

  return el('div', {}, [
    el('div', { class: 'row gap-2' }, [
      seg(1, labels[1]), seg(2, labels[2]), seg(3, labels[3]), seg(4, labels[4]),
    ]),
    errSlot,
  ]);
}

/** Pull a single echoed label for `level` from a /speed response, or null. */
function labelFromSpeedResponse(data, level) {
  const all = labelsFromSpeedResponse(data);
  return all ? all[level] : null;
}

/**
 * The contract (§9) says /speed "echoes labels". The current bridge response is
 * {sent, sequence_id} with no labels, so we defensively look in a few plausible
 * places and return a {1,2,3,4} map only if found.
 */
function labelsFromSpeedResponse(data) {
  if (!data || typeof data !== 'object') return null;
  const cand = data.labels || data.levels || (data.speed && data.speed.labels);
  if (!cand) return null;
  if (Array.isArray(cand) && cand.length >= 4) {
    return { 1: cand[0], 2: cand[1], 3: cand[2], 4: cand[3] };
  }
  if (typeof cand === 'object') {
    const m = {};
    for (let i = 1; i <= 4; i++) m[i] = cand[i] || cand[String(i)] || SPEED_FALLBACK[i];
    return m;
  }
  return null;
}

// ── Raw G-code console (§4.6) — gated; always confirms ───────────────────────
function gcodePanel(app, pid, rerender) {
  const errSlot = inlineSlot();
  // The console is read-only/greyed until we learn the endpoint is enabled.
  // We only learn that by trying; before any attempt, default to enabled-input
  // but the first 403 flips it to the read-only enable hint (per §4.6).
  const disabled = gcodePanel._disabled === true;

  const input = el('input', {
    class: 'input input--mono', type: 'text', placeholder: 'G28',
    'aria-label': 'Raw G-code line',
    disabled: disabled,
  });

  const sendBtn = el('button', {
    class: 'btn btn--sm btn--ghost', text: 'Send',
    disabled: disabled,
    onClick: async () => {
      const line = (input.value || '').trim();
      if (!line) return;
      // Submitting ALWAYS confirms with the literal line.
      const ok = await confirmSheet({
        title: 'Send this G-code?',
        body: line,
        confirmLabel: 'Send',
        danger: true,
      });
      if (!ok) return;
      showInline(errSlot, null);
      const r = await app.api.postJson(`/printers/${pid}/gcode/raw`, { line });
      if (r.ok) { toast('Sent'); input.value = ''; return; }
      if (r.status === 403 && r.error === 'raw_gcode_disabled') {
        // Clear, non-alarming explanation; lock the console read-only.
        gcodePanel._disabled = true;
        showInline(errSlot, errorCard(r, {
          // message + remediation_hint render verbatim from the bridge; if the
          // bridge omitted a hint, the contract's enable instruction stands.
        }));
        rerender();
        return;
      }
      showInline(errSlot, errorCard(r));
    },
  });

  return el('div', {}, [
    el('div', { class: 'listrow', style: { cursor: 'default' } }, [
      el('div', { class: 'listrow__main' }, [
        el('div', { text: 'Send raw G-code' }),
        el('div', { class: 'listrow__sub', text: disabled
          ? 'Disabled on this bridge — set BRIDGE_ENABLE_RAW_GCODE in bridge.env and restart.'
          : 'Forwarded verbatim. Each send confirms the literal line.' }),
      ]),
    ]),
    el('div', { class: 'row gap-2', style: { marginTop: 'var(--space-2)' } }, [
      input, sendBtn,
    ]),
    errSlot,
  ]);
}

// ── tiny pure helper ─────────────────────────────────────────────────────────
function clampInt(v, lo, hi) {
  let n = parseInt(v, 10);
  if (Number.isNaN(n)) n = lo;
  return Math.max(lo, Math.min(hi, n));
}
