// Library artifacts use authenticated downloads. Never put a key in a URL.
import { showReplayReview } from './library-replay.js';

export function mount(root, app) {
  const { el } = app.ui;
  const controller = new AbortController();
  let rows = [], stopped = false, busy = false, more = true;
  const content = el('section', { class: 'stack' });
  const status = el('p', { class: 't-caption', role: 'status', text: 'Loading library…' });
  const archiveStatus = el('p', { class: 't-caption', role: 'status' });
  const search = el('input', { class: 'input', type: 'search', placeholder: 'Search loaded captures', 'aria-label': 'Search loaded captures' });
  const loadMore = el('button', { class: 'btn', text: 'Load more', onClick: () => load() });
  const refresh = el('button', { class: 'btn', text: 'Refresh history', onClick: () => load(true) });
  const page = el('main', { class: 'app-main library-page' }, [
    el('h1', { class: 'topbar__title', text: 'Print library' }),
    el('p', { class: 't-caption', text: 'Recover the files behind a print. Stored projects and preserved originals are listed separately.' }),
    search, refresh, status, archiveStatus, content, loadMore,
  ]);
  root.append(el('div', { class: 'app-shell' }, [page]));

  async function download(row, artifact, button) {
    button.disabled = true;
    try {
      const path = `/library/captures/${encodeURIComponent(row.id)}/files/${encodeURIComponent(artifact.name)}`;
      const response = await app.api.api(path, { raw: true, signal: controller.signal });
      if (!response.ok) throw new Error(response.message || 'Could not download the artifact.');
      const blob = await response.blob();
      if (stopped) return;
      const url = URL.createObjectURL(blob);
      const link = el('a', { href: url, download: artifact.name });
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (!stopped) status.textContent = error.message || 'Could not download the artifact.';
    } finally { button.disabled = false; }
  }

  function render() {
    content.replaceChildren();
    const term = search.value.toLowerCase().trim();
    const matches = rows.filter(row => [row.title, row.slicer_version, ...row.artifacts.map(a => a.name)].join(' ').toLowerCase().includes(term));
    for (const row of matches) {
      const date = new Date(row.created * 1000).toLocaleString();
      const hasProject = row.artifacts.some(a => a.role === 'project');
      const projectState = !hasProject ? 'not included' : row.state !== 'stored' ? 'upload pending' : row.project_roundtrip_verified ? 'reopening verified' : 'stored; reopening not yet verified';
      const card = el('article', { class: 'card', style: { padding: '20px', marginTop: '16px' } }, [
        el('h2', { class: 't-subhead', text: row.title, style: { overflowWrap: 'anywhere' } }),
        el('p', { class: 't-caption', text: `${date} · Plate ${row.plate} · ${row.slicer_version}` }),
        el('p', { text: row.state === 'stored' ? 'Files verified and stored' : 'Upload pending' }),
        el('p', { class: 't-caption', text: `Original inputs: ${row.originals}. Orca project: ${projectState}.` }),
      ]);
      for (const artifact of row.artifacts) {
        const button = el('button', {
          class: 'btn', text: `Download ${artifact.role} · ${artifact.name}`,
          disabled: row.state !== 'stored',
          style: { display: 'block', marginTop: '8px', overflowWrap: 'anywhere' },
        });
        button.addEventListener('click', () => download(row, artifact, button));
        card.append(button);
      }
      if (row.attempts?.length) {
        const attempts = el('details', { style: { marginTop: '16px' } }, [
          el('summary', { text: `Print attempts (${row.attempts.length})` }),
        ]);
        for (const attempt of [...row.attempts].sort((a, b) => b.created_at - a.created_at)) {
          const time = new Date(attempt.created_at * 1000).toLocaleString();
          const state = attempt.state.replaceAll('_', ' ');
          attempts.append(el('p', { text: `${time} · ${state} · ${attempt.printer_id}` }));
        }
        card.append(attempts);
      }
      if (row.state === 'stored' && row.artifacts.some(a => a.role === 'slice')) {
        const review = el('div');
        const openReview = el('button', { class: 'btn', text: 'Review replay mapping', style: { marginTop: '16px' } });
        openReview.addEventListener('click', () => {
          openReview.disabled = true;
          showReplayReview(review, app, row, controller.signal);
        });
        card.append(openReview, review);
      }
      content.append(card);
    }
    if (!matches.length) content.append(el('p', { text: rows.length ? 'No matching captures in the loaded results.' : 'No captures have been archived yet.' }));
    loadMore.hidden = !more;
  }

  async function load(reset = false) {
    if (busy || (!more && !reset)) return;
    busy = true; loadMore.disabled = true; refresh.disabled = true;
    try {
      const last = reset ? null : rows[rows.length - 1];
      const before = last ? `&before=${last.created}&before_id=${encodeURIComponent(last.id)}` : '';
      const result = await app.api.api(`/library/captures?limit=50${before}`, { signal: controller.signal });
      if (!result.ok) throw new Error(result.message || 'The library is unavailable.');
      const batch = result.data;
      if (stopped) return;
      if (reset) rows = [];
      rows.push(...batch); more = batch.length === 50;
      status.textContent = `${rows.length} capture${rows.length === 1 ? '' : 's'} loaded`;
      render();
      const health = await app.api.api('/library/history-status', { signal: controller.signal });
      if (stopped) return;
      if (health.ok) {
        archiveStatus.textContent = health.data.last_error
          ? 'A print could not be archived. Its receipt is retained for retry; check library storage and bridge logs.'
          : health.data.native_available
            ? 'Native print receipts are checked every 30 seconds. Files are saved while the upload cache still retains them.'
            : 'Native print capture is unavailable on this bridge. Plugin uploads remain separate.';
      }
    } catch (error) {
      if (!stopped) status.textContent = error.message || 'The library is unavailable.';
    } finally { busy = false; loadMore.disabled = false; refresh.disabled = false; }
  }
  search.addEventListener('input', render);
  load();
  return () => { stopped = true; controller.abort(); };
}
