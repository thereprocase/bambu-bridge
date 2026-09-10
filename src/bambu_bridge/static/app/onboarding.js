// onboarding.js — the first-run 3-step wizard (frontend-ux-spec §1) and the
// printer add/edit form helper that Settings (§6.1) reuses.
//
// Contract: export function mount(root, app) -> unmount(). See main.js header.
// The wizard owns the whole viewport (the router hides the nav chrome for this
// route) — it renders its own `.wizard` root rather than the `.app-shell`.
//
// Steps:
//   0  Welcome card (only on a truly-fresh first run; "Get started")
//   1  Connect to the bridge: address + key + Test (all status-line outcomes,
//      incl. 503 auth_not_configured). Key persisted to localStorage ONLY on a
//      successful Test, so a bad key never gets stored to bounce the user.
//   2  Register your first printer: NO serial field; LAN-Only Mode callout up
//      front; access-code/IP touchscreen disclosures; E1/E2/E3 inline cards
//      with tailored actions; 409 already-registered shortcut; 422 field issues.
//   3  Success -> land on the dashboard + fire the tour.
//
// Exports (also consumed by settings.js):
//   - mount(root, app)
//   - renderPrinterForm(app, opts) -> { node, getValues, setBusy, showError,
//                                       clearError, focusField }
//       opts: { mode:'add'|'edit', initial?, submitLabel?, busyLabel?,
//               busySubline?, onSubmit(values), onError?(result), showLanCallout }

import { el, errorCard, toast } from './ui.js';

// ── Step-1 status-line copy (frontend-ux-spec §1 table; verbatim-ish) ───────
const STATUS_COPY = {
  connected: (n) => `✓ Connected. Found ${n} printer${n === 1 ? '' : 's'}.`,
  connected0: '✓ Connected. No printers registered yet.',
  key_rejected: 'Bridge reachable, but the API key was rejected.',
  not_configured: 'This bridge has no API key configured yet.',
  no_bridge: "Can't reach the bridge at that address. Check it's running and you're on the same Tailscale network.",
  wrong_address: 'No bridge at that address. Check the host and port (default 8080).',
  bridge_error: (code) => `Bridge error (HTTP ${code}). The bridge's logs will say why.`,
};

// map an outcome -> .statusline modifier
const STATUS_KIND = {
  connected: 'ok',
  connected0: 'ok',
  key_rejected: 'err',
  not_configured: 'warn',
  no_bridge: 'info',
  wrong_address: 'warn',
  bridge_error: 'err',
};

const BRIDGE_KEY_HELP =
  'The key is set on the machine running the bridge, in '
  + '~/.config/bambu-bridge/bridge.env as BRIDGE_API_KEY=…. '
  + "If you set it up, it's the value you generated with openssl rand -hex 32. "
  + 'Paste the whole string.';

const ACCESS_CODE_HELP =
  'On the printer’s touchscreen: Settings ▸ WLAN ▸ Access Code. '
  + "It's an 8-digit code (you can regenerate it there). "
  + 'The printer’s IP is on the same screen under Settings ▸ WLAN ▸ IP '
  + '— note that the IP can change.';

// ── the step rail ───────────────────────────────────────────────────────────
const STEP_LABELS = ['Key', 'Printer', 'Done'];

/**
 * Build the 3-step rail. `current` is 0|1|2 (the 3 visible steps; Step 0
 * Welcome shares the "Key" position).
 * @param {number} current
 * @returns {HTMLElement}
 */
function stepRail(current) {
  const kids = [];
  for (let i = 0; i < 3; i++) {
    const done = i < current;
    const cur = i === current;
    kids.push(el('span', {
      class: 'steprail__dot' + (done ? ' is-done' : cur ? ' is-current' : ''),
    }));
    if (i < 2) kids.push(el('span', { class: 'steprail__bar' + (i < current ? ' is-done' : '') }));
  }
  // collapsed label for phones (CSS can hide the dots if it wants; we keep both)
  kids.push(el('span', { class: 'grow' }));
  kids.push(el('span', { text: `${STEP_LABELS[current]} · Step ${current + 1} of 3` }));
  return el('div', { class: 'steprail' }, kids);
}

// ── mount ────────────────────────────────────────────────────────────────────
export function mount(root, app) {
  const wizard = el('div', { class: 'wizard' });
  root.appendChild(wizard);

  // wizard owns the viewport: no nav chrome.
  app.showNav(false);

  // shared step state. We keep the registered printer's display facts for Step 3.
  const stepHost = el('div', { class: 'grow' });
  wizard.appendChild(stepHost);

  let teardown = null;     // per-step cleanup (timers/listeners)

  function go(render) {
    if (typeof teardown === 'function') { try { teardown(); } catch { /* */ } }
    teardown = null;
    while (stepHost.firstChild) stepHost.removeChild(stepHost.firstChild);
    teardown = render() || null;
  }

  // A truly-fresh first run (no key yet) gets the Welcome card; "Re-run setup"
  // with a key already present skips straight to Step 1.
  const fresh = !app.getKey();
  if (fresh) go(renderWelcome);
  else go(renderStep1);

  // ── Step 0: Welcome ──────────────────────────────────────────────────────
  function renderWelcome() {
    stepHost.appendChild(stepRail(0));
    stepHost.appendChild(el('div', { class: 't-display', style: { fontSize: '32px' }, text: 'Bambu Bridge' }));
    stepHost.appendChild(el('div', { class: 'card mt-4' }, [
      el('div', { class: 't-body', text:
        'Watch and control your P1S from this browser, over your own network.' }),
      el('div', { class: 'dim mt-3', text:
        "You'll need two things: the bridge API key and your printer's IP + access code. "
        + 'Takes about a minute.' }),
      el('button', {
        class: 'btn btn--primary btn--block mt-4',
        text: 'Get started',
        onClick: () => go(renderStep1),
      }),
    ]));
  }

  // ── Step 1: Connect to the bridge (key + Test) ───────────────────────────
  function renderStep1() {
    stepHost.appendChild(stepRail(0));
    stepHost.appendChild(el('div', { class: 't-display', style: { fontSize: '28px' }, text: 'Connect to your bridge' }));

    const addrInput = el('input', {
      class: 'input', type: 'url', autocapitalize: 'off', autocorrect: 'off',
      spellcheck: 'false', value: app.baseUrl() || location.origin,
    });
    const keyInput = el('input', {
      class: 'input input--mono', type: 'password', autocomplete: 'off',
      autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false',
      value: app.getKey(),
    });
    const eye = el('button', { class: 'input-eye', type: 'button', title: 'Show or hide key', text: '👁' });
    eye.addEventListener('click', () => {
      keyInput.type = keyInput.type === 'password' ? 'text' : 'password';
    });

    const helpDisclosure = el('details', { class: 'disclosure' }, [
      el('summary', { text: 'Where do I find this?' }),
      el('div', { class: 'disclosure__body', text: BRIDGE_KEY_HELP }),
    ]);

    const statusLine = el('div', { class: 'statusline' });
    const testBtn = el('button', { class: 'btn btn--primary btn--block', text: 'Test connection' });

    // a "Continue" button only appears after a successful Test (or skip-decision)
    const continueBtn = el('button', { class: 'btn btn--block mt-3 hidden', text: 'Continue' });

    function setStatus(text, kind) {
      statusLine.className = 'statusline' + (kind ? ' is-' + kind : '');
      statusLine.textContent = text;
    }
    function clearStatus() { statusLine.className = 'statusline'; statusLine.textContent = ''; }

    function setTestLabel(testing) {
      while (testBtn.firstChild) testBtn.removeChild(testBtn.firstChild);
      if (testing) {
        testBtn.appendChild(el('span', { class: 'spinner' }));
        testBtn.appendChild(document.createTextNode(' Testing…'));
      } else {
        testBtn.textContent = 'Test connection';
      }
    }

    function refreshDisabled() { testBtn.disabled = !keyInput.value.trim(); }
    keyInput.addEventListener('input', () => { refreshDisabled(); continueBtn.classList.add('hidden'); clearStatus(); });
    addrInput.addEventListener('input', () => { continueBtn.classList.add('hidden'); clearStatus(); });
    refreshDisabled();

    testBtn.addEventListener('click', async () => {
      // Point api.js at the typed address for the Test. Same-origin -> '' so the
      // happy path keeps working if the user is served by the bridge itself.
      const typed = addrInput.value.trim();
      app.api.setBaseUrl(typed === location.origin ? '' : typed);

      // Temporarily set the key so testConnection()'s /printers call carries it.
      // We REVERT on failure — the key is only persisted on success.
      const prevKey = app.getKey();
      app.setKey(keyInput.value.trim());

      testBtn.disabled = true;
      setTestLabel(true);
      setStatus('', 'info');

      let res;
      try {
        res = await app.api.testConnection();
      } catch (e) {
        res = { outcome: 'bridge_error', detail: { status: 0 } };
      }

      setTestLabel(false);

      if (res.outcome === 'connected') {
        const n = res.printerCount || 0;
        setStatus(n ? STATUS_COPY.connected(n) : STATUS_COPY.connected0, 'ok');
        // Key is correct -> it stays persisted (we set it above). Remember the
        // printer list so Step 2 can be skipped / dashboard can land on one.
        if (n && res.printers && res.printers[0]) {
          const first = res.printers[0];
          app.setCurrentPrinterId(first.printer_id || first.serial);
        }
        // store the authoritative list for downstream screens
        if (app.store && typeof app.store.setPrinterList === 'function') {
          app.store.setPrinterList(res.printers || []);
        }
        continueBtn.classList.remove('hidden');
        continueBtn.className = 'btn btn--primary btn--block mt-3';
        const hasPrinter = n > 0;
        continueBtn.textContent = hasPrinter ? 'Continue to dashboard' : 'Continue';
        continueBtn.onclick = () => {
          if (hasPrinter) finishToDashboard();
          else go(renderStep2);
        };
        return;
      }

      // failure: revert to the previous key (never store a bad one)
      app.setKey(prevKey);
      testBtn.disabled = false;
      continueBtn.classList.add('hidden');

      if (res.outcome === 'not_configured') {
        // surface the setup steps inline by opening the disclosure
        helpDisclosure.open = true;
        setStatus(STATUS_COPY.not_configured, STATUS_KIND.not_configured);
        return;
      }
      if (res.outcome === 'bridge_error') {
        const code = (res.detail && res.detail.status) || 500;
        setStatus(STATUS_COPY.bridge_error(code), STATUS_KIND.bridge_error);
        return;
      }
      const copy = STATUS_COPY[res.outcome];
      setStatus(typeof copy === 'function' ? copy() : (copy || 'Could not connect.'),
        STATUS_KIND[res.outcome] || 'err');
    });

    function finishToDashboard() {
      app.navigate('#/');
      if (!app.tourSeen()) app.startTour();
    }

    stepHost.appendChild(el('div', { class: 'card mt-4' }, [
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Bridge address' }),
        addrInput,
      ]),
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'API key' }),
        el('div', { class: 'input-wrap' }, [keyInput, eye]),
        helpDisclosure,
      ]),
      testBtn,
      statusLine,
      continueBtn,
    ]));

    keyInput.focus();
  }

  // ── Step 2: Register your first printer ──────────────────────────────────
  function renderStep2() {
    stepHost.appendChild(stepRail(1));
    stepHost.appendChild(el('div', { class: 't-display', style: { fontSize: '28px' }, text: 'Add your printer' }));

    const form = renderPrinterForm(app, {
      mode: 'add',
      submitLabel: 'Register',
      busyLabel: 'Registering…',
      busySubline: 'Reaching the printer… checking the access code… waiting for first status.',
      showLanCallout: true,
      onSubmit: async (values) => app.api.postJson('/printers', values),
      onResult: (res) => handleRegisterResult(res, form),
    });

    const card = el('div', { class: 'card mt-4' }, [form.node]);
    stepHost.appendChild(card);

    // back to Step 1 (allowed except mid-call; the form disables itself then)
    const back = el('button', {
      class: 'btn btn--ghost btn--block mt-3',
      text: 'Back',
      onClick: () => { if (!form.isBusy()) go(renderStep1); },
    });
    stepHost.appendChild(back);
  }

  /**
   * Map the POST /printers result to the §1 inline forks. The form stays
   * editable; errors render as an inline error card beneath it.
   */
  function handleRegisterResult(res, form) {
    if (res.ok && res.status === 201) {
      const d = res.data || {};
      go(() => renderStep3({
        friendlyName: d.friendly_name || form.getValues().friendly_name || 'Your printer',
        model: d.model || 'P1S',
      }));
      return;
    }

    // 409 already registered -> a shortcut, not an error
    if (res.status === 409 && res.existing_printer_id) {
      form.showError(res, [{
        label: 'Open it', primary: true, onClick: () => {
          app.setCurrentPrinterId(res.existing_printer_id);
          app.navigate('#/');
        },
      }]);
      return;
    }

    // E1 — printer_unreachable (502, transport_phase tls_handshake)
    if (res.error === 'printer_unreachable') {
      form.showError(res, [{
        label: 'Edit IP & try again', primary: true,
        onClick: () => { form.clearError(); form.focusField('host'); },
      }]);
      return;
    }

    // E2 — printer_auth_failed (403). NOT a bridge-key failure (keyed off enum,
    // so the interceptor never bounced us). Reassure from discovered.serial.
    if (res.error === 'printer_auth_failed') {
      const serial = res.discovered && res.discovered.serial;
      form.showError(res, [{
        label: 'Edit access code & try again', primary: true,
        onClick: () => { form.clearError(); form.focusField('access_code'); },
      }], serial ? 'We did reach your printer.' : null);
      return;
    }

    // E3 — mqtt_no_telemetry (502). Reachable + code OK -> plain retry.
    if (res.error === 'mqtt_no_telemetry') {
      form.showError(res, [{
        label: 'Try again', primary: true,
        onClick: () => { form.clearError(); form.submit(); },
      }]);
      return;
    }

    // 422 invalid_input (bad IP, missing field, sent serial) -> issues inline,
    // highlight the offending field when derivable.
    if (res.status === 422) {
      form.showError(res);
      form.highlightFromIssues(res.issues);
      return;
    }

    // anything else (incl. 401 handled by the interceptor already): generic card
    form.showError(res, [{
      label: 'Try again', primary: true,
      onClick: () => { form.clearError(); form.submit(); },
    }]);
  }

  // ── Step 3: Success -> land on dashboard ─────────────────────────────────
  function renderStep3(info) {
    stepHost.appendChild(stepRail(2));
    stepHost.appendChild(el('div', { class: 'card mt-4 center' }, [
      el('div', { class: 't-display', style: { fontSize: '28px' }, text: "You're set up." }),
      el('div', { class: 't-body mt-3', text: `${info.friendlyName} · ${info.model || 'P1S'}` }),
      el('div', { class: 'dim mt-2' }, [
        el('span', { class: 'spinner' }),
        document.createTextNode(' Watching for live status…'),
      ]),
      el('button', {
        class: 'btn btn--primary btn--block mt-4',
        text: 'Open dashboard',
        onClick: land,
      }),
    ]));

    let landed = false;
    function land() {
      if (landed) return; landed = true;
      app.navigate('#/');
      if (!app.tourSeen()) app.startTour();
    }
    const t = setTimeout(land, 1500);
    return () => clearTimeout(t);
  }

  return function unmount() {
    if (typeof teardown === 'function') { try { teardown(); } catch { /* */ } }
  };
}

// ── the shared printer add/edit form (Settings §6.1 reuses this) ────────────
/**
 * Build the printer registration / edit form. Returns a controller the caller
 * drives. The form NEVER collects a serial (the bridge derives it from the TLS
 * cert; sending `serial` is a 422 per contract §3.1).
 *
 * @param {any} app  the shared services object
 * @param {object} opts
 * @param {'add'|'edit'} [opts.mode='add']
 * @param {{friendly_name?:string,host?:string,access_code?:string}} [opts.initial]
 * @param {string} [opts.submitLabel]
 * @param {string} [opts.busyLabel]
 * @param {string} [opts.busySubline]   live-probe sub-line under the busy button
 * @param {boolean} [opts.showLanCallout]  show the LAN-Only Mode prerequisite up front
 * @param {(values:object)=>Promise<import('./api.js').ApiResult>} opts.onSubmit
 * @param {(result:import('./api.js').ApiResult)=>void} [opts.onResult]
 * @returns {{
 *   node:HTMLElement, getValues:()=>object, submit:()=>void, isBusy:()=>boolean,
 *   setBusy:(b:boolean)=>void, showError:(r,actions?,reassurance?)=>void,
 *   clearError:()=>void, focusField:(name:string)=>void,
 *   highlightFromIssues:(issues?:Array)=>void,
 * }}
 */
export function renderPrinterForm(app, opts = {}) {
  const mode = opts.mode || 'add';
  const initial = opts.initial || {};
  const isEdit = mode === 'edit';

  const nameInput = el('input', {
    class: 'input', type: 'text', placeholder: 'P1S',
    autocapitalize: 'words', value: initial.friendly_name || '',
  });
  const hostInput = el('input', {
    class: 'input', type: 'text', inputmode: 'decimal', placeholder: '192.168.1.42',
    autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false',
    value: initial.host || initial.ip || '',
  });
  const codeInput = el('input', {
    // The P1S access code is 8 numeric digits (the bridge enforces ^\d{8}$),
    // so a numeric inputmode shows the number pad on mobile.
    class: 'input input--mono', type: 'password',
    inputmode: 'numeric', pattern: '[0-9]*',
    autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off',
    spellcheck: 'false', maxlength: '8',
    value: initial.access_code || '',
  });
  const codeEye = el('button', { class: 'input-eye', type: 'button', title: 'Show or hide code', text: '👁' });
  codeEye.addEventListener('click', () => {
    codeInput.type = codeInput.type === 'password' ? 'text' : 'password';
  });

  const fields = { friendly_name: nameInput, host: hostInput, access_code: codeInput };

  const nameField = el('div', { class: 'field' }, [
    el('label', { class: 'field__label', text: 'Printer name' }), nameInput,
  ]);
  const hostField = el('div', { class: 'field' }, [
    el('label', { class: 'field__label', text: 'Printer IP address' }), hostInput,
  ]);
  const codeField = el('div', { class: 'field' }, [
    el('label', { class: 'field__label', text: 'Access code  (8 digits)' }),
    el('div', { class: 'input-wrap' }, [codeInput, codeEye]),
    el('details', { class: 'disclosure' }, [
      el('summary', { text: "Where's the access code?" }),
      el('div', { class: 'disclosure__body', text: ACCESS_CODE_HELP }),
    ]),
  ]);

  // LAN-Only Mode prerequisite, shown BEFORE submit (errors.py lists it first).
  const lanCallout = opts.showLanCallout ? el('div', {
    class: 'card mb-4',
    style: { background: 'var(--surface)', borderColor: 'var(--panel-edge)' },
  }, [
    el('div', { class: 'row' }, [
      el('span', { style: { fontSize: '16px' }, text: 'ⓘ' }),
      el('span', { class: 'grow t-body', text: 'Before you tap Register:' }),
    ]),
    el('div', { class: 'dim mt-2', text:
      'Enable LAN-Only Mode on the P1S and Developer Mode if your firmware offers it. Use the access code shown on your printer. LAN-only operation disconnects the printer from Bambu Cloud.' }),
  ]) : null;

  const errorHost = el('div', {});
  const submitBtn = el('button', {
    class: 'btn btn--primary btn--block mt-2',
    text: opts.submitLabel || (isEdit ? 'Save changes' : 'Register'),
  });
  const busySubline = el('div', { class: 'dim mt-2 center hidden', text: opts.busySubline || '' });

  let busy = false;

  function getValues() {
    const v = {};
    const name = nameInput.value.trim();
    const host = hostInput.value.trim();
    const code = codeInput.value.trim();
    if (name) v.friendly_name = name;
    // edit sends only what changed; add sends host+access_code required.
    if (isEdit) {
      if (host) v.ip = host;          // PATCH /printers/{id} uses `ip` (contract §3)
      if (code) v.access_code = code;
    } else {
      v.host = host;
      v.access_code = code;
    }
    return v;
  }

  function setBusy(b) {
    busy = b;
    submitBtn.disabled = b;
    nameInput.disabled = b; hostInput.disabled = b; codeInput.disabled = b;
    while (submitBtn.firstChild) submitBtn.removeChild(submitBtn.firstChild);
    if (b) {
      submitBtn.appendChild(el('span', { class: 'spinner' }));
      submitBtn.appendChild(document.createTextNode(' ' + (opts.busyLabel || 'Working…')));
      busySubline.classList.toggle('hidden', !opts.busySubline);
    } else {
      submitBtn.textContent = opts.submitLabel || (isEdit ? 'Save changes' : 'Register');
      busySubline.classList.add('hidden');
    }
  }

  function clearError() { while (errorHost.firstChild) errorHost.removeChild(errorHost.firstChild); clearHighlights(); }

  function clearHighlights() {
    for (const inp of Object.values(fields)) inp.style.borderColor = '';
  }

  function focusField(name) {
    const inp = fields[name];
    if (inp) { inp.focus(); inp.style.borderColor = 'var(--state-failed)'; }
  }

  function highlightFromIssues(issues) {
    if (!Array.isArray(issues)) return;
    for (const issue of issues) {
      const loc = typeof issue === 'string' ? issue : (issue && issue.loc) || '';
      const s = String(loc);
      if (/host|\bip\b/i.test(s)) hostInput.style.borderColor = 'var(--state-failed)';
      if (/access_code/i.test(s)) codeInput.style.borderColor = 'var(--state-failed)';
      if (/friendly_name/i.test(s)) nameInput.style.borderColor = 'var(--state-failed)';
    }
  }

  function showError(result, actions, reassurance) {
    clearError();
    errorHost.appendChild(errorCard(result, { actions: actions || [], reassurance }));
  }

  function clientValidate() {
    // friendly, pre-network checks so the user isn't billed a 5s probe for a
    // blank field (the bridge still range/format-validates server-side).
    if (!isEdit) {
      if (!hostInput.value.trim()) {
        focusField('host');
        showError({ message: 'Enter the printer’s IP address.',
          remediation_hint: 'It’s on the printer under Settings ▸ WLAN ▸ IP.' });
        return false;
      }
      if (!codeInput.value.trim()) {
        focusField('access_code');
        showError({ message: 'Enter the 8-character access code.',
          remediation_hint: ACCESS_CODE_HELP });
        return false;
      }
    } else if (!hostInput.value.trim() && !codeInput.value.trim() && !nameInput.value.trim()) {
      showError({ message: 'Nothing to change.' });
      return false;
    }
    return true;
  }

  async function submit() {
    if (busy) return;
    clearError();
    if (!clientValidate()) return;
    setBusy(true);
    let res;
    try {
      res = await opts.onSubmit(getValues());
    } catch (e) {
      res = { ok: false, status: 0, network: true,
        message: "Can't reach the bridge.", _netErr: String(e) };
    }
    setBusy(false);
    if (opts.onResult) opts.onResult(res);
    else if (!res.ok) showError(res);
  }

  submitBtn.addEventListener('click', submit);
  // Enter in the access-code field submits (the last field).
  codeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });

  const node = el('div', {}, [
    lanCallout,
    nameField,
    hostField,
    codeField,
    errorHost,
    submitBtn,
    busySubline,
  ]);

  return {
    node, getValues, submit,
    isBusy: () => busy,
    setBusy, showError, clearError, focusField, highlightFromIssues,
  };
}

// keep a named export available even if a consumer only wants the toast helper
export { toast };
