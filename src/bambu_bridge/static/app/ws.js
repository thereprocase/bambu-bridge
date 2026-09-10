// ws.js — the live-status WebSocket client.
//
// Build-free, dependency-free. One connection per printer at a time.
//
// Wire sequence (contract §5.1): hello -> snapshot -> delta… -> event…, with
// server ping every 30s. We reply {type:"pong"} and ALSO send {type:"pong"}
// every 30s as our own keepalive.
//
// Reconnect backoff: 1, 2, 4, 8, 30s (contract §5.4). Reset to 1s on a clean
// snapshot. Close-code handling (contract §5.2):
//   1008 unauthorized   -> stop; bounce to the Key screen
//   1008 unknown_printer -> stop; bounce to the printer picker / Settings
//   4000 protocol_mismatch -> stop; "app update needed"
//   1011 / 1006 abnormal -> reconnect with backoff
//
// State ingestion is delegated to store.js (applySnapshot/applyDelta/setConnected).
// Events are surfaced to a caller-supplied onEvent so dashboard/ui own the
// banners/toasts (the §13 alert derivation is a UI concern, not a wire concern).

import { wsStatusUrl } from './api.js';
import { applySnapshot, applyDelta, setConnected } from './store.js';

const BACKOFF = [1000, 2000, 4000, 8000, 30000];
const KEEPALIVE_MS = 30000;

/**
 * Connect to a printer's status stream. Returns a handle with .close().
 * Only one live connection should exist at a time; the caller (dashboard)
 * closes the previous before opening a new one.
 *
 * @param {string} printerId
 * @param {object} [cb]
 * @param {(name:string, data:object)=>void} [cb.onEvent]      §13 named events
 * @param {(info:{protocol_version:number})=>void} [cb.onHello]
 * @param {()=>void} [cb.onUnauthorized]    1008 unauthorized -> Key screen
 * @param {()=>void} [cb.onUnknownPrinter]  1008 unknown_printer -> picker
 * @param {()=>void} [cb.onProtocolMismatch] 4000 -> "app update needed"
 * @param {(open:boolean)=>void} [cb.onConnState] notified true/false on open/close
 * @returns {{close:()=>void}}
 */
export function connectStatus(printerId, cb = {}) {
  let ws = null;
  let keepaliveTimer = null;
  let reconnectTimer = null;
  let attempt = 0;            // backoff index
  let stopped = false;        // hard stop (auth/unknown/protocol) — do not retry
  let gotSnapshot = false;

  function clearTimers() {
    if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  }

  function scheduleReconnect() {
    if (stopped) return;
    const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)];
    attempt += 1;
    reconnectTimer = setTimeout(open, delay);
  }

  function open() {
    if (stopped) return;
    gotSnapshot = false;
    let socket;
    try {
      socket = new WebSocket(wsStatusUrl(printerId));
    } catch {
      // bad URL / no key — treat as a recoverable network blip
      setConnected(printerId, false);
      cb.onConnState && cb.onConnState(false);
      scheduleReconnect();
      return;
    }
    ws = socket;

    socket.onopen = () => {
      // keepalive: client also sends {type:'pong'} every 30s (contract §5.1.5)
      keepaliveTimer = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          try { socket.send(JSON.stringify({ type: 'pong' })); } catch { /* */ }
        }
      }, KEEPALIVE_MS);
    };

    socket.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      switch (msg.type) {
        case 'hello':
          cb.onHello && cb.onHello(msg);
          // contract §5.2: protocol_version != 1 is reserved; the wire close
          // (4000) is the authoritative signal, so we just surface hello.
          break;
        case 'snapshot':
          gotSnapshot = true;
          attempt = 0;                       // reset backoff on a good snapshot
          applySnapshot(printerId, msg.data);
          cb.onConnState && cb.onConnState(true);
          break;
        case 'delta':
          // never render a delta alone — store deep-merges into the snapshot
          if (gotSnapshot) applyDelta(printerId, msg.data);
          break;
        case 'event':
          cb.onEvent && cb.onEvent(msg.event, msg.data || {});
          break;
        case 'ping':
          if (socket.readyState === WebSocket.OPEN) {
            try { socket.send(JSON.stringify({ type: 'pong' })); } catch { /* */ }
          }
          break;
        default:
          break;
      }
    };

    socket.onclose = (ev) => {
      clearTimers();
      setConnected(printerId, false);
      cb.onConnState && cb.onConnState(false);
      ws = null;

      const reason = ev.reason || '';
      if (ev.code === 1008 && reason === 'unauthorized') {
        stopped = true; cb.onUnauthorized && cb.onUnauthorized(); return;
      }
      if (ev.code === 1008 && reason === 'unknown_printer') {
        stopped = true; cb.onUnknownPrinter && cb.onUnknownPrinter(); return;
      }
      if (ev.code === 4000) {
        stopped = true; cb.onProtocolMismatch && cb.onProtocolMismatch(); return;
      }
      if (ev.code === 1000 && stopped) return;   // we closed it deliberately
      scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose fires after onerror; let onclose drive reconnect.
    };
  }

  open();

  return {
    close() {
      stopped = true;
      clearTimers();
      if (ws) {
        try { ws.close(1000, 'normal_close'); } catch { /* */ }
        ws = null;
      }
      setConnected(printerId, false);
    },
  };
}
