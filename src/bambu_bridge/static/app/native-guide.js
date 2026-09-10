import { el, toast } from './ui.js';

export function mountNativeGuide(parent, app, current, manual) {
  let alive = true, timer = null, checks = 0;
  const live = () => alive && current() && parent.isConnected;
  const guide = el('div', { class: 'native-guide' });
  const step = (number, title, children) => el('section', { class: 'native-step' }, [
    el('h3', { class: 'native-step__title' }, [el('span', { class: 'native-step__number', text: String(number) }), el('span', { text: title })]),
    ...children,
  ]);
  const setupMessage = el('p', { class: 'field__hint', role: 'status' });
  const command = el('textarea', { class: 'input native-command', readonly: '', spellcheck: 'false', 'aria-label': 'Windows setup command' });
  const preview = el('details', { class: 'native-manual' }, [el('summary', { text: 'Review the setup command' }), command]);
  preview.hidden = true;
  const copy = el('button', { class: 'btn btn--primary', text: 'Copy Windows setup command' });
  copy.addEventListener('click', async () => {
    copy.disabled = true; setupMessage.textContent = 'Preparing setup for your bridge…';
    const result = await app.api.api('/native/setup', { cache: 'no-store' });
    if (!live()) return;
    copy.disabled = false;
    if (!result.ok) { setupMessage.textContent = result.message || 'Could not prepare setup. Open Manual setup below to save your existing native code, then try again.'; manual.open = true; return; }
    command.value = result.data.script; preview.hidden = false;
    try { await navigator.clipboard.writeText(command.value); toast('Setup command copied.'); setupMessage.textContent = 'Copied. Open Windows PowerShell normally, paste, and press Enter.'; }
    catch { preview.open = true; command.focus(); command.select(); setupMessage.textContent = 'Select and copy the command below, then paste it into Windows PowerShell.'; }
  });
  const windows = el('div', { class: 'stack gap-2' }, [
    el('ol', {}, [
      el('li', { text: 'Save your work and close Orca.' }),
      el('li', { text: 'Copy the setup command below. Open Start, type Windows PowerShell, and open it normally.' }),
      el('li', { text: 'Paste the command and press Enter. If Windows asks to update Orca’s certificate file, choose Yes.' }),
      el('li', { text: 'Wait for the green “Ready!” message. The helper saves the correct address and code, trusts this bridge in Orca, and backs up changed files.' }),
    ]), copy, setupMessage, preview,
    el('p', { class: 'field__hint', text: 'Run this on each computer you use with Orca. The command contains your native printer code; keep it private. Run a fresh command after an Orca update if the connection stops working.' }),
  ]);
  const platform = el('select', { class: 'select', 'aria-label': 'Computer operating system' }, [
    el('option', { value: 'windows', text: 'Windows — guided setup' }),
    el('option', { value: 'manual', text: 'macOS or Linux — manual setup' }),
  ]);
  const other = el('p', { class: 'field__hint', text: 'Open Manual setup below for the address, code and certificate. The automatic helper currently supports Windows.' });
  other.hidden = true;
  platform.addEventListener('change', () => { windows.hidden = platform.value !== 'windows'; other.hidden = !windows.hidden; if (windows.hidden) manual.open = true; });
  guide.append(step(2, 'Set up this computer', [
    el('p', { text: 'Open this dashboard on the computer where Orca will run. Away from home? Connect that computer to your Tailscale network first.' }),
    platform, windows, other,
  ]));
  guide.append(step(3, 'Open your printer in Orca', [
    el('p', { text: 'Open Orca → Device → printer list → Bridge P1S. This is a separate printer from your original P1S. The helper adds its own serial, address and code; the original printer entry stays in place.' }),
    el('p', { text: 'Use a Bambu Lab P1S printer preset with “Use 3rd-party print host” off. Press Play in the camera panel to start live view.' }),
    el('p', { class: 'field__hint', text: 'When printing, choose AMS slots or the external spool in Orca’s print dialog. The physical printer decides which combinations it supports.' }),
  ]));
  const printer = el('p', { class: 'native-check', text: 'Printer: not checked yet' });
  const camera = el('p', { class: 'native-check', text: 'Camera: press Play in Orca, then check' });
  const message = el('p', { class: 'field__hint', role: 'status' });
  const check = el('button', { class: 'btn btn--primary', text: 'Check this computer' });
  async function refresh() {
    const result = await app.api.api('/native/setup-status', { cache: 'no-store' });
    if (!live()) return;
    if (!result.ok) { message.textContent = result.message || 'Could not check the connection.'; clearInterval(timer); timer = null; check.disabled = false; return; }
    const state = result.data;
    printer.textContent = state.printer_connected ? '✓ Printer connected on this computer' : 'Waiting for Orca’s printer connection';
    camera.textContent = state.camera_streaming ? '✓ Camera frames reaching this computer' : 'Waiting for camera — press Play in Orca';
    printer.classList.toggle('is-ready', state.printer_connected);
    camera.classList.toggle('is-ready', state.camera_streaming);
    message.textContent = state.printer_connected && state.camera_streaming ? 'Connected. Your printer and camera are ready.'
      : state.printer_connected ? 'Printer connection is working. If the camera times out, run a fresh setup command, reopen Orca, and press Play again.'
      : 'Open Orca’s Device tab and select Bridge P1S. A connection from another computer does not count here.';
    if (++checks >= 40 || (state.printer_connected && state.camera_streaming)) { clearInterval(timer); timer = null; check.disabled = false; }
  }
  check.addEventListener('click', () => { clearInterval(timer); checks = 0; check.disabled = true; timer = setInterval(refresh, 3000); refresh(); });
  guide.append(step(4, 'Check printer and camera', [printer, camera, check, message,
    el('p', { class: 'field__hint', text: 'This checks live connections from the computer displaying this page. Keep Orca open while checking. Seeing both checks does not start a print.' }),
  ]));
  parent.appendChild(guide);
  return () => { alive = false; clearInterval(timer); command.value = ''; };
}
