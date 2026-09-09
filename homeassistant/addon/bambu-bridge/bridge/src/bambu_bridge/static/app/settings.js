// settings.js — the Settings screen (frontend-ux-spec §6).
//
// Build-free, dependency-free. Sections, in order:
//
//   §6.1 BRIDGE   — bridge address + masked key + Test (the SAME two-call
//                   health→printers flow as onboarding Step 1, via
//                   api.testConnection(); the key is persisted only on a
//                   successful Test).
//        PRINTERS — GET /printers list; Add (POST) / Edit (PATCH) via a shared
//                   form helper (no serial field; LAN-Only callout; the E1/E2/
//                   E3 forks render identically to onboarding); Delete (DELETE,
//                   409 active_job_ids → cascade confirm ?cascade_jobs=true).
//   §6.2 3D VIEWER — a prominent entry opening GET …/viz?token= (new tab).
//   §6.1 APP      — theme (app.setPref → applyTheme), camera FPS, default
//                   unload °C, Dev mode.
//   §6.3 ABOUT & UPDATES — bridge version (GET /version) + the SPA build
//                   constant + an honest "Check for updates" panel (optional
//                   manifest URL, OFF by default; version compare + copyable
//                   runbook; never fails loudly) + "Re-run setup".
//   §6.4 Cert-changed (403 printer_cert_changed) anywhere → ui.certChangedSheet
//                   with Trust (POST …/trust) + Not now.
//
// Contract: export function mount(root, app) -> unmount(). See main.js header.

import {
  el, clear, toast, confirmSheet, errorCard, certChangedSheet,
} from './ui.js';

// The SPA's own build constant (§6.3). Bumped by hand alongside releases; the
// bridge version comes from GET /version separately.
const SPA_BUILD = '0.1.1';

// Test-flow status-line copy — identical wording to onboarding Step 1 (§6.1).
const TEST_COPY = {
  connected: (n) => `✓ Connected. Found ${n} printer${n === 1 ? '' : 's'}.`,
  connected0: '✓ Connected. No printers registered yet.',
  key_rejected: 'Bridge reachable, but the API key was rejected.',
  not_configured: 'This bridge has no API key configured yet.',
  no_bridge: "Can't reach the bridge at that address. Check it's running and you're on the same network.",
  wrong_address: 'No bridge at that address. Check the host and port (default 8080).',
  bridge_error: 'Bridge error. The bridge’s logs will say why.',
};
const TEST_KIND = {
  connected: 'ok', key_rejected: 'err', not_configured: 'warn',
  no_bridge: 'info', wrong_address: 'warn', bridge_error: 'err',
};

export function mount(root, app) {
  const shell = el('div', { class: 'app-shell' });
  const main = el('div', { class: 'app-main' });
  shell.appendChild(main);
  root.appendChild(shell);

  main.appendChild(el('div', { class: 'topbar' }, [
    el('div', { class: 'topbar__title', text: 'Settings' }),
  ]));

  main.appendChild(buildBridgeSection(app));

  const printersSection = el('div', { class: 'section' });
  main.appendChild(printersSection);
  renderPrinters(app, printersSection);

  main.appendChild(buildViewerSection(app));
  main.appendChild(buildAppSection(app));
  main.appendChild(buildAboutSection(app));

  // static screen — no timers/WS to tear down.
  return function unmount() {};
}

// ── §6.1 BRIDGE (address + masked key + Test) ─────────────────────────────────
function buildBridgeSection(app) {
  const addrInput = el('input', {
    class: 'input', type: 'url',
    value: app.baseUrl() || location.origin,
  });

  const keyInput = el('input', {
    class: 'input input--mono', type: 'password', autocomplete: 'off',
    value: app.getKey(),
  });
  const eye = el('button', { class: 'input-eye', type: 'button', text: '👁', title: 'Show/hide' });
  eye.addEventListener('click', () => {
    keyInput.type = keyInput.type === 'password' ? 'text' : 'password';
  });

  const statusLine = el('div', { class: 'statusline' });
  const testBtn = el('button', { class: 'btn btn--primary btn--block', text: 'Test connection' });

  function setStatus(text, kind) {
    statusLine.className = 'statusline is-' + kind;
    statusLine.textContent = text;
  }

  testBtn.addEventListener('click', async () => {
    const prevKey = app.getKey();
    // point testConnection() at the typed address; same-origin → clear override.
    const addr = addrInput.value.trim();
    app.api.setBaseUrl(addr === location.origin ? '' : addr);
    app.setKey(keyInput.value.trim());      // temporary; reverted on failure

    testBtn.disabled = true;
    setStatus('Testing…', 'info');
    const res = await app.api.testConnection();

    if (res.outcome === 'connected') {
      const n = res.printerCount || 0;
      setStatus(n ? TEST_COPY.connected(n) : TEST_COPY.connected0, 'ok');
      // key already stored (success); refresh the printer list in the store.
      if (Array.isArray(res.printers)) app.store.setPrinterList(res.printers);
      testBtn.disabled = false;
      return;
    }

    // failure — do not persist a bad key; revert.
    app.setKey(prevKey);
    testBtn.disabled = false;
    setStatus(TEST_COPY[res.outcome] || 'Could not connect.', TEST_KIND[res.outcome] || 'err');
  });

  return el('div', { class: 'section' }, [
    el('div', { class: 't-section', text: 'Bridge' }),
    el('div', { class: 'card mt-2' }, [
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Bridge address' }), addrInput,
      ]),
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'API key' }),
        el('div', { class: 'input-wrap' }, [keyInput, eye]),
      ]),
      testBtn,
      statusLine,
    ]),
  ]);
}

// ── §6.1 PRINTERS (list + add/edit/delete) ────────────────────────────────────
async function renderPrinters(app, section) {
  clear(section);
  section.appendChild(el('div', { class: 't-section', text: 'Printers' }));

  const listCard = el('div', { class: 'card mt-2' }, [
    el('div', { class: 'dim', text: 'Loading printers…' }),
  ]);
  section.appendChild(listCard);

  const addBtn = el('button', {
    class: 'btn btn--ghost btn--block mt-3', text: '+ Add printer',
    onClick: () => openPrinterForm(app, section, null),
  });
  section.appendChild(addBtn);

  const res = await app.api.api('/printers');

  // cert-changed can surface on this read too (§6.4) — but GET /printers is
  // bridge-level, not per-printer; the cert gate fires on per-printer routes.
  if (handleCertChanged(app, res, () => renderPrinters(app, section))) return;

  clear(listCard);
  const list = Array.isArray(res.data) ? res.data : [];
  if (!res.ok) {
    listCard.appendChild(el('div', { class: 'statusline is-err', text:
      res.message || 'Could not load printers.' }));
    return;
  }
  if (app.store && app.store.setPrinterList) app.store.setPrinterList(list);
  if (list.length === 0) {
    listCard.appendChild(el('div', { class: 'dim', text: 'No printers registered yet.' }));
    return;
  }

  const stack = el('div', { class: 'stack' });
  for (const p of list) {
    const online = !!p.connected;
    stack.appendChild(el('button', {
      class: 'listrow',
      onClick: () => openPrinterForm(app, section, p),
    }, [
      el('div', { class: 'listrow__main' }, [
        el('div', { class: 'ellipsis', text: p.friendly_name || p.serial || 'P1S' }),
        el('div', { class: 'listrow__sub', text: online ? '● online' : '○ offline' }),
      ]),
      el('span', { class: 'listrow__chev', text: '›' }),
    ]));
  }
  listCard.appendChild(stack);
}

// The add/edit form (shared shape with onboarding Step 2: no serial field, the
// LAN-Only prerequisite callout, the access-code/IP disclosures). Add → POST,
// Edit → PATCH. Both can return the E1/E2/E3 three-fork errors (§3.4).
function openPrinterForm(app, section, existing) {
  const isEdit = !!existing;

  const nameInput = el('input', {
    class: 'input', type: 'text', placeholder: 'P1S',
    value: (existing && existing.friendly_name) || '',
  });
  const ipInput = el('input', {
    class: 'input', type: 'text', placeholder: '192.168.1.42',
    value: (existing && (existing.ip || existing.host)) || '',
  });
  const codeInput = el('input', {
    class: 'input input--mono', type: 'password', autocomplete: 'off',
    inputmode: 'numeric', placeholder: '8 characters',
  });
  const codeEye = el('button', { class: 'input-eye', type: 'button', text: '👁' });
  codeEye.addEventListener('click', () => {
    codeInput.type = codeInput.type === 'password' ? 'text' : 'password';
  });

  const errSlot = el('div', {});
  const submitBtn = el('button', {
    class: 'btn btn--primary btn--block',
    text: isEdit ? 'Save changes' : 'Register',
  });

  const form = el('div', {}, [
    el('div', { class: 't-display', style: { fontSize: '22px' },
      text: isEdit ? 'Edit printer' : 'Add your printer' }),
    el('div', { class: 'card mt-4' }, [
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Printer name' }), nameInput,
      ]),
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Printer IP address' }), ipInput,
        el('details', { class: 'disclosure' }, [
          el('summary', { text: 'Where’s the IP?' }),
          el('div', { class: 'disclosure__body', text:
            'On the printer: Settings ▸ WLAN ▸ IP. Note the IP can change.' }),
        ]),
      ]),
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text:
          isEdit ? 'Access code (leave blank to keep)' : 'Access code (8 characters)' }),
        el('div', { class: 'input-wrap' }, [codeInput, codeEye]),
        el('details', { class: 'disclosure' }, [
          el('summary', { text: 'Where’s the access code?' }),
          el('div', { class: 'disclosure__body', text:
            'On the printer’s touchscreen: Settings ▸ WLAN ▸ Access Code. '
            + 'It’s an 8-character code (case-sensitive, and you can regenerate it there).' }),
        ]),
      ]),
      // the LAN-Only prerequisite, shown up front (§1 Step 2)
      el('div', { class: 'banner banner--amber' }, [
        el('span', { class: 'banner__msg', text:
          'Before you tap ' + (isEdit ? 'Save' : 'Register')
          + ': on the P1S, turn ON Settings ▸ Network ▸ LAN-Only Mode.' }),
      ]),
      submitBtn,
      errSlot,
    ]),
  ]);

  // delete (edit only) — DELETE, with the 409 cascade confirm (§6.1).
  if (isEdit) {
    form.querySelector('.card').appendChild(el('button', {
      class: 'btn btn--danger btn--block mt-3', text: 'Delete printer',
      onClick: () => doDelete(app, section, existing),
    }));
  }

  form.appendChild(el('button', {
    class: 'btn btn--ghost btn--block mt-3', text: 'Cancel',
    onClick: () => renderPrinters(app, section),
  }));

  // swap the printers section to show the form
  clear(section);
  section.appendChild(el('div', { class: 't-section', text: 'Printers' }));
  section.appendChild(form);

  submitBtn.addEventListener('click', async () => {
    clear(errSlot);
    const name = nameInput.value.trim();
    const ip = ipInput.value.trim();
    const code = codeInput.value.trim();

    submitBtn.disabled = true;
    submitBtn.textContent = isEdit ? 'Saving…' : 'Registering…';

    let res;
    if (isEdit) {
      const patch = {};
      if (name) patch.friendly_name = name;
      if (ip) patch.ip = ip;
      if (code) patch.access_code = code;
      res = await app.api.patchJson(
        `/printers/${encodeURIComponent(existing.printer_id)}`, patch);
    } else {
      const payload = { host: ip, access_code: code };
      if (name) payload.friendly_name = name;
      res = await app.api.postJson('/printers', payload);
    }

    submitBtn.disabled = false;
    submitBtn.textContent = isEdit ? 'Save changes' : 'Register';

    if (res.ok || res.status === 200 || res.status === 201) {
      toast(isEdit ? 'Printer updated.' : 'Printer added.');
      const created = res.data && (res.data.printer_id);
      if (!isEdit && created) app.setCurrentPrinterId(created);
      renderPrinters(app, section);
      return;
    }

    // 403 printer_cert_changed → trust sheet, then retry the same action.
    if (handleCertChanged(app, res, () => submitBtn.click())) return;

    // 409 conflict — already registered (POST): offer to open it.
    if (res.status === 409 && res.existing_printer_id) {
      errSlot.appendChild(errorCard(res, {
        actions: [{ label: 'Open it', primary: true, onClick: () => {
          app.setCurrentPrinterId(res.existing_printer_id);
          app.navigate('#/');
        } }],
      }));
      return;
    }

    // E1/E2/E3 + 422 — inline error cards with tailored remediation.
    errSlot.appendChild(buildRegisterError(app, res, {
      focusIp: () => ipInput.focus(),
      focusCode: () => codeInput.focus(),
      retry: () => submitBtn.click(),
    }));
  });
}

// Map a register/edit failure to the right inline card (§1 forks, §3.4).
function buildRegisterError(app, res, handlers) {
  const err = res.error;

  // E1 — printer_unreachable: edit IP & try again.
  if (err === 'printer_unreachable') {
    return errorCard(res, {
      actions: [{ label: 'Edit IP & try again', primary: true,
        onClick: () => handlers.focusIp() }],
    });
  }
  // E2 — printer_auth_failed: reassure (reached the printer), edit code.
  if (err === 'printer_auth_failed') {
    const serial = res.discovered && res.discovered.serial;
    return errorCard(res, {
      reassurance: serial ? 'We did reach your printer.' : undefined,
      actions: [{ label: 'Edit access code & try again', primary: true,
        onClick: () => handlers.focusCode() }],
    });
  }
  // E3 — mqtt_no_telemetry: plain retry (flip a printer setting first).
  if (err === 'mqtt_no_telemetry') {
    return errorCard(res, {
      actions: [{ label: 'Try again', primary: true, onClick: () => handlers.retry() }],
    });
  }
  // 422 invalid_input — render message + issues; keep the form.
  // anything else — generic with the verbatim message.
  return errorCard(res);
}

async function doDelete(app, section, printer) {
  const ok = await confirmSheet({
    title: 'Delete this printer?',
    body: `${printer.friendly_name || printer.serial || 'This printer'} will be removed.`,
    confirmLabel: 'Delete', danger: true,
  });
  if (!ok) return;

  let res = await app.api.del(`/printers/${encodeURIComponent(printer.printer_id)}`);

  // 409 conflict with active_job_ids → cascade confirm (§6.1).
  if (res.status === 409 && Array.isArray(res.active_job_ids) && res.active_job_ids.length) {
    const cascade = await confirmSheet({
      title: 'This printer has an active job.',
      body: 'Delete it anyway (and its job history)?',
      confirmLabel: 'Delete everything', danger: true,
    });
    if (!cascade) return;
    res = await app.api.del(
      `/printers/${encodeURIComponent(printer.printer_id)}?cascade_jobs=true`);
  }

  if (res.ok || res.status === 204) {
    if (app.store && app.store.clearCachedSnapshot) {
      app.store.clearCachedSnapshot(printer.printer_id);
    }
    if (app.currentPrinterId() === printer.printer_id) app.setCurrentPrinterId(null);
    toast('Printer deleted.');
    renderPrinters(app, section);
    return;
  }
  if (handleCertChanged(app, res, () => doDelete(app, section, printer))) return;
  toast(res.message || 'Could not delete the printer.', { error: true, ms: 3500 });
}

// ── §6.2 3D viewer entry ───────────────────────────────────────────────────────
function buildViewerSection(app) {
  const id = app.currentPrinterId();
  const open = () => {
    if (!id) { toast('Select a printer first.', { error: true }); return; }
    // viz accepts the master key OR the viz token via ?token= (contract §1.1).
    const url = app.api.tokenUrl(`/printers/${encodeURIComponent(id)}/viz`);
    window.open(url, '_blank', 'noopener');
  };
  const list = app.store ? app.store.getState().printerList : [];
  const current = Array.isArray(list) ? list.find((p) => p.printer_id === id) : null;
  const name = (current && current.friendly_name) || 'this printer';

  return el('div', { class: 'section' }, [
    el('div', { class: 't-section', text: '3D print viewer' }),
    el('div', { class: 'card mt-2' }, [
      el('button', {
        class: 'btn btn--primary btn--block',
        text: id ? `Open 3D viewer for ${name} →` : 'Open 3D viewer →',
        disabled: !id,
        onClick: open,
      }),
      el('div', { class: 'field__hint mt-2', text:
        'Watch the model build layer-by-layer. Opens in a new tab.' }),
    ]),
  ]);
}

// ── §6.1 APP prefs ─────────────────────────────────────────────────────────────
function buildAppSection(app) {
  // theme
  const themeSel = el('select', { class: 'select' }, [
    el('option', { value: 'system', text: 'System' }),
    el('option', { value: 'dark', text: 'Dark' }),
    el('option', { value: 'light', text: 'Light' }),
  ]);
  themeSel.value = app.prefs.theme;
  themeSel.addEventListener('change', () => {
    app.setPref('theme', themeSel.value);   // setPref re-applies theme on 'theme'
  });

  // camera FPS
  const fpsSel = el('select', { class: 'select' }, [
    el('option', { value: '0.5', text: '0.5 fps' }),
    el('option', { value: '1', text: '1 fps' }),
    el('option', { value: '2', text: '2 fps' }),
  ]);
  fpsSel.value = String(app.prefs.cameraFps);
  fpsSel.addEventListener('change', () => app.setPref('cameraFps', Number(fpsSel.value)));

  // default unload temperature (180–280)
  const unloadInput = el('input', {
    class: 'input', type: 'number', min: '180', max: '280', step: '5',
    value: String(app.prefs.defaultUnloadC),
  });
  unloadInput.addEventListener('change', () => {
    let v = parseInt(unloadInput.value, 10);
    if (Number.isNaN(v)) v = 220;
    v = Math.min(280, Math.max(180, v));
    unloadInput.value = String(v);
    app.setPref('defaultUnloadC', v);
  });

  // dev mode
  const devToggle = el('input', { type: 'checkbox' });
  devToggle.checked = !!app.prefs.devMode;
  devToggle.addEventListener('change', () => app.setPref('devMode', devToggle.checked));

  return el('div', { class: 'section' }, [
    el('div', { class: 't-section', text: 'App' }),
    el('div', { class: 'card mt-2' }, [
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Theme' }), themeSel,
      ]),
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Camera frame rate' }), fpsSel,
      ]),
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Default unload temperature (°C)' }),
        unloadInput,
      ]),
      el('label', { class: 'row row--between' }, [
        el('div', {}, [
          el('div', { text: 'Dev mode' }),
          el('div', { class: 'listrow__sub', text:
            'Shows technical details (_raw) on error cards.' }),
        ]),
        devToggle,
      ]),
    ]),
  ]);
}

// ── §6.3 ABOUT & UPDATES ───────────────────────────────────────────────────────
function buildAboutSection(app) {
  const bridgeVerEl = el('span', { class: 'live-num', text: '…' });

  // current bridge version (GET /version, unauthenticated).
  app.api.api('/version', { auth: false }).then((res) => {
    bridgeVerEl.textContent = (res.ok && res.data && res.data.version)
      ? `v${res.data.version}` : 'unknown';
  }).catch(() => { bridgeVerEl.textContent = 'unknown'; });

  // update manifest URL (OFF by default — airgap-friendly, §6.3 open decision).
  const manifestInput = el('input', {
    class: 'input', type: 'url', placeholder: 'https://…/releases/latest.json',
    value: app.prefs.updateManifestUrl || '',
  });
  manifestInput.addEventListener('change', () =>
    app.setPref('updateManifestUrl', manifestInput.value.trim()));

  const updateStatus = el('div', { class: 'statusline mt-3' });
  const runbookSlot = el('div', { class: 'mt-3' });

  const checkBtn = el('button', {
    class: 'btn btn--ghost btn--block mt-3', text: 'Check for updates',
    onClick: () => checkForUpdates(app, manifestInput.value.trim(), updateStatus, runbookSlot, bridgeVerEl),
  });

  return el('div', { class: 'section' }, [
    el('div', { class: 't-section', text: 'About & updates' }),
    el('a', { class: 'btn btn--ghost btn--block mt-2', href: 'https://github.com/thereprocase/bambu-bridge/tree/v0.1.1', target: '_blank', rel: 'noopener noreferrer', text: 'Source code & AGPL license' }),
    el('div', { class: 'card mt-2' }, [
      el('div', { class: 'row row--between' }, [
        el('span', { class: 'dim', text: 'Bridge version' }), bridgeVerEl,
      ]),
      el('div', { class: 'row row--between mt-2' }, [
        el('span', { class: 'dim', text: 'App (SPA)' }),
        el('span', { text: `v${SPA_BUILD}` }),
      ]),
      el('div', { class: 'field mt-4' }, [
        el('label', { class: 'field__label', text: 'Update manifest URL (optional)' }),
        manifestInput,
        el('div', { class: 'field__hint', text:
          'Off by default. A strict offline appliance can leave this blank.' }),
      ]),
      checkBtn,
      updateStatus,
      runbookSlot,
      el('button', {
        class: 'btn btn--ghost btn--block mt-4', text: 'Re-run setup',
        onClick: () => app.navigate('#/setup'),
      }),
    ]),
  ]);
}

// Honest "check for updates" (§6.3): best-effort manifest fetch, never fail
// loudly; show a copyable runbook when a newer version exists.
async function checkForUpdates(app, manifestUrl, statusEl, runbookSlot, bridgeVerEl) {
  clear(runbookSlot);
  if (!manifestUrl) {
    statusEl.className = 'statusline is-info mt-3';
    statusEl.textContent = 'No update source set. Showing the version you’re on.';
    return;
  }

  statusEl.className = 'statusline is-info mt-3';
  statusEl.textContent = 'Checking…';

  // current bridge version for comparison
  let current = SPA_BUILD;
  try {
    const v = await app.api.api('/version', { auth: false });
    if (v.ok && v.data && v.data.version) current = v.data.version;
  } catch { /* fall back to SPA_BUILD */ }

  let latest = null;
  try {
    const r = await fetch(manifestUrl, { mode: 'cors' });
    if (r.ok) {
      const m = await r.json().catch(() => null);
      latest = m && (m.version || m.tag_name || m.name);
      if (latest) latest = String(latest).replace(/^v/, '');
    }
  } catch { /* network/offline — handled below, never loud */ }

  if (!latest) {
    // §6.3: not an error, just a fact.
    statusEl.className = 'statusline is-info mt-3';
    statusEl.textContent =
      `Couldn’t check for updates (this bridge may be offline by design). You’re on v${current}.`;
    return;
  }

  if (cmpVersions(latest, current) <= 0) {
    statusEl.className = 'statusline is-ok mt-3';
    statusEl.textContent = `You’re up to date (v${current}).`;
    return;
  }

  // newer version available — show the exact host runbook, copyable.
  statusEl.className = 'statusline is-warn mt-3';
  statusEl.textContent = `v${latest} is available (you’re on v${current}).`;

  const runbook =
    'cd ~/bambu-bridge && git pull   # or rsync per DEPLOY.md\n'
    + '.venv/bin/pip install .\n'
    + 'sudo systemctl restart bambu-bridge';

  const pre = el('pre', { class: 'errcard__raw', text: runbook });
  const copyBtn = el('button', {
    class: 'btn btn--sm btn--ghost mt-2', text: 'Copy commands',
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(runbook);
        toast('Copied.');
      } catch { toast('Copy failed — select the text manually.', { error: true }); }
    },
  });
  runbookSlot.appendChild(el('div', { class: 'card mt-2' }, [
    el('div', { class: 'field__label', text: 'Run this on the bridge host' }),
    pre, copyBtn,
    el('div', { class: 'field__hint mt-2', text:
      'After it restarts, this page will reconnect automatically.' }),
  ]));
}

// ── §6.4 cert-changed everywhere ───────────────────────────────────────────────
/**
 * If a result is a 403 printer_cert_changed, show the TOFU trust sheet. On
 * Trust, POST …/trust and re-run `retry`. Returns true if it handled the
 * result (so the caller stops). The auth interceptor never bounces this to the
 * Key screen — it keys off the error enum.
 */
function handleCertChanged(app, res, retry) {
  if (!res || res.error !== 'printer_cert_changed') return false;
  certChangedSheet(res, async (action) => {
    if (!action || action.id !== 'trust') return;       // "Not now"
    // the trust action carries its own method+path; honor it.
    const path = (action.path || '').replace(/^\/api\/v1/, '');
    const t = await app.api.postJson(path, undefined);
    if (t.ok || t.status === 204) {
      toast('Printer trusted.');
      if (typeof retry === 'function') retry();
    } else {
      toast(t.message || 'Could not trust the printer.', { error: true });
    }
  });
  return true;
}

// ── version compare (semver-ish; tolerant of pre-release suffixes) ────────────
function cmpVersions(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d !== 0) return d < 0 ? -1 : 1;
  }
  return 0;
}
