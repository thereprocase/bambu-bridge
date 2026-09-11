// Owner-only phone enrollment. Invitations stay in memory, never in storage or URLs.
import { el, clear, confirmSheet, toast } from './ui.js';

export function mountPhonePairing(parent, app) {
  const section = el('section', { class: 'section', id: 'phone-pairing' }, [
    el('h2', { class: 't-section', text: 'Phones & pairing' }),
  ]);
  const card = el('div', { class: 'card mt-2 pairing-card' });
  section.appendChild(card);
  parent.appendChild(section);
  const key = app.getKey();
  const base = app.api.apiBase();
  let alive = true;
  let invitation = null;
  let imageUrl = null;
  let polling = false;
  let nextPoll = 0;
  let timer = null;
  const sameConfig = () => app.getKey() === key && app.api.apiBase() === base;
  const current = () => alive && sameConfig();
  const status = el('p', { class: 'statusline', role: 'status', 'aria-live': 'polite' });
  const qrSlot = el('div', { class: 'pairing-code' });
  const devices = el('div', { class: 'stack mt-3' });
  const select = el('select', { class: 'select', id: 'pairing-network' });
  const hint = el('p', { class: 'field__hint mt-2' });
  const create = el('button', { class: 'btn btn--primary btn--block', text: 'Pair a phone', disabled: true });
  const refresh = el('button', { class: 'btn btn--ghost btn--sm', text: 'Refresh devices' });

  function message(text, error = false) {
    status.textContent = text;
    status.className = 'statusline ' + (error ? 'is-err' : 'is-info');
  }
  function hideCode() {
    const old = invitation;
    invitation = null;
    clear(qrSlot);
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    create.textContent = 'Pair a phone';
    return old;
  }
  async function cancelCode() {
    const old = hideCode();
    if (!old || !sameConfig()) return true;
    const res = await app.api.del(`/pairing/invitations/${old.id}`, { cache: 'no-store', keepalive: true });
    if (!res.ok && current()) message('The code is hidden but could not be cancelled. It will expire shortly.', true);
    return res.ok;
  }
  async function loadDevices() {
    if (!current()) return;
    refresh.disabled = true;
    const res = await app.api.api('/pairing/devices', { cache: 'no-store' });
    if (!current()) return;
    refresh.disabled = false;
    clear(devices);
    if (!res.ok) {
      devices.appendChild(el('p', { class: 'statusline is-err', text: res.message || 'Could not load paired devices.' }));
      return;
    }
    const list = Array.isArray(res.data) ? res.data : [];
    const active = list.filter((device) => !device.revoked);
    if (!active.length) devices.appendChild(el('p', { class: 'dim', text: 'No phones are paired yet.' }));
    for (const device of active) {
      const revoke = el('button', { class: 'btn btn--danger btn--sm', text: 'Revoke access' });
      revoke.addEventListener('click', async () => {
        const approved = await confirmSheet({ title: `Revoke access for ${device.name}?`,
          body: 'This phone will disconnect and must pair again to reconnect.', confirmLabel: 'Revoke access', danger: true });
        if (!approved || !current()) return;
        revoke.disabled = true;
        const result = await app.api.del(`/pairing/devices/${encodeURIComponent(device.id)}`, { cache: 'no-store' });
        if (!current()) return;
        if (result.ok || result.status === 404) { toast('Phone access revoked.'); await loadDevices(); }
        else { revoke.disabled = false; message(result.message || 'Could not revoke access.', true); }
      });
      devices.appendChild(el('div', { class: 'pairing-device' }, [
        el('div', { class: 'pairing-device__name' }, [
          el('strong', { text: device.name }),
          el('div', { class: 'field__hint', text: `Paired ${new Date(device.created * 1000).toLocaleString()}` }),
        ]), revoke,
      ]));
    }
    const revoked = list.filter((device) => device.revoked);
    if (revoked.length) devices.appendChild(el('details', { class: 'disclosure' }, [
      el('summary', { text: `Previously revoked (${revoked.length})` }),
      el('div', { class: 'disclosure__body' }, revoked.map((device) =>
        el('p', { text: `${device.name} — revoked ${new Date(device.revoked * 1000).toLocaleString()}` }))),
    ]));
  }
  function networkHint() {
    hint.textContent = select.value === 'remote'
      ? 'Away from home: keep Tailscale connected on the phone while pairing and using the bridge.'
      : 'At home: connect the phone to the same home network as the bridge.';
  }
  select.addEventListener('change', async () => {
    networkHint();
    if (invitation) {
      create.disabled = true;
      await cancelCode();
      if (current()) { create.disabled = false; message('Create a new code for this network.'); }
    }
  });
  create.addEventListener('click', async () => {
    if (!current()) return;
    create.disabled = true;
    select.disabled = true;
    message('Creating a private pairing code…');
    if (!await cancelCode() || !current()) {
      if (current()) { create.disabled = false; select.disabled = false; }
      return;
    }
    const res = await app.api.postJson('/pairing/invitations', { target: select.value }, { cache: 'no-store' });
    if (!current()) {
      if (res.ok && sameConfig()) app.api.del(`/pairing/invitations/${res.data.id}`, { cache: 'no-store', keepalive: true });
      return;
    }
    create.disabled = false;
    select.disabled = false;
    if (!res.ok) { message(res.message || 'Could not create a pairing code.', true); return; }
    invitation = res.data;
    create.textContent = 'Generate a new code';
    imageUrl = URL.createObjectURL(new Blob([invitation.qr_svg], { type: 'image/svg+xml' }));
    const countdown = el('p', { class: 'field__hint pairing-countdown' });
    const code = el('textarea', { class: 'input input--mono pairing-payload', readonly: '',
      rows: '5', 'aria-label': 'Pairing code', spellcheck: 'false' });
    code.value = invitation.payload;
    const copy = el('button', { class: 'btn btn--ghost btn--sm', text: 'Copy pairing code' });
    copy.addEventListener('click', async () => {
      if (!invitation || invitation.expires * 1000 <= Date.now()) return;
      try { await navigator.clipboard.writeText(invitation.payload); toast('Pairing code copied.'); }
      catch { code.focus(); code.select(); toast('Select and copy the code below.'); }
    });
    const cancel = el('button', { class: 'btn btn--ghost btn--sm', text: 'Cancel this code' });
    cancel.addEventListener('click', async () => {
      cancel.disabled = true;
      if (await cancelCode() && current()) message('Pairing code cancelled.');
    });
    qrSlot.appendChild(el('div', { class: 'pairing-qr-panel' }, [
      el('h3', { text: 'Scan with Bambu Bridge for Android' }),
      el('p', { text: 'On the phone: Settings → Pair with QR code. Scan, name the phone, and tap Pair securely.' }),
      el('img', { class: 'pairing-qr', src: imageUrl, alt: 'Single-use phone pairing QR code', width: '360', height: '360' }),
      countdown,
      el('details', { class: 'disclosure' }, [
        el('summary', { text: 'Using this page on the same phone?' }),
        el('div', { class: 'disclosure__body' }, [
          el('p', { text: 'Copy this code, then paste it into the app’s pairing screen.' }), code, copy,
        ]),
      ]), cancel,
    ]));
    message('Keep this code private. It enrolls one phone; your API key is not shared.');
    qrSlot.querySelector('img').scrollIntoView({ block: 'center' });
    nextPoll = 0;
    tick();
  });
  async function tick() {
    if (!current()) {
      hideCode();
      clearInterval(timer);
      return;
    }
    if (!invitation) return;
    const remaining = Math.max(0, Math.ceil(invitation.expires - Date.now() / 1000));
    if (!remaining) { hideCode(); message('Code expired. Generate a new code to pair.'); return; }
    qrSlot.querySelector('.pairing-countdown').textContent =
      `One use · Expires in ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
    if (polling || Date.now() < nextPoll) return;
    polling = true;
    nextPoll = Date.now() + 4000;
    const id = invitation.id;
    const res = await app.api.api(`/pairing/invitations/${id}`, { cache: 'no-store' });
    polling = false;
    if (!current() || invitation?.id !== id) return;
    if (res.ok && !res.data.active) {
      hideCode();
      message('This code has been used or cancelled. Paired phones are listed below.');
      await loadDevices();
    }
  }
  refresh.addEventListener('click', loadDevices);

  if (location.protocol !== 'https:' || new URL(base, location.href).protocol !== 'https:') {
    card.appendChild(el('p', { text: 'Open this dashboard through your HTTPS address to pair or manage phones. Your existing HTTP connection remains available for other controls.' }));
    return () => { alive = false; };
  }
  card.appendChild(el('p', { text: 'Connect a phone with a QR code. Each phone gets its own access, which you can revoke here.' }));
  card.appendChild(el('div', { class: 'field' }, [
    el('label', { class: 'field__label', for: 'pairing-network', text: 'Where will the phone connect?' }), select, hint,
  ]));
  card.append(create, status, qrSlot);
  card.appendChild(el('div', { class: 'pairing-devices-heading mt-4' }, [el('h3', { text: 'Paired phones' }), refresh]));
  card.appendChild(devices);
  app.api.api('/pairing/options', { cache: 'no-store' }).then((res) => {
    if (!current()) return;
    if (!res.ok) { message(res.message || 'Phone pairing is unavailable.', true); return; }
    for (const target of res.data.targets) select.appendChild(el('option', { value: target.id, text: target.label }));
    if (location.hostname.endsWith('.ts.net') && res.data.targets.some((target) => target.id === 'remote')) select.value = 'remote';
    networkHint();
    create.disabled = false;
    loadDevices();
  });
  timer = setInterval(tick, 1000);
  const pageHide = () => { cancelCode(); };
  window.addEventListener('pagehide', pageHide);
  return () => {
    alive = false;
    clearInterval(timer);
    window.removeEventListener('pagehide', pageHide);
    cancelCode();
  };
}
