// Stock OrcaSlicer print-host setup. The one-time key lives only in this view.
import { el, clear, confirmSheet, toast } from './ui.js';

export function mountOrca(parent, app) {
  const section = el('section', { class: 'section', id: 'orca-setup' }, [
    el('h2', { class: 't-section', text: 'OrcaSlicer' }),
  ]);
  const card = el('div', { class: 'card mt-2 pairing-card' });
  section.appendChild(card);
  parent.appendChild(section);
  const base = app.api.apiBase();
  const owner = app.getKey();
  let alive = true;
  const current = () => alive && base === app.api.apiBase() && owner === app.getKey();
  if (location.protocol !== 'https:' || new URL(base, location.href).protocol !== 'https:') {
    card.appendChild(el('p', { text: 'Open the dashboard over trusted HTTPS to connect OrcaSlicer.' }));
    return () => { alive = false; };
  }
  card.appendChild(el('p', { text: 'Send a sliced plate through this server using Orca’s Octo/Klipper print host. Works with Orca 2.4.2; no replacement networking DLL is needed.' }));
  card.appendChild(el('p', { class: 'field__hint', text: 'Preview: one sliced plate at position 1. Camera, live 3D, controls and filament selection remain in the bridge dashboard. Orca’s native AMS sync is not connected.' }));
  const printers = el('select', { class: 'select', id: 'orca-printer' });
  const name = el('input', { class: 'input', id: 'orca-name', value: 'OrcaSlicer desktop', maxlength: '64' });
  const mode = el('select', { class: 'select', id: 'orca-mode' }, [
    el('option', { value: 'upload', text: 'Upload only' }),
    el('option', { value: 'ams', text: 'Print with AMS' }),
    el('option', { value: 'external', text: 'Print with external spool' }),
  ]);
  const mapping = el('input', { class: 'input', id: 'orca-mapping', placeholder: '0,1,2,3', disabled: true });
  const field = (label, id, input) => el('div', { class: 'field mt-2' }, [
    el('label', { class: 'field__label', for: id, text: label }), input,
  ]);
  card.append(field('Printer', 'orca-printer', printers), field('Connection name', 'orca-name', name),
    field('Permission', 'orca-mode', mode), field('AMS slots in slicer filament order', 'orca-mapping', mapping));
  card.appendChild(el('p', { class: 'field__hint', text: 'AMS slots are zero based: 0 is slot 1, 1 is slot 2. For one slicer filament using physical slot 2, enter 1. The mapping is fixed for this key; create a new key when it changes.' }));
  const modeHint = el('p', { class: 'field__hint', text: 'Upload only saves the file to the SD card. Start it later at the printer.' });
  mode.after(modeHint);
  mode.addEventListener('change', () => {
    mapping.disabled = mode.value !== 'ams';
    modeHint.textContent = mode.value === 'upload' ? 'Upload only saves the file to the SD card. Start it later at the printer.'
      : mode.value === 'ams' ? 'Upload and print uses the fixed mapping below. Check the loaded materials before sending a plate.'
        : 'Upload and print uses one external spool. Multi-filament slices are rejected.';
  });
  const create = el('button', { class: 'btn btn--primary btn--block mt-3', text: 'Create Orca connection', disabled: true });
  const status = el('p', { class: 'statusline', role: 'status', 'aria-live': 'polite' });
  const output = el('div', { class: 'stack' });
  const clients = el('div', { class: 'stack mt-3' });
  card.append(create, status, output, el('h3', { class: 'mt-4', text: 'Slicer connections' }), clients);
  function message(text, error = false) {
    status.textContent = text;
    status.className = 'statusline ' + (error ? 'is-err' : 'is-info');
  }
  function copyField(label, value, secret = false) {
    const input = el('input', { class: 'input input--mono', readonly: '', type: secret ? 'password' : 'text',
      'aria-label': label, autocomplete: 'off' });
    input.value = value;
    const copy = el('button', { class: 'btn btn--ghost btn--sm', text: `Copy ${label}` });
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(input.value); toast(`${label} copied.`); }
      catch { input.type = 'text'; input.focus(); input.select(); }
    });
    return el('div', { class: 'field' }, [el('label', { class: 'field__label', text: label }), input, copy]);
  }
  async function loadClients() {
    const res = await app.api.api('/orca/clients', { cache: 'no-store' });
    if (!current()) return;
    clear(clients);
    if (!res.ok) { message(res.message || 'Could not load slicer connections.', true); return; }
    for (const row of res.data.filter((c) => !c.revoked)) {
      const revoke = el('button', { class: 'btn btn--ghost btn--sm', text: 'Revoke slicer key' });
      revoke.addEventListener('click', async () => {
        const yes = await confirmSheet({ title: `Revoke ${row.name}?`,
          body: 'Orca will need a new key to send more plates. A print already submitted will continue.',
          confirmLabel: 'Revoke key', danger: true });
        if (!yes || !current()) return;
        const result = await app.api.del(`/orca/clients/${row.id}`, { cache: 'no-store' });
        if (!current()) return;
        if (result.ok) { clear(output); await loadClients(); }
        else message(result.message || 'Could not revoke key.', true);
      });
      const access = row.ams_mapping === null ? 'Upload only' : row.ams_mapping.length
        ? `Print · AMS ${row.ams_mapping.join(', ')}` : 'Print · external spool';
      clients.appendChild(el('div', { class: 'card' }, [
        el('strong', { text: row.name }), el('p', { class: 'field__hint', text: `${row.printer_id} · ${access}` }), revoke,
      ]));
    }
    if (!clients.children.length) clients.appendChild(el('p', { text: 'No slicer connections yet.' }));
  }
  create.addEventListener('click', async () => {
    let ams = null;
    if (mode.value === 'ams') {
      if (!/^\d+(\s*,\s*\d+)*$/.test(mapping.value.trim())) {
        message('Enter AMS slot numbers separated by commas.', true); return;
      }
      ams = mapping.value.split(',').map(Number);
    } else if (mode.value === 'external') ams = [];
    if (!name.value.trim()) { message('Give the connection a name.', true); return; }
    create.disabled = true;
    clear(output);
    const res = await app.api.postJson('/orca/clients', {
      name: name.value.trim(), printer_id: printers.value, ams_mapping: ams,
    }, { cache: 'no-store' });
    if (!current()) return;
    create.disabled = false;
    if (!res.ok) { message(res.message || 'Could not create connection.', true); return; }
    const origin = new URL(base, location.href).origin;
    output.append(copyField('Host URL', origin + res.data.host_path), copyField('API key', res.data.token, true),
      copyField('Device UI URL', origin + '/app'));
    output.appendChild(el('ol', {}, [
      el('li', { text: 'Edit your Bambu printer preset. Turn on Advanced → Basic information → Use 3rd-party print host. Save it as a separate Bridge preset.' }),
      el('li', { text: 'Open the connection / Wi-Fi icon. Choose Octo/Klipper, paste the Host URL and API key, then click Test.' }),
      el('li', { text: 'Use the Device UI URL for monitoring. Keep certificate verification enabled; use a trusted HTTPS address such as Tailscale Serve.' }),
      el('li', { text: 'Slice plate 1 and choose Upload, or Upload and print if you enabled that permission. Leave the upload folder empty.' }),
    ]));
    message('Copy this key now; it is shown only once. Keep exported Orca presets containing the key private.');
    output.scrollIntoView({ block: 'center' });
    await loadClients();
  });
  Promise.all([app.api.api('/printers'), app.api.api('/orca/clients', { cache: 'no-store' })]).then(([ps, cs]) => {
    if (!current()) return;
    if (!ps.ok || !cs.ok) { message(cs.message || ps.message || 'Owner access is required.', true); return; }
    for (const p of ps.data) printers.appendChild(el('option', { value: p.printer_id, text: p.friendly_name || p.printer_id }));
    create.disabled = !printers.options.length;
    if (!printers.options.length) message('Add a printer below first, then reopen Settings.');
    loadClients();
  });
  return () => { alive = false; clear(output); };
}
