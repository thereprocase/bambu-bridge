// submit.js — the Print tab: file → AMS map → confirm → upload.
//
// Build-free, dependency-free. Implements frontend-ux-spec §5 (and the §9
// cross-cutting rules it touches):
//
//   §5.1 Pick     — <input type=file accept=".3mf,.gcode.3mf">, client-side
//                   extension + size<300MB enforcement with inline messages,
//                   and a RECENT reprint list seeded from GET /jobs.
//   §5.2 Map      — parse Metadata/slice_info.config from the chosen .3mf
//                   IN-BROWSER (zip central dir + DecompressionStream(
//                   'deflate-raw') inflate; feature-detected). Defaults the
//                   per-filament tray pick; collects ams_mapping in 1-BASED
//                   physical-slot space (1-4) — never 0 (contract §7.1).
//   §5.3 Confirm  — multipart POST .../jobs via XMLHttpRequest with a
//                   determinate .upbar upload-progress bar. On 201 → dashboard
//                   + snackbar "Submitted — waiting for the printer to start"
//                   (NEVER "Printing" — queued ≠ printing, contract §7.3).
//
//   Error mapping (contract §7.2, §8): 422 invalid_3mf(issues) /
//   ams_slot_empty / ams_slot_invalid / ams_material_mismatch(confirm_mismatch),
//   409 printer_offline, 502 ftps_failed / print_submit_failed, 401 → Key
//   screen, 403 printer_cert_changed → the TOFU trust sheet.
//
// Contract: export function mount(root, app) -> unmount(). See main.js header.

import {
  el, clear, toast, errorCard, banner, certChangedSheet, slotLabel,
} from './ui.js';

const MAX_BYTES = 300 * 1024 * 1024;            // 300 MB (§5.1)
const ACCEPT = '.3mf,.gcode.3mf';
const SLICE_INFO_PATH = 'Metadata/slice_info.config';

// External-spool sentinel for the per-filament pickers. The user picks "Slot
// 1..4" (physical) or "External"; physical slots go into ams_mapping (1-based),
// External flips the external_spool flag.
const EXTERNAL = 'external';

export function mount(root, app) {
  const printerId = app.currentPrinterId();

  // ── screen scaffold ───────────────────────────────────────────────────────
  const shell = el('div', { class: 'app-shell' });
  const main = el('div', { class: 'app-main' });
  shell.appendChild(main);
  root.appendChild(shell);

  main.appendChild(el('div', { class: 'topbar' }, [
    el('div', { class: 'topbar__title', text: 'Print' }),
  ]));

  // the swappable body — each step renders into this node
  const body = el('div', {});
  main.appendChild(body);

  // a hidden file input reused across renders (clicked programmatically)
  const fileInput = el('input', {
    type: 'file', accept: ACCEPT, style: { display: 'none' },
  });
  shell.appendChild(fileInput);

  // ── per-flow mutable state ────────────────────────────────────────────────
  /** @type {{file:File|null, sliceErr:string|null, filaments:Array, externalSpool:boolean}} */
  const flow = {
    file: null,           // the chosen File
    sliceErr: null,       // non-null when slice_info couldn't be read (§5.2)
    filaments: [],        // [{index, type, color, slot}]  slot is 1-4 or EXTERNAL
    externalSpool: false, // derived from a picker set to External
  };

  let xhr = null;         // the in-flight upload (aborted on unmount)

  // No printer selected — nothing to submit to.
  if (!printerId) {
    renderNoPrinter();
    return cleanup;
  }

  // ── file selection (shared by the picker button and recent reprint) ───────
  fileInput.addEventListener('change', () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';           // allow re-picking the same file later
    if (f) onFileChosen(f);
  });

  async function onFileChosen(f) {
    const err = validateFile(f);
    if (err) { renderPick(err); return; }
    flow.file = f;
    flow.externalSpool = false;
    flow._confirmMismatch = false;   // fresh file — clear any prior overrides
    flow._submitFailCount = 0;
    // parse slice_info in-browser to seed the AMS-map defaults (§5.2)
    renderMapLoading();
    const parsed = await readSliceInfo(f);
    flow.sliceErr = parsed.error;
    flow.filaments = parsed.filaments;
    renderMap();
  }

  // ── STEP 1: Pick ──────────────────────────────────────────────────────────
  renderPick();

  function renderPick(inlineError) {
    clear(body);

    const pickBtn = el('button', {
      class: 'btn btn--primary btn--block',
      text: 'Choose a .3mf from this device',
      onClick: () => fileInput.click(),
    });

    const card = el('div', { class: 'card' }, [
      el('div', { class: 't-section', text: 'Print a file' }),
      el('div', { class: 'mt-3' }, [pickBtn]),
      el('div', { class: 'field__hint mt-2', text:
        'Accepts .3mf / .gcode.3mf, up to 300 MB.' }),
    ]);
    if (inlineError) {
      card.appendChild(el('div', { class: 'statusline is-err mt-3', text: inlineError }));
    }
    body.appendChild(card);

    // RECENT reprint list (§5.1) — best-effort; absence is silent.
    const recentCard = el('div', { class: 'card mt-4' }, [
      el('div', { class: 't-section', text: 'Recent' }),
      el('div', { class: 'mt-3 dim', text: 'Loading recent prints…' }),
    ]);
    body.appendChild(recentCard);
    loadRecent(recentCard);
  }

  async function loadRecent(card) {
    const res = await app.api.api(
      `/jobs?printer_id=${encodeURIComponent(printerId)}&limit=5`);
    clear(card);
    card.appendChild(el('div', { class: 't-section', text: 'Recent' }));
    if (!res.ok || !Array.isArray(res.data) || res.data.length === 0) {
      card.appendChild(el('div', { class: 'mt-3 dim', text:
        res.ok ? 'No recent prints yet.' : 'Recent prints are unavailable.' }));
      return;
    }
    const list = el('div', { class: 'stack mt-3' });
    for (const job of res.data) {
      const name = job.file_name || job.subtask_name || 'print';
      const sub = [job.state, job.queued_at ? relTime(job.queued_at) : null]
        .filter(Boolean).join(' · ');
      // Recent rows reprint by name only — the browser cannot re-read a
      // server-side file into a File, and a bridge-file re-submit path is v0.1
      // (§5.1). So the reprint affordance guides the user to re-pick from disk.
      list.appendChild(el('button', {
        class: 'listrow',
        onClick: () => {
          toast(`Pick “${name}” from your device to reprint`, { ms: 3200 });
          fileInput.click();
        },
      }, [
        el('div', { class: 'listrow__main' }, [
          el('div', { class: 'ellipsis', text: name }),
          sub ? el('div', { class: 'listrow__sub', text: sub }) : null,
        ]),
        el('span', { class: 'listrow__chev', text: '↻' }),
      ]));
    }
    card.appendChild(list);
  }

  // ── STEP 2: AMS mapping ────────────────────────────────────────────────────
  function renderMapLoading() {
    clear(body);
    body.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 't-section', text: 'Reading the file…' }),
      el('div', { class: 'mt-3 dim', text: 'Checking which filaments it needs.' }),
    ]));
  }

  function renderMap() {
    clear(body);

    body.appendChild(stepHeader('Map filaments', flow.file.name,
      `${flow.filaments.length} filament${flow.filaments.length === 1 ? '' : 's'} needed`));

    if (flow.sliceErr) {
      // couldn't read the slice — defaulted all to Slot 1 (§5.2)
      body.appendChild(banner(flow.sliceErr, 'amber'));
    }

    const rows = el('div', { class: 'stack' });
    flow.filaments.forEach((fil, i) => {
      rows.appendChild(filamentRow(fil, i));
    });

    body.appendChild(el('div', { class: 'card' }, [
      rows,
      el('div', { class: 'field__hint mt-3', text:
        'Defaults came from the slice file. The bridge rejects empty or '
        + 'wrong-type trays — you can override above.' }),
    ]));

    const actions = el('div', { class: 'row mt-4' }, [
      el('button', {
        class: 'btn btn--ghost', text: 'Back',
        onClick: () => { flow.file = null; renderPick(); },
      }),
      el('span', { class: 'spread' }),
      el('button', {
        class: 'btn btn--primary', text: 'Continue',
        onClick: () => renderConfirm(),
      }),
    ]);
    body.appendChild(actions);
  }

  function filamentRow(fil, i) {
    const sel = el('select', { class: 'select' }, [
      el('option', { value: '1', text: slotLabel(1) }),
      el('option', { value: '2', text: slotLabel(2) }),
      el('option', { value: '3', text: slotLabel(3) }),
      el('option', { value: '4', text: slotLabel(4) }),
      el('option', { value: EXTERNAL, text: 'External' }),
    ]);
    sel.value = String(fil.slot);
    sel.addEventListener('change', () => {
      flow._confirmMismatch = false;   // re-mapping clears the mismatch override
      fil.slot = sel.value === EXTERNAL ? EXTERNAL : Number(sel.value);
    });

    const swatch = fil.color
      ? el('span', { class: 'swatch', style: { background: fil.color } })
      : null;
    const label = fil.type || `Filament ${fil.index}`;

    return el('div', { class: 'row row--between' }, [
      el('div', { class: 'row' }, [
        swatch,
        el('div', {}, [
          el('div', { text: `Filament ${fil.index}` }),
          el('div', { class: 'listrow__sub', text: label }),
        ]),
      ]),
      sel,
    ]);
  }

  // ── STEP 3: Confirm + upload ───────────────────────────────────────────────
  function renderConfirm(errResult) {
    clear(body);

    flow.externalSpool = flow.filaments.some((f) => f.slot === EXTERNAL);
    const trayLine = flow.filaments
      .map((f) => `${f.type || `Filament ${f.index}`} → ${slotLabel(f.slot === EXTERNAL ? null : f.slot)}`)
      .join('  ·  ');

    body.appendChild(stepHeader('Start print?', flow.file.name, fmtSize(flow.file.size)));

    body.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 't-section', text: 'Trays' }),
      el('div', { class: 'mt-2', text: trayLine || 'No AMS mapping' }),
    ]));

    // error from a prior submit attempt (§7.2 mapping)
    let errSlot = null;
    if (errResult) {
      errSlot = el('div', { class: 'mt-4' });
      errSlot.appendChild(renderSubmitError(errResult));
      body.appendChild(errSlot);
    }

    // the action becomes a determinate .upbar during upload (§5.3)
    const upbar = el('div', { class: 'upbar hidden' }, [
      el('div', { class: 'upbar__fill' }),
      el('div', { class: 'upbar__label', text: 'Uploading… 0%' }),
    ]);
    const startBtn = el('button', {
      class: 'btn btn--primary btn--block', text: 'Start print',
      onClick: () => doUpload(startBtn, cancelBtn, upbar),
    });
    const cancelBtn = el('button', {
      class: 'btn btn--ghost btn--block mt-2', text: 'Back',
      onClick: () => renderMap(),
    });

    body.appendChild(el('div', { class: 'mt-4' }, [startBtn, upbar, cancelBtn]));
  }

  // build the ams_mapping (1-based physical slots; External filaments are not
  // listed — they ride external_spool) and submit via XHR with upload progress.
  function doUpload(startBtn, cancelBtn, upbar) {
    const fd = new FormData();
    fd.append('file', flow.file, flow.file.name);

    // ams_mapping: comma-separated PHYSICAL slots (1-4). Per contract §7.1 it is
    // 1-based and MUST NOT contain 0. External-spool filaments are dropped from
    // the mapping and signalled via external_spool instead.
    const mapping = flow.filaments
      .filter((f) => f.slot !== EXTERNAL)
      .map((f) => f.slot);
    if (mapping.length) fd.append('ams_mapping', mapping.join(','));
    if (flow.externalSpool) fd.append('external_spool', 'true');
    // confirm_mismatch rides along when the user opted to use a mismatched tray
    if (flow._confirmMismatch) fd.append('confirm_mismatch', 'true');

    // UI → uploading
    startBtn.classList.add('hidden');
    cancelBtn.disabled = true;
    upbar.classList.remove('hidden');
    const fill = upbar.querySelector('.upbar__fill');
    const lbl = upbar.querySelector('.upbar__label');

    xhr = new XMLHttpRequest();
    const url = app.api.apiBase()
      + `/printers/${encodeURIComponent(printerId)}/jobs`;
    xhr.open('POST', url);
    const key = app.getKey();
    if (key) xhr.setRequestHeader('Authorization', `Bearer ${key}`);

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      fill.style.width = pct + '%';
      lbl.textContent = `Uploading… ${pct}%`;
    };
    xhr.upload.onload = () => { lbl.textContent = 'Validating…'; };

    xhr.onerror = () => {
      xhr = null;
      // network-level failure — treat like a no-bridge result
      renderConfirm({ ok: false, status: 0, network: true,
        message: "Can't reach the bridge to upload.", error: 'network' });
    };

    xhr.onload = () => {
      const status = xhr.status;
      let json = null;
      try { json = JSON.parse(xhr.responseText || 'null'); } catch { /* */ }
      xhr = null;

      if (status >= 200 && status < 300) {
        // queued ≠ printing — NEVER say "Printing" here (§7.3, §9.5).
        app.setCurrentPrinterId(printerId);
        app.navigate('#/');
        toast('Submitted — waiting for the printer to start.', { ms: 3500 });
        return;
      }

      // build a normalized failure result from the parsed body
      const result = app.api.parseEnvelope(status, json);

      // 401 → bridge-key failure → Key screen (NOT an upload retry, §7.2).
      if (status === 401) { app.requireKeyScreen(); return; }

      // 403 printer_cert_changed → TOFU trust sheet (never the Key screen).
      if (result.error === 'printer_cert_changed') {
        certChangedSheet(result, (action) => {
          if (action && action.id === 'trust') {
            trustThenRetry(startBtn, cancelBtn, upbar);
          } else {
            renderConfirm();
          }
        });
        return;
      }

      renderConfirm(result);
    };

    xhr.send(fd);
  }

  async function trustThenRetry(startBtn, cancelBtn, upbar) {
    const t = await app.api.postJson(
      `/printers/${encodeURIComponent(printerId)}/trust`, undefined);
    if (t.ok || t.status === 204) {
      // re-render confirm so the upbar/buttons reset, then auto-retry the upload
      renderConfirm();
      const sb = body.querySelector('.btn--primary');
      const cb = body.querySelector('.btn--ghost');
      const ub = body.querySelector('.upbar');
      if (sb && cb && ub) doUpload(sb, cb, ub);
    } else {
      renderConfirm(t);
    }
  }

  // map a submit failure result to the right inline UI + actions (§7.2).
  function renderSubmitError(result) {
    const err = result.error;

    // 409 printer_offline — banner + Retry (the print itself is fine).
    if (result.status === 409 && err === 'printer_offline') {
      return banner(
        result.message || 'Printer is offline — power on the P1S and try again.',
        'red',
        { label: 'Retry', onClick: () => renderConfirm() });
    }

    // 422 invalid_3mf — fix-the-file problems; do NOT offer a blind retry.
    if (err === 'invalid_3mf') {
      const amsIssue = (result.issues || []).some(
        (i) => (i && i.category) === 'ams');
      return errorCard(result, {
        actions: amsIssue
          ? [{ label: 'Edit mapping', primary: true, onClick: () => renderMap() }]
          : [{ label: 'Pick another file', primary: true,
              onClick: () => { flow.file = null; renderPick(); } }],
      });
    }

    // 422 ams_material_mismatch — confirmable warning: "Use it anyway".
    if (err === 'ams_material_mismatch') {
      return errorCard(result, {
        actions: [
          { label: 'Use it anyway', primary: true, onClick: () => {
            flow._confirmMismatch = true; renderConfirm();
          } },
          { label: 'Edit mapping', onClick: () => renderMap() },
        ],
      });
    }

    // 422 ams_slot_empty / ams_slot_invalid — fix the mapping.
    if (err === 'ams_slot_empty' || err === 'ams_slot_invalid') {
      return errorCard(result, {
        actions: [{ label: 'Edit mapping', primary: true, onClick: () => renderMap() }],
      });
    }

    // 502 ftps_failed / print_submit_failed — printer rejected the file.
    if (err === 'ftps_failed' || err === 'print_submit_failed'
        || result.status === 502) {
      flow._submitFailCount = (flow._submitFailCount || 0) + 1;
      const second = flow._submitFailCount >= 2;
      return errorCard(
        { ...result, message: result.message
            || 'Upload failed (the printer rejected the file).',
          remediation_hint: second
            ? 'It failed again — check the bridge log for the FTPS error.'
            : result.remediation_hint },
        { actions: [{ label: 'Retry', primary: true, onClick: () => renderConfirm() }] });
    }

    // anything else (incl. network) — generic retry.
    return errorCard(result, {
      actions: [{ label: 'Retry', primary: true, onClick: () => renderConfirm() }],
    });
  }

  // ── no-printer fallback ────────────────────────────────────────────────────
  function renderNoPrinter() {
    clear(body);
    body.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 't-section', text: 'Print a file' }),
      el('div', { class: 'mt-3 dim', text:
        'No printer is selected. Add one in Settings first.' }),
      el('button', {
        class: 'btn btn--ghost btn--block mt-4', text: 'Open settings',
        onClick: () => app.navigate('#/settings'),
      }),
    ]));
  }

  // ── small render helpers ────────────────────────────────────────────────────
  function stepHeader(title, name, sub) {
    return el('div', { class: 'section' }, [
      el('div', { class: 't-display', style: { fontSize: '24px' }, text: title }),
      el('div', { class: 'mt-2 ellipsis', text: name }),
      sub ? el('div', { class: 'listrow__sub', text: sub }) : null,
    ]);
  }

  // ── lifecycle ───────────────────────────────────────────────────────────────
  function cleanup() {
    if (xhr) { try { xhr.abort(); } catch { /* */ } xhr = null; }
  }
  return cleanup;
}

// ── client-side file validation (§5.1) ────────────────────────────────────────
/** @param {File} f @returns {string|null} an inline error message, or null if OK */
function validateFile(f) {
  const name = (f.name || '').toLowerCase();
  const okExt = name.endsWith('.gcode.3mf') || name.endsWith('.3mf');
  if (!okExt) return `“${f.name}” isn’t a .3mf file. Choose a .3mf or .gcode.3mf.`;
  if (f.size > MAX_BYTES) {
    return `That file is ${fmtSize(f.size)} — the limit is 300 MB.`;
  }
  if (f.size === 0) return 'That file is empty.';
  return null;
}

// ── in-browser slice_info parse (§5.2) ─────────────────────────────────────────
/**
 * Read Metadata/slice_info.config out of the chosen .3mf (a ZIP) entirely in
 * the browser: locate the End-Of-Central-Directory record, walk the central
 * directory to find the entry, then inflate it with DecompressionStream(
 * 'deflate-raw'). Feature-detected; on any absence/failure we default all
 * filaments to Slot 1 and return an honest banner string.
 *
 * @param {File} file
 * @returns {Promise<{filaments:Array<{index:number,type:string|null,color:string|null,slot:number}>, error:string|null}>}
 */
async function readSliceInfo(file) {
  const fallback = (msg, count) => ({
    filaments: defaultFilaments(count || 1),
    error: msg,
  });

  // feature-detect deflate-raw (Chromium/FF/Safari current; absent on old)
  if (typeof DecompressionStream === 'undefined') {
    return fallback('Couldn’t read the slice — pick trays manually.', 1);
  }

  try {
    const buf = new Uint8Array(await file.arrayBuffer());
    const entry = findZipEntry(buf, SLICE_INFO_PATH);
    if (!entry) {
      return fallback('Couldn’t find filament info in the file — pick trays manually.', 1);
    }
    let xml;
    if (entry.method === 0) {
      // stored (no compression)
      xml = new TextDecoder().decode(entry.data);
    } else if (entry.method === 8) {
      xml = await inflateRaw(entry.data);
    } else {
      return fallback('Couldn’t read the slice — pick trays manually.', 1);
    }
    const filaments = parseSliceInfoXml(xml);
    if (!filaments.length) {
      return fallback('Couldn’t read the slice — pick trays manually.', 1);
    }
    return { filaments, error: null };
  } catch {
    return fallback('Couldn’t read the slice — pick trays manually.', 1);
  }
}

function defaultFilaments(count) {
  const out = [];
  for (let i = 1; i <= Math.max(1, count); i++) {
    out.push({ index: i, type: null, color: null, slot: 1 });
  }
  return out;
}

/** Inflate raw-deflate bytes to a UTF-8 string via DecompressionStream. */
async function inflateRaw(bytes) {
  const ds = new DecompressionStream('deflate-raw');
  const stream = new Blob([bytes]).stream().pipeThrough(ds);
  const out = await new Response(stream).arrayBuffer();
  return new TextDecoder().decode(new Uint8Array(out));
}

// Minimal ZIP central-directory reader. Returns {data, method} for the named
// entry, where data is the (possibly compressed) file bytes. Pure, no deps.
function findZipEntry(buf, wantName) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  // locate EOCD (sig 0x06054b50), searching back from the end (max 64KB comment)
  const EOCD_SIG = 0x06054b50;
  let eocd = -1;
  const minPos = Math.max(0, buf.length - 22 - 0xffff);
  for (let i = buf.length - 22; i >= minPos; i--) {
    if (dv.getUint32(i, true) === EOCD_SIG) { eocd = i; break; }
  }
  if (eocd < 0) return null;
  const cdCount = dv.getUint16(eocd + 10, true);
  let cdOffset = dv.getUint32(eocd + 16, true);

  // walk central directory entries (sig 0x02014b50)
  const CD_SIG = 0x02014b50;
  let p = cdOffset;
  for (let n = 0; n < cdCount; n++) {
    if (dv.getUint32(p, true) !== CD_SIG) break;
    const method = dv.getUint16(p + 10, true);
    const compSize = dv.getUint32(p + 20, true);
    const nameLen = dv.getUint16(p + 28, true);
    const extraLen = dv.getUint16(p + 30, true);
    const commentLen = dv.getUint16(p + 32, true);
    const localOffset = dv.getUint32(p + 42, true);
    const name = new TextDecoder().decode(buf.subarray(p + 46, p + 46 + nameLen));
    if (name === wantName || name === wantName.replace(/^\//, '')) {
      return readLocalEntry(buf, dv, localOffset, method, compSize);
    }
    p += 46 + nameLen + extraLen + commentLen;
  }
  return null;
}

function readLocalEntry(buf, dv, localOffset, method, compSize) {
  const LFH_SIG = 0x04034b50;
  if (dv.getUint32(localOffset, true) !== LFH_SIG) return null;
  const nameLen = dv.getUint16(localOffset + 26, true);
  const extraLen = dv.getUint16(localOffset + 28, true);
  const dataStart = localOffset + 30 + nameLen + extraLen;
  const data = buf.subarray(dataStart, dataStart + compSize);
  return { data, method };
}

/**
 * Parse Bambu's slice_info.config XML. It contains a <filament> element per
 * extruder-mapped filament with id/type/color attributes, e.g.:
 *   <filament id="1" type="PLA" color="#FF0000" used_g="18.2"/>
 * We use DOMParser (built-in, no deps) and fall back to a regex scan if the
 * document isn't well-formed.
 * @param {string} xml
 * @returns {Array<{index:number,type:string|null,color:string|null,slot:number}>}
 */
function parseSliceInfoXml(xml) {
  const out = [];
  try {
    const doc = new DOMParser().parseFromString(xml, 'application/xml');
    if (!doc.querySelector('parsererror')) {
      const nodes = doc.querySelectorAll('filament');
      nodes.forEach((node, i) => {
        const id = parseInt(node.getAttribute('id') || '', 10);
        const index = Number.isFinite(id) && id > 0 ? id : i + 1;
        out.push({
          index,
          type: node.getAttribute('type') || null,
          color: normColor(node.getAttribute('color')),
          slot: clampSlot(index),
        });
      });
    }
  } catch { /* fall through to regex */ }

  if (out.length) return dedupeByIndex(out);

  // regex fallback for slightly-off XML
  const re = /<filament\b[^>]*>/g;
  let m, i = 0;
  while ((m = re.exec(xml)) !== null) {
    const tag = m[0];
    i += 1;
    const id = parseInt((/\bid="(\d+)"/.exec(tag) || [])[1] || '', 10);
    const index = Number.isFinite(id) && id > 0 ? id : i;
    out.push({
      index,
      type: (/\btype="([^"]*)"/.exec(tag) || [])[1] || null,
      color: normColor((/\bcolor="([^"]*)"/.exec(tag) || [])[1]),
      slot: clampSlot(index),
    });
  }
  return dedupeByIndex(out);
}

function dedupeByIndex(list) {
  const seen = new Set();
  const out = [];
  for (const f of list) {
    if (seen.has(f.index)) continue;
    seen.add(f.index);
    out.push(f);
  }
  // renumber to a contiguous 1..N for display while keeping the default slot
  return out.map((f, i) => ({ ...f, index: i + 1, slot: clampSlot(i + 1) }));
}

// default per-filament tray = the matching physical slot, clamped to 1-4.
function clampSlot(index) {
  return Math.min(4, Math.max(1, index));
}

function normColor(c) {
  if (!c) return null;
  const s = String(c).trim();
  // slice_info colors arrive as #RRGGBB or #RRGGBBAA — keep the leading 6 hex.
  const m = /^#?([0-9a-fA-F]{6})/.exec(s);
  return m ? '#' + m[1] : null;
}

// ── tiny formatters (bridge owns words; these are pure presentation) ───────────
function fmtSize(bytes) {
  if (bytes == null) return '';
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  const kb = bytes / 1024;
  return `${Math.max(1, Math.round(kb))} KB`;
}

function relTime(iso) {
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
