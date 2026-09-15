// Readonly replay review. Analysis and inventory refresh never submit a print.
export async function showReplayReview(root, app, capture, signal) {
  const { el } = app.ui;
  root.replaceChildren();
  let report = null, busy = false, choices = {};
  const status = el('p', { role: 'status', text: 'Loading printers…' });
  const picker = el('select', { class: 'input', 'aria-label': 'Replay printer' });
  const content = el('div');
  const check = el('button', { class: 'btn', text: 'Refresh AMS and check mapping', disabled: true });
  root.append(el('h3', { text: 'Review a repeat print' }),
    el('p', { class: 't-caption', text: 'Choose spools for the saved toolpath. This preview does not start a print.' }),
    picker, check, status, content);

  function render() {
    content.replaceChildren();
    const spec = report.requirements;
    content.append(el('p', { class: 't-caption', text: `Plate ${spec.plate} · ${spec.nozzle_diameter ?? 'Unknown'} mm nozzle · ${spec.bed_type || 'Unknown plate'}` }));
    for (const row of report.mapping) {
      const group = el('label', { style: { display: 'block', marginTop: '12px' } });
      group.append(el('span', { text: `${row.label} · ${row.material} · ${row.profile_name || 'Profile name unavailable'}`, style: { display: 'block', overflowWrap: 'anywhere' } }));
      const select = el('select', { class: 'input', 'aria-label': `${row.label} current spool` });
      select.append(el('option', { value: '', text: 'Choose a current spool' }));
      for (const slot of row.candidates) {
        const suggestion = slot.wire_id === row.suggestion ? ' · matches saved color/profile' : '';
        select.append(el('option', { value: String(slot.wire_id), text: `${slot.label} · ${slot.material}${suggestion}` }));
      }
      select.value = row.choice === null ? '' : String(row.choice);
      select.addEventListener('change', () => {
        if (select.value === '') delete choices[row.index];
        else choices[row.index] = Number(select.value);
        status.textContent = 'Choices changed. Check the mapping against a fresh inventory.';
      });
      group.append(select);
      content.append(group);
    }
    const issues = el('ul');
    for (const issue of report.issues) issues.append(el('li', { text: issue }));
    content.append(issues);
    for (const note of report.review_notes) content.append(el('p', { class: 't-caption', text: note }));
    content.append(el('p', { class: 't-caption', text: report.dispatch_note }));
  }

  async function run() {
    if (busy || !picker.value) return;
    busy = true; check.disabled = true; picker.disabled = true;
    content.querySelectorAll('select').forEach(select => { select.disabled = true; });
    status.textContent = 'Reading current material inventory…';
    try {
      const response = await app.api.postJson(`/library/captures/${encodeURIComponent(capture.id)}/replay-review`, {
        printer_id: picker.value, choices, refresh: true,
        expected_inventory: report?.inventory.fingerprint ?? null,
      }, { signal });
      if (signal.aborted) return;
      if (!response.ok) throw new Error(response.message || 'The saved slice could not be reviewed.');
      report = response.data;
      status.textContent = report.mapping_complete ? 'Mapping choices checked. Hardware and profile review still required.' : 'Review the mapping details below.';
      render();
    } catch (error) {
      if (!signal.aborted) status.textContent = error.message || 'Could not review this slice.';
    } finally {
      busy = false; check.disabled = false; picker.disabled = false;
      content.querySelectorAll('select').forEach(select => { select.disabled = false; });
    }
  }
  check.addEventListener('click', run);
  picker.addEventListener('change', () => { report = null; choices = {}; run(); });
  try {
    const result = await app.api.api('/printers', { signal });
    if (signal.aborted) return;
    if (!result.ok) throw new Error(result.message || 'Could not load printers.');
    for (const printer of result.data) {
      picker.append(el('option', { value: printer.printer_id, text: printer.friendly_name || printer.printer_id }));
    }
    if (!picker.options.length) { status.textContent = 'Register a printer to review current spool choices.'; return; }
    const preferred = app.currentPrinterId?.();
    if (preferred && [...picker.options].some(option => option.value === preferred)) picker.value = preferred;
    check.disabled = false;
    await run();
  } catch (error) {
    if (!signal.aborted) status.textContent = error.message || 'Could not load printers.';
  }
}
