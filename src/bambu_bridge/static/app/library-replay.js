// Analysis is readonly. An enabled Start requires explicit, persisted approval.
export async function showReplayReview(root, app, capture, signal) {
  const { el } = app.ui;
  root.replaceChildren();
  let report = null, busy = false, choices = {};
  const pendingKey = `bbl.libraryReplay:${app.api.apiBase?.() || ''}:${capture.id}`;
  let pendingId = null, pendingBody = null, submitting = false;
  try {
    const saved = localStorage.getItem(pendingKey);
    if (/^[0-9a-f]{32}$/.test(saved || '')) pendingId = saved; // earlier ID-only records
    else if (saved) {
      const pending = JSON.parse(saved);
      if (/^[0-9a-f]{32}$/.test(pending.id)) {
        pendingId = pending.id;
        if (pending.body?.id === pendingId) pendingBody = pending.body;
      }
    }
  } catch {}
  const status = el('p', { role: 'status', text: 'Loading printers…' });
  const picker = el('select', { class: 'input', 'aria-label': 'Replay printer' });
  const content = el('div');
  const submission = el('div', { role: 'status', style: { marginTop: '16px' } });
  const checkSubmission = el('button', { class: 'btn', text: 'Check submission', hidden: !pendingId });
  const check = el('button', { class: 'btn', text: 'Refresh AMS and check mapping', disabled: true });
  root.append(el('h3', { text: 'Review a repeat print' }),
    el('p', { class: 't-caption', text: 'Choose spools for the saved toolpath. This preview does not start a print.' }),
    picker, check, status, content, submission, checkSubmission);

  function remember(id, body = null) {
    pendingId = id;
    pendingBody = body;
    try {
      if (id) localStorage.setItem(pendingKey, JSON.stringify({ id, body }));
      else localStorage.removeItem(pendingKey);
    } catch {}
    checkSubmission.hidden = !id;
    picker.hidden = check.hidden = !!id;
    if (id) pendingView();
  }

  function pendingView() {
    picker.hidden = check.hidden = true;
    content.replaceChildren();
    status.textContent = 'Check the saved print request before preparing another.';
    if (!pendingBody) return;
    content.append(el('p', { text: `${pendingBody.nozzle_diameter} mm nozzle · ${pendingBody.bed_type}` }));
    for (const [logical, slot] of Object.entries(pendingBody.choices || {})) {
      const target = slot === 254 ? 'External spool' : `AMS ${String.fromCharCode(65 + Math.floor(slot / 4))} · slot ${slot % 4 + 1}`;
      content.append(el('p', { class: 't-caption', text: `Filament ${Number(logical) + 1} → ${target}` }));
    }
  }

  function showSubmission(result) {
    const needsReview = ['failed', 'blocked', 'unknown', 'rejected', 'receipt_unavailable'].includes(result.state);
    submission.replaceChildren(el('p', { text: `Replay request: ${result.state.replaceAll('_', ' ')}${needsReview && result.code ? ` · ${result.code}` : ''}` }));
    if (['completed', 'failed', 'canceled', 'cancelled', 'interrupted', 'blocked', 'not_started', 'resolved'].includes(result.state)) {
      submission.append(el('button', { class: 'btn', text: 'Review another print', onClick: () => {
        remember(null); report = null; choices = {}; submission.replaceChildren(); run();
      } }));
    }
  }

  async function inspectSubmission() {
    if (!pendingId || submitting) return;
    checkSubmission.disabled = true;
    try {
      const response = await app.api.api(`/library/replays/${encodeURIComponent(pendingId)}`, { signal });
      if (signal.aborted) return;
      if (response.ok) showSubmission(response.data);
      else {
        submission.replaceChildren(el('p', { text: response.status === 404
          ? 'No receipt is available yet. Keep this request ID; retrying it uses the same saved choices and can start this print.'
          : response.message || 'Could not check the request. Keep this request ID until its outcome is known.' }));
        if (response.status === 404 && pendingBody) {
          const ready = el('input', { type: 'checkbox', 'aria-label': 'Bed and spools still checked' });
          const retry = el('button', { class: 'btn', text: 'Retry same request', disabled: true, onClick: sendPending });
          ready.addEventListener('change', () => { retry.disabled = !ready.checked; });
          submission.append(el('label', { style: { display: 'block' } }, [
            ready, ' The bed is still clear and my hardware and spool choices are unchanged.',
          ]), retry);
        }
      }
    } finally { checkSubmission.disabled = false; }
  }
  checkSubmission.addEventListener('click', inspectSubmission);

  async function start(ready, options, button) {
    if (!ready.checked || pendingId || !report?.ready_for_confirmation) return;
    const id = crypto.randomUUID().replaceAll('-', '');
    // Keep the request ID across navigation or a lost response; never invent
    // another Start to recover an uncertain result.
    remember(id, {
      id, printer_id: picker.value, choices: { ...choices },
      inventory_fingerprint: report.inventory.fingerprint,
      nozzle_diameter: report.requirements.nozzle_diameter, bed_type: report.requirements.bed_type,
      ready_confirmed: true, options,
    });
    button.disabled = true;
    ready.disabled = true;
    await sendPending();
  }

  async function sendPending() {
    if (submitting || !pendingId || !pendingBody) return;
    submitting = true; checkSubmission.disabled = true;
    submission.textContent = 'Recording the request and staging the saved slice…';
    try {
      const response = await app.api.postJson(`/library/captures/${encodeURIComponent(capture.id)}/replay`, pendingBody, { signal });
      if (signal.aborted) return;
      if (response.ok) showSubmission(response.data);
      else submission.textContent = `${response.message || 'The submission response was lost.'} Check submission before preparing another print.`;
    } finally { submitting = false; checkSubmission.disabled = false; }
  }

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
        report.ready_for_confirmation = false;
        const ready = content.querySelector('[aria-label="Hardware, bed and materials checked"]');
        if (ready) ready.checked = false;
        status.textContent = 'Choices changed. Check the mapping against a fresh inventory.';
        const button = content.querySelector('[data-replay-start]');
        if (button) button.disabled = true;
      });
      group.append(select);
      content.append(group);
    }
    const issues = el('ul');
    for (const issue of report.issues) issues.append(el('li', { text: issue }));
    content.append(issues);
    for (const note of report.review_notes) content.append(el('p', { class: 't-caption', text: note }));
    content.append(el('p', { class: 't-caption', text: report.dispatch_note }));
    if (report.dispatch_available && !pendingId) {
      const options = { bed_leveling: true, flow_cali: false, vibration_cali: false, layer_inspect: true, timelapse: false };
      const saved = [...(capture.attempts || [])].sort((a, b) => b.created_at - a.created_at).find(attempt => attempt.start_options)?.start_options;
      for (const key of Object.keys(options)) if (typeof saved?.[key] === 'boolean') options[key] = saved[key];
      const settings = el('details', {}, [el('summary', { text: 'Start options' })]);
      const ready = el('input', { type: 'checkbox', 'aria-label': 'Hardware, bed and materials checked' });
      const startButton = el('button', { class: 'btn btn--primary', text: 'Start print', disabled: true, 'data-replay-start': '' });
      for (const [key, label] of Object.entries({ bed_leveling: 'Bed leveling', flow_cali: 'Flow calibration', vibration_cali: 'Vibration calibration', layer_inspect: 'Layer inspection', timelapse: 'Timelapse' })) {
        const input = el('input', { type: 'checkbox', checked: options[key] });
        input.addEventListener('change', () => { options[key] = input.checked; ready.checked = false; startButton.disabled = true; });
        settings.append(el('label', { style: { display: 'block', marginTop: '8px' } }, [input, ` ${label}`]));
      }
      ready.addEventListener('change', () => {
        const printing = ['RUNNING', 'PREPARE', 'PAUSE'].includes(report.printer_state);
        startButton.disabled = !ready.checked || !report.ready_for_confirmation || printing || !!pendingId;
      });
      startButton.addEventListener('click', () => start(ready, { ...options }, startButton));
      content.append(settings, el('label', { style: { display: 'block', marginTop: '16px' } }, [
        ready, ` I checked this P1S: bed clear, ${spec.nozzle_diameter} mm nozzle and ${spec.bed_type} installed, and enough suitable material in the selected spools for the saved profiles.`,
      ]), startButton);
    }
  }

  async function run() {
    if (busy || pendingId || !picker.value) return;
    busy = true; check.disabled = true; picker.disabled = true;
    if (report) report.ready_for_confirmation = false;
    const ready = content.querySelector('[aria-label="Hardware, bed and materials checked"]');
    if (ready) ready.checked = false;
    const startButton = content.querySelector('[data-replay-start]');
    if (startButton) startButton.disabled = true;
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
    if (pendingId) { pendingView(); await inspectSubmission(); }
    else await run();
  } catch (error) {
    if (!signal.aborted) status.textContent = error.message || 'Could not load printers.';
  }
}
