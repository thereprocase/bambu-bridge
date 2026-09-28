import { el } from './ui.js';
import { mountNativeRecovery } from './native-recovery.js';

// Dedicated route keeps recovery reachable without navigating setup controls.
export function mount(root, app) {
  const main = el('main', { class: 'app-main' });
  const shell = el('div', { class: 'app-shell' }, [main]);
  main.appendChild(el('header', { class: 'topbar' }, [
    el('h1', { class: 'topbar__title', text: 'Bridge queue' }),
  ]));
  root.appendChild(shell);
  return mountNativeRecovery(main, app);
}
