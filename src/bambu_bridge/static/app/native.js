import { el, clear, toast, confirmSheet } from './ui.js';

export function mountNative(parent, app) {
  const section = el('section', { class: 'section', id: 'native-p1s' }, [
    el('h2', { class: 't-section', text: 'Orca · native P1S' }),
  ]);
  const card = el('div', { class: 'card mt-2 pairing-card' });
  section.appendChild(card); parent.appendChild(section);
  const base = app.api.apiBase(), owner = app.getKey();
  let alive = true, enabled = false;
  const current = () => alive && owner === app.getKey() && base === app.api.apiBase();
  card.appendChild(el('p', { text: 'Use Orca’s P1S Device page, live camera and normal print dialog. Choose AMS slots or the external spool for each print; there is no fixed filament mapping in this connection.' }));
  if (location.protocol !== 'https:' || new URL(base).protocol !== 'https:') {
    card.appendChild(el('p', { text: 'Open this dashboard over HTTPS to configure native access.' }));
    return () => { alive = false; };
  }
  const printer = el('select', { class: 'select', id: 'native-printer' });
  const toggle = el('input', { type: 'checkbox', id: 'native-enabled', disabled: true });
  const status = el('p', { class: 'statusline', role: 'status' });
  const output = el('div', { class: 'stack mt-3' });
  const rotate = el('button', { class: 'btn btn--ghost mt-3', text: 'Replace native access code', disabled: true });
  card.append(el('label', { class: 'field__label', for: 'native-printer', text: 'Printer' }), printer,
    el('label', { class: 'native-toggle mt-3', for: 'native-enabled' }, [toggle,
      el('strong', { text: 'Expose to Orca as a P1S' })]), status, output, rotate);
  card.appendChild(el('p', { class: 'field__hint mt-3', text: 'This is full native printer access over your private LAN/Tailscale connection. The separate 8-character code controls only this gateway. Disabling it disconnects native clients; a print already running continues.' }));
  const diagnostics = el('p', { class: 'field__hint', role: 'status' });
  const check = el('button', { class: 'btn btn--ghost mt-3', text: 'Check Orca connection' });
  card.append(check, diagnostics);
  check.addEventListener('click', async () => {
    check.disabled = true;
    const res = await app.api.api('/native', { cache: 'no-store' });
    if (!current()) return;
    check.disabled = false;
    if (!res.ok) { diagnostics.textContent = res.message || 'Could not check connection.'; return; }
    const phases = { tls_connected: 'secure connection opened', authenticated: 'code accepted',
      access_code_rejected: 'native access code rejected', disconnected: 'connection ended',
      timeout: 'connection timed out', protocol_error: 'protocol error',
      unsupported_mqtt_version: 'unsupported MQTT version', streaming: 'camera frames flowing' };
    const entries = Object.entries(res.data.connections || {});
    diagnostics.textContent = entries.length ? entries.map(([name, info]) =>
      `${name.toUpperCase()}: ${phases[info.phase] || 'waiting'} (${info.tls_connections} secure connections, ${info.auth_failures} rejected codes).`).join(' ')
      : 'No completed secure connection since the server started. Check the address and private network connection, then try Connect in Orca again.';
  });
  function message(text, error = false) { status.textContent = text; status.className = 'statusline ' + (error ? 'is-err' : 'is-info'); }
  function showSetup(data) {
    clear(output);
    for (const [label, value] of [['Printer address', data.host], ['Printer serial', data.printer_id],
      ...(data.access_code ? [['Native access code', data.access_code]] : [])]) {
      const input = el('input', { class: 'input input--mono', readonly: '', 'aria-label': label,
        type: label === 'Native access code' ? 'password' : 'text', autocomplete: 'off' });
      input.value = value;
      const copy = el('button', { class: 'btn btn--ghost btn--sm', text: `Copy ${label.toLowerCase()}` });
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(input.value); toast(`${label} copied.`); }
        catch { input.type = 'text'; input.focus(); input.select(); }
      });
      output.appendChild(el('div', { class: 'field' }, [el('label', { class: 'field__label', text: label }), input, copy]));
    }
    output.appendChild(el('ol', {}, [
      el('li', { text: 'In Orca, use a Bambu Lab P1S preset with “Use 3rd-party print host” turned OFF.' }),
      el('li', { text: 'Add/connect a LAN printer in the Device page using this server address, the P1S model, printer serial and native access code.' }),
      el('li', { text: 'Slice and open Print plate. Select the live AMS slots or choose the external spool. View the camera in Device.' }),
    ]));
    const find = el('button', { class: 'btn btn--primary', text: 'Find in Orca on this computer' });
    find.addEventListener('click', async () => {
      find.disabled = true;
      const res = await app.api.postJson('/native/announce', {}, { cache: 'no-store' });
      if (!current()) return;
      find.disabled = false;
      message(res.ok ? 'Discovery sent. Open Orca’s printer list and select Bridge P1S. Use the native access code above.' : (res.message || 'Discovery failed. Use the server address to connect manually.'), !res.ok);
    });
    output.appendChild(find);
    if (data.access_code) output.appendChild(el('p', { class: 'field__hint', text: 'Copy the code now; it is shown once. This is the bridge code, not the printer’s original access code.' }));
    output.appendChild(el('p', { class: 'field__hint', text: 'The physical P1S still determines which material combinations it supports. Automatic mixing of AMS and an external spool is not added by this gateway.' }));
  }
  async function enable() {
    toggle.disabled = true; rotate.disabled = true; clear(output);
    message('Starting native P1S access…');
    const res = await app.api.postJson('/native', { printer_id: printer.value }, { cache: 'no-store' });
    if (!current()) return;
    enabled = !!res.ok; toggle.checked = enabled; toggle.disabled = false; rotate.disabled = !enabled;
    printer.disabled = enabled;
    if (!res.ok) { message(res.message || 'Could not start native access.', true); return; }
    showSetup(res.data); message('Native P1S access is on. Connect Orca using the fields below.');
    output.scrollIntoView({ block: 'center' });
  }
  toggle.addEventListener('change', async () => {
    if (toggle.checked) { await enable(); return; }
    toggle.disabled = true;
    const res = await app.api.del('/native', { cache: 'no-store' });
    if (!current()) return;
    if (res.ok) { enabled = false; clear(output); message('Native P1S access is off.'); }
    else message(res.message || 'Could not disable native access.', true);
    toggle.checked = enabled; toggle.disabled = false; rotate.disabled = !enabled; printer.disabled = enabled;
  });
  rotate.addEventListener('click', async () => {
    if (await confirmSheet({ title: 'Replace native access code?', body: 'Connected native clients will disconnect and need the new code.', confirmLabel: 'Replace code' }) && current()) await enable();
  });
  Promise.all([app.api.api('/native', { cache: 'no-store' }), app.api.api('/printers')]).then(([state, ps]) => {
    if (!current()) return;
    if (!state.ok || !ps.ok) { message(state.message || ps.message || 'Owner access is required.', true); return; }
    for (const p of ps.data) if (!p.model || /p1s|c12/i.test(p.model)) printer.appendChild(el('option', { value: p.printer_id, text: p.friendly_name || 'P1S' }));
    if (!state.data.configured) { message('Set BRIDGE_NATIVE_HOST on the server to its private/Tailscale IPv4 address first.'); return; }
    enabled = state.data.enabled; toggle.checked = enabled; printer.disabled = enabled;
    toggle.disabled = !printer.options.length; rotate.disabled = !enabled;
    if (state.data.printer_id) printer.value = state.data.printer_id;
    if (enabled) { showSetup(state.data); message('Native P1S access is on. Your existing native code still works.'); }
    else message(state.data.error || 'Turn on native P1S access to connect Orca.');
  });
  return () => { alive = false; clear(output); };
}
