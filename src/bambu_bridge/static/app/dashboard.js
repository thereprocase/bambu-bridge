// dashboard.js — the home screen: glance / intervene (frontend-ux-spec §3).
//
// Build-free, dependency-free. Renders entirely from store.viewModel() — the
// ONE place the phase/headline rules live — never from raw mc_percent.
//
// Responsibilities:
//   - Open the status WS (app.ws.connectStatus) for the current printer and
//     subscribe to the store; render the glanceable block (status word color-
//     coded, percent + time-left hero, layer m/n, progress bar GATED on phase:
//     preparing => indeterminate shimmer + NO percent).
//   - Header WS dot (live / reconnecting / offline / no-bridge) + state banners.
//   - Camera card (delegated to camera.js) with a "3D" affordance (new tab).
//   - Temps with heat/cool cues (§3.2 exact rules), fan row, AMS one-line
//     (Slot N, rescan-hold), recent jobs (GET /jobs?printer_id=&limit=5).
//   - The sticky morphing action bar (§3.4): while active -> Pause|Resume /
//     Stop (confirm) / Light (toast) / More; while idle -> Light / Temp / Move /
//     More.
//   - The never-show-stale rule: when not connected, grey .live-num and stamp
//     "last update Xs ago".
//   - Printer picker in the header when >1 printer.
//
// Contract: export function mount(root, app) -> unmount(). See main.js header.

import { el, clear, fmtMinutes, slotLabel, confirmSheet, toast, banner } from './ui.js';
import { mountInto as mountCamera, open3dViewer } from './camera.js';

export function mount(root, app) {
  const { store, ws, api } = app;
  const pid = app.currentPrinterId();

  // ── DOM scaffold ───────────────────────────────────────────────────────────
  const shell = el('div', { class: 'app-shell' });
  const main = el('div', { class: 'app-main' });
  shell.appendChild(main);
  root.appendChild(shell);

  // Header: printer picker (if >1) / title + WS dot.
  const titleSlot = el('div', { class: 'topbar__title', text: 'Printer' });
  const connEl = el('div', { class: 'conn conn--nobridge' }, [
    el('span', { class: 'conn__dot' }),
    el('span', { class: 'conn__label', text: '…' }),
  ]);
  const topbar = el('div', { class: 'topbar' }, [titleSlot, connEl]);
  main.appendChild(topbar);

  // Under-header banner host (reconnecting / offline / no-bridge).
  const bannerHost = el('div', { class: 'banner-host' });
  main.appendChild(bannerHost);

  if (!pid) {
    main.appendChild(el('div', { class: 'placeholder' }, [
      el('div', { text: 'No printer selected.' }),
      el('div', { class: 't-caption mt-2', text: 'Add or pick a printer in Settings.' }),
      el('button', {
        class: 'btn btn--primary mt-4',
        text: 'Open Settings',
        onClick: () => app.navigate('#/settings'),
      }),
    ]));
    return function unmount() {};
  }

  // The glance block (status, hero percent/time, layer, progress).
  const glance = el('div', { class: 'glance' });
  main.appendChild(glance);

  // Camera card.
  const cameraCard = el('div', { class: 'card' });
  main.appendChild(cameraCard);
  const camera = mountCamera(cameraCard, app, {
    onTapFrame: () => app.navigate('#/camera'),
    onOpen3d: () => open3dViewer(app, pid),
  });

  // Temps + fans + AMS card.
  const sensorCard = el('div', { class: 'card mt-4' });
  const tempsBox = el('div', { class: 'stack' });
  const fanRow = el('div', { class: 'fanrow mt-3' });
  const amsLine = el('div', { class: 'amsline mt-3' });
  sensorCard.appendChild(tempsBox);
  sensorCard.appendChild(fanRow);
  sensorCard.appendChild(amsLine);
  main.appendChild(sensorCard);

  // Recent jobs.
  const recentSection = el('div', { class: 'section mt-6' }, [
    el('div', { class: 't-section', text: 'Recent' }),
  ]);
  const recentBox = el('div', { class: 'stack' });
  recentSection.appendChild(recentBox);
  main.appendChild(recentSection);

  // Sticky action bar (built/rebuilt on each render to morph by phase).
  const actionbar = el('div', { class: 'actionbar' });
  shell.appendChild(actionbar);

  // ── live state ──────────────────────────────────────────────────────────────
  let conn = null;
  let unsub = null;
  let staleTimer = null;        // ticks "last update Xs ago" while disconnected
  let busy = false;             // an action POST is in flight (debounce taps)

  // ── connection dot + banner (§7.1) ──────────────────────────────────────────
  // We distinguish: live, reconnecting (WS retrying), offline (printer offline
  // per session.connected==false but bridge reachable — surfaced via banner),
  // and no-bridge (we never reached the bridge at all). The store only tracks
  // the WS link; "printer offline vs no bridge" is inferred from whether we ever
  // got a snapshot this session.
  let everConnected = false;    // got at least one WS snapshot this mount
  let wsOpen = false;

  function setConn(kind, labelText) {
    connEl.className = 'conn conn--' + kind;
    const lbl = connEl.querySelector('.conn__label');
    if (lbl) lbl.textContent = labelText;
  }

  function renderBanner(vm) {
    clear(bannerHost);
    if (vm.connected) return;
    const ago = vm.lastTelemetryAt ? store.secsAgo(vm.lastTelemetryAt) : null;
    if (!everConnected && !vm.hasData) {
      // never reached: treat as no-bridge until we prove otherwise.
      bannerHost.appendChild(banner("Can't reach the bridge.", 'grey',
        { label: 'Retry', onClick: reconnect }));
      return;
    }
    // reconnecting: WS dropped, retrying. Keep last-known values dimmed.
    const msg = ago != null ? `Reconnecting… Last update ${ago} s ago` : 'Reconnecting…';
    bannerHost.appendChild(banner(msg, 'amber', { label: 'Retry', onClick: reconnect }));
  }

  function reconnect() {
    if (conn) { try { conn.close(); } catch { /* */ } conn = null; }
    openWs();
  }

  // ── glance block (§3.1) ──────────────────────────────────────────────────────
  const PHASE_STATUS_CLASS = {
    printing: 'is-printing', paused: 'is-paused', failed: 'is-failed',
    idle: 'is-idle', completed: 'is-finished',
  };

  function renderGlance(vm) {
    clear(glance);

    // status word — color by phase, but only when connected (disconnected
    // override shows "Reconnecting…" with no phase color).
    const statusCls = vm.connected ? (PHASE_STATUS_CLASS[vm.phase] || '') : '';
    glance.appendChild(el('div', { class: 'glance__status ' + statusCls, text: vm.statusTitle }));
    if (vm.statusSubtitle) {
      glance.appendChild(el('div', { class: 'glance__subtask', text: vm.statusSubtitle }));
    }

    // hero percent + time-left — ONLY when printing (showPercent gates it).
    if (vm.showPercent) {
      const pctText = vm.percent == null ? '—' : `${Math.round(vm.percent)} %`;
      glance.appendChild(el('div', { class: 'glance__hero' }, [
        el('div', { class: 'glance__pct live-num', text: pctText }),
        el('div', { class: 'glance__time live-num',
          text: vm.remainingMin == null ? '—' : fmtMinutes(vm.remainingMin) + ' left' }),
      ]));
      // layer m of n (null total -> "Layer N" with no denominator).
      if (vm.layerNum != null) {
        const layerText = vm.totalLayer
          ? `Layer ${vm.layerNum} of ${vm.totalLayer}`
          : `Layer ${vm.layerNum}`;
        glance.appendChild(el('div', { class: 'glance__layer live-num', text: layerText }));
      }
    }

    // progress bar gated on the indicator (§3.1 mapping):
    //   progress -> filled bar; indeterminate -> shimmer; amber/green/red ->
    //   color token w/ a thin filled bar; none -> no bar.
    const bar = buildProgressBar(vm);
    if (bar) glance.appendChild(bar);

    // grey live numbers when not connected (never-show-stale).
    glance.classList.toggle('is-stale', !vm.connected);
  }

  function buildProgressBar(vm) {
    if (!vm.connected) return null;            // no live bar while disconnected
    const ind = vm.indicator;
    if (ind === 'none') return null;
    if (ind === 'indeterminate') {
      return el('div', { class: 'progress progress--indeterminate' }, [
        el('div', { class: 'progress__fill' }),
      ]);
    }
    // determinate-ish indicators
    const pct = vm.showPercent && vm.percent != null ? Math.max(0, Math.min(100, vm.percent)) : 0;
    let cls = 'progress';
    if (ind === 'amber') cls += ' progress--amber';
    const fill = el('div', { class: 'progress__fill', style: { width: pct + '%' } });
    if (ind === 'green') fill.style.background = 'var(--state-finished)';
    if (ind === 'red') fill.style.background = 'var(--state-failed)';
    if (ind === 'amber' || ind === 'green' || ind === 'red') {
      // a state color with no percent: show a full thin bar as a status band.
      if (!vm.showPercent) fill.style.width = '100%';
    }
    return el('div', { class: cls }, [fill]);
  }

  // ── temperatures (§3.2 exact rules) ──────────────────────────────────────────
  function renderTemps(vm) {
    clear(tempsBox);
    const t = vm.temps || {};
    tempsBox.appendChild(tempRow('Nozzle', t.nozzle, vm.connected, false));
    tempsBox.appendChild(tempRow('Bed', t.bed, vm.connected, false));
    tempsBox.appendChild(tempRow('Chamber', t.chamber, vm.connected, true));
    tempsBox.classList.toggle('is-stale', !vm.connected);
  }

  // §3.2: {actual} °C [-> {target} °C] {hint}, heating/cooling cues.
  // chamberOnly => actual only (P1S has no chamber heater control).
  function tempRow(label, heater, connected, chamberOnly) {
    const row = el('div', { class: 'temprow' });
    row.appendChild(el('span', { class: 'temprow__label', text: label }));

    const actual = heater && typeof heater.current_c === 'number' ? heater.current_c : null;
    let target = heater && typeof heater.target_c === 'number' ? heater.target_c : null;
    if (chamberOnly) target = null;

    if (actual == null) {
      row.appendChild(el('span', { class: 'temprow__val live-num', text: '—' }));
      return row;
    }

    const valEl = el('span', { class: 'temprow__val live-num', text: `${Math.round(actual)} °C` });
    row.appendChild(valEl);

    // decide hint per §3.2.
    let hint = null;            // {text, cls}
    const showTarget = target != null && target > 0;
    if (showTarget) {
      if (target > actual + 2) hint = { text: '↑ heating', cls: 'is-heating' };
      else if (target < actual - 2) hint = { text: '↓ cooling', cls: 'is-cooling' };
      // |target-actual| <= 2 -> no arrow/hint, but still show the target reached.
    } else if (target === 0 && actual > 35) {
      // target 0 & actual >35 -> still cooling toward ambient.
      hint = { text: '↓ cooling', cls: 'is-cooling' };
    }
    // target 0 & actual <=35 -> actual only (no target shown). target≈actual -> no hint.

    if (showTarget) {
      row.appendChild(el('span', { class: 'temprow__target live-num', text: `→ ${Math.round(target)} °C` }));
    }
    if (hint) {
      if (hint.cls === 'is-heating') valEl.classList.add('is-hot');
      row.appendChild(el('span', { class: 'temprow__hint ' + hint.cls, text: hint.text }));
    }
    return row;
  }

  // ── fan row ──────────────────────────────────────────────────────────────────
  function renderFans(vm) {
    clear(fanRow);
    const c = vm.cooling || {};
    const fan = (label, key) => {
      const f = c[key];
      const pct = f && typeof f.percent === 'number' ? f.percent : null;
      return el('span', { class: 'live-num' }, [
        document.createTextNode(label + ' '),
        el('b', { text: pct == null ? '—' : pct + ' %' }),
      ]);
    };
    fanRow.appendChild(fan('Part', 'part_fan'));
    fanRow.appendChild(fan('Aux', 'aux_fan'));
    fanRow.appendChild(fan('Chamber', 'chamber_fan'));
    fanRow.classList.toggle('is-stale', !vm.connected);
  }

  // ── AMS one-line (§4.3 labeling + §6.1.1 rescan hold) ────────────────────────
  function renderAms(vm) {
    clear(amsLine);
    const ams = vm.ams || {};
    if (!ams.present) {
      amsLine.appendChild(el('span', { class: 'dim t-caption', text: 'No AMS detected' }));
      amsLine.classList.remove('is-rescan');
      return;
    }
    // rescan transient: hold last-known slots dimmed (never flip to "no AMS",
    // never render the empty transient).
    const slots = Array.isArray(ams.displaySlots) ? ams.displaySlots : [];
    amsLine.classList.toggle('is-rescan', !!ams.rescanning);

    const engaged = typeof ams.engaged_slot === 'number' ? ams.engaged_slot : null;
    const shown = engaged != null
      ? slots.find((s) => s.physical_slot === engaged) || slots[0]
      : slots[0];

    amsLine.appendChild(el('span', { class: 't-section', text: 'AMS ·' }));
    if (ams.rescanning && slots.length === 0) {
      amsLine.appendChild(el('span', { class: 'dim t-caption', text: 'AMS re-scanning…' }));
      return;
    }
    if (!shown) {
      amsLine.appendChild(el('span', { class: 'dim t-caption', text: 'No tray loaded' }));
      if (ams.rescanning) amsLine.appendChild(el('span', { class: 'dim t-caption', text: '· re-scanning…' }));
      return;
    }
    if (shown.color) {
      amsLine.appendChild(el('span', { class: 'swatch', style: { background: String(shown.color) } }));
    }
    const pct = typeof shown.remaining_pct === 'number' ? ` (${shown.remaining_pct} %)` : '';
    const desc = `${slotLabel(shown.physical_slot)} ${shown.type || 'Unknown'}${pct}`;
    amsLine.appendChild(el('span', { class: 'live-num', text: desc }));
    if (ams.rescanning) {
      amsLine.appendChild(el('span', { class: 'dim t-caption', text: '· re-scanning…' }));
    }
    amsLine.classList.toggle('is-stale', !vm.connected);
  }

  // ── sticky action bar (§3.4) ─────────────────────────────────────────────────
  const ACTIVE_PHASES = new Set(['printing', 'preparing', 'paused']);

  function renderActionBar(vm) {
    clear(actionbar);
    const active = vm.connected && ACTIVE_PHASES.has(vm.phase);
    const disabled = !vm.connected;        // controls disabled while disconnected

    if (active) {
      const isPaused = vm.phase === 'paused';
      actionbar.appendChild(actBtn(
        isPaused ? 'Resume' : 'Pause',
        () => doPauseResume(isPaused),
        { disabled },
      ));
      actionbar.appendChild(actBtn('Stop', () => doStop(vm), { disabled, danger: true }));
      actionbar.appendChild(actBtn('Light', () => doLight(vm), { disabled }));
      actionbar.appendChild(actBtn('More', () => app.navigate('#/controls'), {}));
    } else {
      // idle/completed/unknown: Pause/Stop disappear (don't waste a slot).
      actionbar.appendChild(actBtn('Light', () => doLight(vm), { disabled }));
      actionbar.appendChild(actBtn('Temp', () => app.navigate('#/controls'), { disabled }));
      actionbar.appendChild(actBtn('Move', () => app.navigate('#/controls'), { disabled }));
      actionbar.appendChild(actBtn('More', () => app.navigate('#/controls'), {}));
    }
  }

  function actBtn(label, onClick, opts = {}) {
    return el('button', {
      class: 'btn' + (opts.danger ? ' btn--danger' : '') + (opts.primary ? ' btn--primary' : ''),
      text: label,
      disabled: !!opts.disabled,
      onClick,
    });
  }

  // ── action handlers ──────────────────────────────────────────────────────────
  // All commands: 200 means accepted-by-bridge, NOT confirmed-by-printer. We show
  // transient feedback and let the WS state change confirm.
  async function post(path, body) {
    if (busy) return null;
    busy = true;
    try {
      const r = await api.postJson(`/printers/${encodeURIComponent(pid)}` + path, body);
      return r;
    } finally {
      busy = false;
    }
  }

  function showCommandError(r) {
    if (!r) return;
    if (r.error === 'printer_offline') {
      toast('Printer is offline.', { error: true });
      return;
    }
    // generic: a brief error toast; render verbatim message.
    toast(r.message || 'That command failed.', { error: true });
  }

  async function doPauseResume(isPaused) {
    const r = await post(isPaused ? '/print/resume' : '/print/pause', undefined);
    if (r && r.ok) toast(isPaused ? 'Resuming…' : 'Pausing…');
    else showCommandError(r);
  }

  async function doStop(vm) {
    const pctLine = vm.showPercent && vm.percent != null ? ` · ${Math.round(vm.percent)} % done` : '';
    const name = vm.subtaskName ? vm.subtaskName : 'this print';
    const ok = await confirmSheet({
      title: 'Stop this print?',
      body: `${name}${pctLine}\nThis can't be undone. The print ends and the toolhead parks.`,
      confirmLabel: 'Stop print',
      cancelLabel: 'Cancel',
      danger: true,
    });
    if (!ok) return;
    const r = await post('/print/stop', undefined);
    // command_accepted ≠ print_stopped: show "Stopping…" and wait for the WS
    // job_state_change -> canceled to flip the headline (never say "Stopped" here).
    if (r && r.ok) toast('Stopping…');
    else showCommandError(r);
  }

  async function doLight(vm) {
    const next = !vm.lightOn;
    const r = await post('/light', { on: next });
    if (r && r.ok) toast(next ? 'Light on' : 'Light off');
    else showCommandError(r);
  }

  // ── recent jobs (§3.5) ───────────────────────────────────────────────────────
  let recentLoaded = false;
  async function loadRecent() {
    if (recentLoaded) return;
    recentLoaded = true;
    const r = await api.api(`/jobs?printer_id=${encodeURIComponent(pid)}&limit=5`);
    clear(recentBox);
    if (!r || !r.ok || !Array.isArray(r.data)) {
      // history is v0.1; a 404/empty is not an error feeling — show nothing loud.
      recentBox.appendChild(el('div', { class: 'dim t-caption', text: 'No recent prints yet.' }));
      return;
    }
    const jobs = r.data;
    if (jobs.length === 0) {
      recentBox.appendChild(el('div', { class: 'dim t-caption', text: 'No recent prints yet.' }));
      return;
    }
    for (const job of jobs) {
      recentBox.appendChild(recentRow(job));
    }
  }

  function recentRow(job) {
    const name = job.file_name || job.subtask_name || 'Print';
    const state = job.state || 'unknown';
    const when = relTime(job.finished_at || job.started_at || job.queued_at);
    const sub = `${stateWord(state)}${when ? ' · ' + when : ''}`;
    const jid = job.job_id || '';
    // Job-detail is not a v0 screen; the actionable thing a tap can do is the
    // reprint jump to Submit (a real route). Both the row and the ↻ do that.
    const reprint = () => app.navigate(`#/print?reprint=${encodeURIComponent(jid)}`);
    const row = el('button', { class: 'listrow', onClick: reprint }, [
      el('div', { class: 'listrow__main' }, [
        el('div', { class: 'ellipsis', text: name }),
        el('div', { class: 'listrow__sub', text: sub }),
      ]),
      el('span', { class: 'listrow__chev', text: '↻', title: 'Reprint' }),
    ]);
    return row;
  }

  function stateWord(state) {
    return {
      queued: 'queued', uploading: 'uploading', submitted: 'submitted',
      preparing: 'preparing', printing: 'printing', paused: 'paused',
      completed: 'done', failed: 'failed', canceled: 'stopped',
    }[state] || state;
  }

  function relTime(iso) {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return 'just now';
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h} h ago`;
    const d = Math.round(h / 24);
    return `${d} d ago`;
  }

  // ── the render pass (subscribed to the store) ────────────────────────────────
  function render() {
    const vm = store.viewModel(pid);

    // header title (friendly name from the snapshot, or the picker).
    renderHeader(vm);

    // connection dot.
    if (vm.connected) {
      everConnected = true;
      setConn('live', 'live');
    } else if (everConnected || vm.hasData) {
      setConn('reconnecting', 'reconnecting');
    } else {
      setConn('nobridge', 'no bridge');
    }

    renderBanner(vm);
    renderGlance(vm);
    renderTemps(vm);
    renderFans(vm);
    renderAms(vm);
    renderActionBar(vm);

    // keep the "last update Xs ago" ticking while disconnected.
    if (!vm.connected) startStaleTicker();
    else stopStaleTicker();
  }

  function renderHeader(vm) {
    clear(topbar);
    const list = store.getState().printerList || [];
    const snap = store.current(pid);
    const name = (snap && (snap.friendly_name || snap.model))
      || friendlyFromList(list, pid) || 'Printer';

    if (list.length > 1) {
      const picker = el('button', { class: 'printer-picker' }, [
        el('span', { class: 'ellipsis', text: name }),
        el('span', { class: 'printer-picker__chev', text: '▾' }),
      ]);
      picker.addEventListener('click', () => openPicker(list));
      topbar.appendChild(picker);
    } else {
      topbar.appendChild(el('div', { class: 'topbar__title', text: name }));
    }
    topbar.appendChild(connEl);
  }

  function friendlyFromList(list, id) {
    const p = list.find((x) => (x.printer_id || x.id) === id);
    return p ? (p.friendly_name || p.model) : null;
  }

  function openPicker(list) {
    // a lightweight picker: a bottom sheet of printers. Reuses confirmSheet's
    // host indirectly via simple navigation — but here we just toggle current.
    const rows = list.map((p) => {
      const id = p.printer_id || p.id;
      return el('button', {
        class: 'listrow', onClick: () => { selectPrinter(id); pickerHandle.close(); },
      }, [
        el('div', { class: 'listrow__main' }, [
          el('div', { text: p.friendly_name || p.model || id }),
          el('div', { class: 'listrow__sub', text: p.model || '' }),
        ]),
        id === pid ? el('span', { class: 'listrow__chev', text: '✓' }) : null,
      ]);
    });
    const content = el('div', {}, [
      el('div', { class: 'sheet__title', text: 'Choose printer' }),
      el('div', { class: 'stack' }, rows),
    ]);
    const pickerHandle = app.ui.mountSheet(content);
  }

  function selectPrinter(id) {
    if (id === pid) return;
    app.setCurrentPrinterId(id);
    // Switching the printer needs a clean re-mount (new WS, new camera poller).
    // The router only re-renders on hashchange, and we're already at #/, so a
    // reload is the simplest dependency-free way to rebuild against the new id.
    location.reload();
  }

  // ── stale ticker ─────────────────────────────────────────────────────────────
  function startStaleTicker() {
    if (staleTimer) return;
    staleTimer = setInterval(() => {
      const vm = store.viewModel(pid);
      if (vm.connected) { stopStaleTicker(); return; }
      renderBanner(vm);
      // also refresh the disconnected subtitle in the glance.
      const sub = glance.querySelector('.glance__subtask');
      if (sub && !vm.connected && vm.lastTelemetryAt) {
        sub.textContent = `Last update ${store.secsAgo(vm.lastTelemetryAt)} s ago`;
      }
    }, 1000);
  }
  function stopStaleTicker() {
    if (staleTimer) { clearInterval(staleTimer); staleTimer = null; }
  }

  // ── WS lifecycle ─────────────────────────────────────────────────────────────
  function openWs() {
    conn = ws.connectStatus(pid, {
      onConnState: (open) => { wsOpen = open; if (open) everConnected = true; },
      onEvent: (name, data) => onWsEvent(name, data),
      onUnauthorized: () => app.requireKeyScreen(),
      onUnknownPrinter: () => app.navigate('#/settings'),
      onProtocolMismatch: () => {
        clear(bannerHost);
        bannerHost.appendChild(banner(
          'This app needs an update to talk to the bridge.', 'red'));
      },
    });
  }

  function onWsEvent(name, data) {
    // §13 alert derivation is a UI concern; the load-bearing v0 one is the
    // feed_warning air-print banner. We surface it as a non-blocking banner with
    // "View camera" + "Stop".
    if (name === 'feed_warning') {
      clear(bannerHost);
      const since = data && data.since_ms ? Math.round(data.since_ms / 1000) : null;
      const msg = since != null
        ? `Print may not be feeding filament (${since}s). Check the plate.`
        : 'Print may not be feeding filament. Check the plate.';
      const b = banner(msg, 'amber', { label: 'View camera', onClick: () => app.navigate('#/camera') });
      bannerHost.appendChild(b);
    }
    // job_state_change / print_* events flow through the store snapshot/delta
    // (the headline/phase already reflects them); no extra handling needed here.
  }

  // ── boot ─────────────────────────────────────────────────────────────────────
  store.loadCachedSnapshot(pid);   // cold-load: render last-known immediately
  unsub = store.subscribe(render);
  openWs();
  render();
  loadRecent();

  // refresh the printer list so the picker shows up if >1 (best-effort).
  api.api('/printers').then((r) => {
    if (r && r.ok && Array.isArray(r.data)) store.setPrinterList(r.data);
  }).catch(() => { /* non-fatal */ });

  return function unmount() {
    if (conn) { try { conn.close(); } catch { /* */ } conn = null; }
    if (unsub) { unsub(); unsub = null; }
    stopStaleTicker();
    camera.destroy();
  };
}
