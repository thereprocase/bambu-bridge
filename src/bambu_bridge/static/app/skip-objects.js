// skip-objects.js — the Skip Objects sheet (OrcaSlicer 2.4.2 PartSkipDialog).
//
// The bridge gives the running plate's objects and Orca's pick map as
// run-length rows of identify_ids (GET /printers/{id}/skip_objects). The map
// image is the bridge's rendering in Orca's skip-canvas colours with the
// current selection (GET …/skip_objects/map.png?checked=…); a tap is
// hit-tested against the rows exactly as SkipPartCanvas::GetIdAtViewPt reads
// the pick image: the id at that pixel, if it is a listed object.
//
// Dialog rules (PartSkipDialog.cpp):
//   - Already-skipped objects (the printer's s_obj) show checked and disabled,
//     and follow the printer while the sheet is open.
//   - "Select All" toggles every object not yet skipped.
//   - "<n> /<m> Selected": n checked, m not yet skipped.
//   - Skip is refused with Orca's tooltip text: nothing selected, over 64
//     objects, or a job sliced without object labels.
//   - Confirm: "Skipping <n> objects." / "This action cannot be undone.
//     Continue?"; when every object would be gone, "Skipping all objects." /
//     "The printing job will be stopped. Continue?" (the bridge then stops
//     the print, as Orca does).
//
// The POST carries the job identity this sheet was built from (job,
// gcode_file, plate, digest) and the action the user confirmed; the bridge
// refuses with 409 if either no longer holds. The sheet reloads when the
// printer reports another job.

import { el, clear, mountSheet, confirmSheet, toast, errorCard } from './ui.js';

const MAX_OBJECTS = 64;

/**
 * identify_id at image pixel (ix, iy) of a run-length map, or 0.
 * @param {{width:number,height:number,rows:number[][]}} map
 * @param {number} ix
 * @param {number} iy
 */
export function idAt(map, ix, iy) {
  if (!map || ix < 0 || iy < 0 || ix >= map.width || iy >= map.height) return 0;
  const row = map.rows[iy] || [];
  let x = 0;
  for (let i = 0; i < row.length; i += 2) {
    x += row[i + 1];
    if (ix < x) return row[i];
  }
  return 0;
}

/**
 * The object under a tap at (x, y) inside an element of the given size that
 * shows the whole map stretched to fit (SkipPartCanvas at 100 % zoom).
 * Ids the plate does not list (stray colours) are not objects.
 */
export function hitTest(map, objectIds, x, y, width, height) {
  if (!map || !(width > 0) || !(height > 0)) return 0;
  const id = idAt(map, Math.floor((x * map.width) / width), Math.floor((y * map.height) / height));
  return objectIds.has(id) ? id : 0;
}

/** Orca's Skip-button refusal for this state, or '' when it may apply. */
export function applyRefusal(data, states) {
  const checked = [...states.values()].filter((s) => s === 'checked').length;
  if (!checked) return 'Nothing selected';
  if (states.size > MAX_OBJECTS) return 'Over 64 objects in single plate';
  if (!data.label_object_enabled) return 'The current print job cannot be skipped';
  return data.available ? '' : (data.reason || 'Skipping objects is unavailable');
}

/** True when the printer now reports a different job than the sheet shows. */
export function jobChanged(data, snap) {
  const raw = (snap && snap._raw) || {};
  return (typeof raw.subtask_name === 'string' && raw.subtask_name !== data.job)
    || (typeof raw.gcode_file === 'string' && raw.gcode_file !== data.gcode_file);
}

function reportedJob(snap) {
  const raw = (snap && snap._raw) || {};
  return `${raw.subtask_name}|${raw.gcode_file}`;
}

/**
 * @param {any} app
 * @param {string} pid
 * @returns {{close:()=>void}}
 */
export function open(app, pid) {
  const body = el('div');
  const errSlot = el('div', { class: 'controls-err' });
  let unsub = null;
  const handle = mountSheet(el('div', {}, [
    el('div', { class: 'row row--between mb-4' }, [
      el('div', { class: 'sheet__title', style: { margin: '0' }, text: 'Skip Objects' }),
      el('button', {
        class: 'input-eye', 'aria-label': 'Close skip objects', text: '✕',
        style: { position: 'static', width: '36px', height: '36px' },
        onClick: () => handle.close(),
      }),
    ]),
    errSlot,
    body,
  ]), { drawer: true, onClose: () => { if (unsub) unsub(); unsub = null; } });

  async function load() {
    if (unsub) { unsub(); unsub = null; }
    clear(errSlot);
    clear(body);
    body.appendChild(el('div', { class: 'field__hint', text: 'Loading…' }));
    const r = await app.api.api(`/printers/${encodeURIComponent(pid)}/skip_objects`);
    clear(body);
    if (!r.ok) {
      // PartSkipDialog's retry page.
      body.appendChild(el('div', { class: 'field__hint',
        text: 'Load skipping objects information failed. Please try again.' }));
      body.appendChild(errorCard(r));
      body.appendChild(el('button', { class: 'btn btn--primary mt-3', text: 'Retry', onClick: load }));
      return;
    }
    unsub = build(app, pid, r.data, body, errSlot, handle, reloadFor);
  }
  // Reload once per job the printer reports, so a bridge answer that still
  // disagrees with the live snapshot cannot loop.
  let reloadedFor = null;
  function reloadFor(key) {
    if (key === reloadedFor) return false;
    reloadedFor = key;
    load();
    return true;
  }
  load();
  return { close: () => handle.close() };
}

function build(app, pid, data, host, errSlot, handle, reload) {
  // part id -> 'unchecked' | 'checked' | 'skipped' (PartState), listed by id.
  const states = new Map(data.objects.map((o) => [o.id, o.skipped ? 'skipped' : 'unchecked']));
  const ids = new Set(states.keys());
  const rows = new Map();

  const img = el('img', {
    alt: 'Plate map', draggable: 'false',
    style: { width: '100%', aspectRatio: '1 / 1', display: 'block', cursor: 'pointer', touchAction: 'manipulation' },
    onClick: (e) => {
      const id = hitTest(data.map, ids, e.offsetX, e.offsetY, img.clientWidth, img.clientHeight);
      if (id && states.get(id) !== 'skipped') toggle(id, states.get(id) !== 'checked');
    },
  });
  const allBox = el('input', { type: 'checkbox', 'aria-label': 'Select All',
    onChange: () => {
      for (const [id, s] of states) {
        if (s !== 'skipped') states.set(id, allBox.checked ? 'checked' : 'unchecked');
      }
      render();
    } });
  const count = el('span', { class: 't-section' });
  const total = el('span', { class: 't-caption' });
  const hint = el('div', { class: 'field__hint' });
  const skipBtn = el('button', { class: 'btn btn--danger', text: 'Skip', onClick: apply });
  let sending = false;

  function toggle(id, on) {
    states.set(id, on ? 'checked' : 'unchecked');
    render();
  }

  const list = el('div', {}, data.objects.map((o) => {
    const box = el('input', { type: 'checkbox', 'aria-label': o.name || `Object ${o.id}`,
      onChange: () => toggle(o.id, box.checked) });
    rows.set(o.id, box);
    return el('label', { class: 'row gap-2', style: { padding: 'var(--space-1) 0' } }, [
      box, el('span', { text: o.name || `Object ${o.id}` }),
    ]);
  }));

  let mapKey = null;
  function render() {
    const checked = [...states].filter(([, s]) => s === 'checked').map(([id]) => id);
    const open = [...states.values()].filter((s) => s !== 'skipped').length;
    for (const [id, box] of rows) {
      const s = states.get(id);
      box.checked = s !== 'unchecked';
      box.disabled = s === 'skipped';
    }
    allBox.checked = open > 0 && [...states.values()].every((s) => s !== 'unchecked');
    count.textContent = String(checked.length);
    total.textContent = ` /${open} Selected`;
    const refusal = applyRefusal(data, states);
    skipBtn.disabled = !!refusal || sending;
    hint.textContent = refusal;
    const key = [...states].map(([id, s]) => `${id}:${s}`).join(',');
    if (data.map && key !== mapKey) {
      // The bridge draws skipped parts from s_obj; `v` changes with them so
      // the image reloads when the printer skips something.
      mapKey = key;
      const skipped = [...states].filter(([, s]) => s === 'skipped').map(([id]) => id);
      img.src = app.api.tokenUrl(`/printers/${encodeURIComponent(pid)}/skip_objects/map.png`,
        { checked: checked.join(','), v: skipped.join(','), digest: data.digest });
    }
  }

  async function apply() {
    const chosen = [...states].filter(([, s]) => s === 'checked').map(([id]) => id);
    if (sending || !chosen.length || applyRefusal(data, states)) return;
    // One confirm and one POST at a time: Skip stays disabled until done.
    sending = true;
    render();
    try {
      const all = [...states.values()].every((s) => s !== 'unchecked');
      const ok = await confirmSheet({
        title: all ? 'Skipping all objects.' : `Skipping ${chosen.length} objects.`,
        body: all ? 'The printing job will be stopped. Continue?' : 'This action cannot be undone. Continue?',
        confirmLabel: 'Continue',
        danger: true,
      });
      if (!ok) return;
      clear(errSlot);
      const r = await app.api.postJson(`/printers/${encodeURIComponent(pid)}/skip_objects`, {
        obj_list: chosen,
        action: all ? 'stop' : 'skip',
        job: data.job,
        gcode_file: data.gcode_file,
        plate: data.plate,
        digest: data.digest,
      });
      if (!r.ok) { errSlot.appendChild(errorCard(r)); return; }
      toast(r.data && r.data.action === 'stop' ? 'Stopping…' : `Skipping ${chosen.length} objects`);
      handle.close();
    } finally {
      sending = false;
      render();
    }
  }

  // PartSkipDialog::UpdatePartsStateFromPrinter: follow the printer's s_obj.
  function follow() {
    const snap = app.store.current(pid);
    if (jobChanged(data, snap) && reload(reportedJob(snap))) return;
    const reported = (snap && snap.job && snap.job.skipped_objects) || [];
    let changed = false;
    for (const id of reported) {
      if (states.has(id) && states.get(id) !== 'skipped') { states.set(id, 'skipped'); changed = true; }
    }
    if (changed) render();
  }

  host.appendChild(el('div', { class: 'stack' }, [
    data.map
      ? el('div', { style: { maxWidth: '400px', margin: '0 auto var(--space-3)' } }, [img])
      : el('div', { class: 'field__hint', text: 'This print file has no object map; pick objects from the list.' }),
    el('label', { class: 'row gap-2' }, [allBox, el('span', { text: 'Select All' })]),
    list,
    el('div', { class: 'row row--between', style: { marginTop: 'var(--space-3)' } }, [
      el('div', {}, [count, total]),
      skipBtn,
    ]),
    hint,
  ]));
  follow();
  render();
  return app.store.subscribe(follow);
}
