import { el, clear, toast, confirmSheet } from './ui.js';
import { timingSummary } from './native-timings.js';
import { fileLabel, ownerLabel, startLabel } from './native-state.js';

export function mountNativeRecovery(parent, app) {
  const base = app.api.apiBase(), key = app.getKey();
  let alive = true, rows = [], hasMore = false, view = 'review', acknowledgeableCount = 0;
  const current = () => alive && base === app.api.apiBase() && key === app.getKey();
  const section = el('section', { class: 'section mt-3' }, [
    el('h2', { class: 't-section', text: 'Print activity and recovery' }),
  ]);
  const card = el('div', { class: 'card mt-2 stack' });
  const summary = el('p', { class: 'statusline', role: 'status' });
  const receipts = el('div', { class: 'stack mt-3' });
  const backups = el('div', { class: 'stack mt-3' });
  const refresh = el('button', { class: 'btn btn--ghost', text: 'Refresh receipts and backups' });
  const filters = el('div', { class: 'row gap-2', style: { flexWrap: 'wrap' } });
  const acknowledge = el('button', { class: 'btn btn--ghost', text: 'Acknowledge past warnings', hidden: true });
  const older = el('button', { class: 'btn btn--ghost', text: 'Load older receipts', hidden: true });
  const create = el('button', { class: 'btn btn--primary', text: 'Create inbox backup' });
  const importFile = el('input', { type: 'file', accept: '.zip,application/zip', 'aria-label': 'Import inbox backup' });
  card.append(
    el('p', { text: 'File transfers and print starts. A receipt is not a waiting-list item.' }),
    refresh, summary, filters, acknowledge, receipts, older,
    el('h3', { text: 'Inbox backups' }),
    el('p', { text: 'Backups contain the receipt database and complete cached files. Download a copy off the bridge. Restoring restarts native access and holds pending starts for review.' }),
    create, importFile, backups,
  );
  section.appendChild(card); parent.appendChild(section);

  function renderReceipt(upload) {
    const item = el('div', { class: 'stack mt-3' }, [
      el('p', { text: `${upload.logical || upload.id} · File: ${fileLabel(upload.state)} · Start: ${startLabel(upload.start_state)}` }),
      el('p', { class: 'field__hint', text: `${upload.printer} · ${new Date(upload.created * 1000).toLocaleString()} · ${upload.bytes} bytes · ${upload.id} · ${upload.code || 'no code'}${upload.review_ack_at ? ' · acknowledged' : ''}` }),
      el('p', { class: 'field__hint', text: timingSummary(upload) }),
    ]);
    const actions = [];
    if (['reserved', 'queued'].includes(upload.start_state))
      actions.push(['cancel', 'Cancel pending start', 'Cancel this start before it reaches the printer.']);
    if (['unknown', 'accepted', 'sent', 'running', 'blocked', 'cancelled', 'rejected'].includes(upload.start_state))
      actions.push(['resolve', 'Resolve after checking printer', 'Confirm at the printer that this print is not running or about to start.']);
    if (upload.state === 'failed' && upload.complete)
      actions.push(['retry_delivery', 'Retry file delivery', 'Send the saved file again. A blocked print start will not be sent automatically.']);
    if (['failed', 'delivered', 'external'].includes(upload.state) && upload.retained)
      actions.push(['discard', 'Discard local file', 'Delete only the bridge cached copy, not the printer file.']);
    for (const [action, label, body] of actions) {
      const button = el('button', { class: 'btn btn--ghost', text: label });
      button.addEventListener('click', async () => {
        if (!await confirmSheet({ title: label + '?', body, confirmLabel: label }) || !current()) return;
        button.disabled = true;
        const result = await app.api.postJson('/native/uploads/' + upload.id,
          { action, confirm: 'I checked the printer and this action' });
        if (!current()) return;
        if (!result.ok) toast(result.message || 'Recovery action was refused.');
        await load();
      });
      item.appendChild(button);
    }
    return item;
  }

  function renderReceipts() {
    clear(receipts);
    if (!rows.length) receipts.appendChild(el('p', { text: view === 'review' ? 'No receipts need review.' : view === 'active' ? 'Nothing in progress.' : 'No native receipts yet.' }));
    for (const row of rows) receipts.appendChild(renderReceipt(row));
    older.hidden = !hasMore;
  }

  async function loadQueue() {
    const requestedView = view;
    const result = await app.api.api(`/native/queue?limit=100&offset=0&view=${requestedView}`, { cache: 'no-store' });
    if (!current() || view !== requestedView) return;
    if (!result.ok) { summary.textContent = result.message || 'Native inbox unavailable.'; return; }
    rows = result.data.uploads;
    hasMore = result.data.has_more;
    summary.textContent = `${ownerLabel(result.data)}. ${result.data.review_count} receipts need review.`;
    acknowledgeableCount = result.data.acknowledgeable_count;
    acknowledge.hidden = !acknowledgeableCount;
    acknowledge.textContent = `Acknowledge past warnings (${acknowledgeableCount})`;
    renderReceipts();
  }

  for (const [id, label] of [['review', 'Needs review'], ['active', 'In progress'], ['all', 'All receipts']]) {
    const button = el('button', { class: 'btn btn--ghost', text: label, 'aria-pressed': String(view === id) });
    button.addEventListener('click', () => {
      view = id;
      for (const peer of filters.children) peer.setAttribute('aria-pressed', String(peer === button));
      rows = []; hasMore = false; renderReceipts();
      void loadQueue();
    });
    filters.appendChild(button);
  }
  acknowledge.addEventListener('click', async () => {
    const count = acknowledgeableCount;
    if (!count || !await confirmSheet({ title: 'Acknowledge past warnings?',
      body: `Hide ${count} historical warnings from Needs review. Receipts and files remain. Current or uncertain print starts are never changed.`,
      confirmLabel: 'Acknowledge past' }) || !current()) return;
    acknowledge.disabled = true;
    const result = await app.api.postJson('/native/queue/acknowledge',
      { confirm: 'Ignore past review warnings; keep active starts' });
    if (!current()) return;
    acknowledge.disabled = false;
    toast(result.ok ? `${result.data.acknowledged} past warnings acknowledged.` : (result.message || 'Could not acknowledge warnings.'));
    await loadQueue();
  });

  async function loadBackups() {
    const result = await app.api.api('/native/recovery/backups', { cache: 'no-store' });
    if (!current()) return;
    clear(backups);
    if (!result.ok) { backups.appendChild(el('p', { text: result.message || 'Backups unavailable.' })); return; }
    if (!result.data.length) backups.appendChild(el('p', { text: 'No backups saved yet.' }));
    for (const saved of result.data) {
      const item = el('div', { class: 'stack mt-3' }, [
        el('p', { text: `${new Date(saved.created * 1000).toLocaleString()} · ${saved.bytes} bytes · ${saved.id}` }),
      ]);
      const download = el('button', { class: 'btn btn--ghost', text: 'Download' });
      download.addEventListener('click', async () => {
        download.disabled = true;
        const response = await app.api.api('/native/recovery/backups/' + saved.id, { raw: true, cache: 'no-store' });
        if (!current()) return;
        download.disabled = false;
        if (!response.ok || !response.blob) { toast('Could not download backup.'); return; }
        const url = URL.createObjectURL(await response.blob());
        const link = el('a', { href: url, download: `native-inbox-${saved.id}.zip` });
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
      const restore = el('button', { class: 'btn btn--ghost', text: 'Restore' });
      restore.addEventListener('click', async () => {
        const body = 'Native access will restart. Every pending print start in this backup will require review; none will be replayed. The current inbox will be backed up first.';
        if (!await confirmSheet({ title: 'Restore native inbox?', body, confirmLabel: 'Restore' }) || !current()) return;
        restore.disabled = true;
        const result = await app.api.postJson('/native/recovery/backups/' + saved.id + '/restore',
          { confirm: 'Restore native inbox and review every pending start' });
        if (!current()) return;
        toast(result.ok ? 'Inbox restored. Review pending starts before printing.' : (result.message || 'Restore failed.'));
        await load();
      });
      const remove = el('button', { class: 'btn btn--ghost', text: 'Delete backup' });
      remove.addEventListener('click', async () => {
        if (!await confirmSheet({ title: 'Delete inbox backup?', body: 'This removes the saved backup from the bridge. Keep an off-device copy if you may need it later.', confirmLabel: 'Delete' }) || !current()) return;
        remove.disabled = true;
        const result = await app.api.del('/native/recovery/backups/' + saved.id);
        if (!current()) return;
        toast(result.ok ? 'Backup deleted.' : (result.message || 'Could not delete backup.'));
        await loadBackups();
      });
      item.append(download, restore, remove); backups.appendChild(item);
    }
  }

  async function load() {
    refresh.disabled = true;
    await Promise.all([loadQueue(), loadBackups()]);
    if (current()) refresh.disabled = false;
  }
  refresh.addEventListener('click', load);
  older.addEventListener('click', async () => {
    older.disabled = true;
    const requestedView = view;
    const result = await app.api.api(`/native/queue?limit=100&offset=${rows.length}&view=${requestedView}`, { cache: 'no-store' });
    older.disabled = false;
    if (!current() || view !== requestedView) return;
    if (!result.ok) { toast(result.message || 'Could not load older receipts.'); return; }
    rows.push(...result.data.uploads); hasMore = result.data.has_more; renderReceipts();
  });
  create.addEventListener('click', async () => {
    create.disabled = true;
    const result = await app.api.postJson('/native/recovery/backups', {});
    if (!current()) return;
    create.disabled = false;
    toast(result.ok ? 'Inbox backup saved.' : (result.message || 'Backup failed.'));
    await loadBackups();
  });
  importFile.addEventListener('change', async () => {
    const file = importFile.files?.[0];
    if (!file) return;
    if (!await confirmSheet({ title: 'Import inbox backup?', body: 'The file will be verified and stored on this bridge. Import does not restore it until you choose Restore.', confirmLabel: 'Import' })) {
      importFile.value = ''; return;
    }
    const body = new FormData(); body.append('file', file);
    importFile.disabled = true;
    const result = await app.api.api('/native/recovery/backups/import', { method: 'POST', body });
    if (!current()) return;
    importFile.disabled = false; importFile.value = '';
    toast(result.ok ? 'Backup imported and verified.' : (result.message || 'Import failed.'));
    await loadBackups();
  });
  void load();
  return () => { alive = false; section.remove(); };
}
