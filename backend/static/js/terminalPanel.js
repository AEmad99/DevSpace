// static/js/terminalPanel.js
// Interactive terminal panel — a real PTY rendered with xterm.js, wired to the
// backend over a WebSocket (/api/workspace/pty). Keystrokes are streamed to a
// persistent shell attached to a pseudo-terminal, and its raw output is written
// straight back, so REPLs, full-screen TUIs, and long-running interactive
// agents work exactly as in a native terminal. The shell starts in the Code
// Workspace root. xterm + its fit addon are vendored in /static/lib and
// lazy-loaded the first time the panel opens.
//
// Protocol (matches routes/code_workspace_routes.py):
//   client -> server  binary = keystroke bytes ; text = JSON {type:'resize',cols,rows}
//   server -> client  binary = PTY output bytes ; text = JSON {type:'exit'|'error',...}

import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const MODAL_ID = 'terminal-modal';
// Terminal glyph reused in the header and the minimized-dock chip.
const TERM_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>';

let _modal = null;
let _term = null;
let _fit = null;
let _ws = null;
let _loaded = false;
let _ro = null;
let _wired = false;
let _fitRaf = 0;
const _enc = new TextEncoder();

function _loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = () => rej(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });
}

async function _ensureXterm() {
  if (window.Terminal && window.FitAddon) return true;
  if (!_loaded) {
    _loaded = true;
    if (!document.querySelector('link[data-xterm]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet'; link.href = '/static/lib/xterm.css'; link.dataset.xterm = '1';
      document.head.appendChild(link);
    }
    try {
      await _loadScript('/static/lib/xterm.js');
      await _loadScript('/static/lib/xterm-addon-fit.js');
    } catch (e) { return false; }
  }
  return !!(window.Terminal && window.FitAddon);
}

function _theme() {
  const cs = getComputedStyle(document.documentElement);
  const g = (n, d) => (cs.getPropertyValue(n).trim() || d);
  const bg = g('--hl-bg', g('--bg', '#1e2228'));
  const fg = g('--fg', '#cfd8e3');
  const accent = g('--accent', g('--red', '#61afef'));
  return {
    background: bg, foreground: fg, cursor: accent, cursorAccent: bg,
    selectionBackground: 'rgba(120,170,255,0.28)',
    black: '#2b2f37', red: '#e06c75', green: g('--green', '#98c379'), yellow: '#e5c07b',
    blue: '#61afef', magenta: '#c678dd', cyan: '#56b6c2', white: '#d7dae0',
    brightBlack: '#5c6370', brightRed: '#e06c75', brightGreen: '#98c379', brightYellow: '#e5c07b',
    brightBlue: '#61afef', brightMagenta: '#c678dd', brightCyan: '#56b6c2', brightWhite: '#fff',
  };
}

function _buildModal() {
  if (_modal) return _modal;
  _modal = document.getElementById(MODAL_ID);
  if (!_modal) return null;
  _modal.innerHTML = `
    <div class="modal-content" role="dialog" aria-label="Terminal" style="background:var(--bg)">
      <div class="modal-header">
        <h4>${TERM_ICON}<span style="margin-left:6px">Terminal</span></h4>
        <div class="term-actions">
          <button class="term-restart" id="term-restart" title="Start a new shell" hidden>⟳ Restart</button>
          <button class="close-btn" id="term-min" title="Minimize (keeps the shell running)" aria-label="Minimize Terminal">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="18" x2="19" y2="18"/></svg>
          </button>
          <button class="close-btn" id="term-close" title="Close (ends the shell)" aria-label="Close Terminal">✖</button>
        </div>
      </div>
      <div class="modal-body">
        <div class="term-wrap">
          <div class="term-host" id="term-host"></div>
          <div class="term-status" id="term-status">
            <span class="term-status-dot" id="term-status-dot"></span>
            <span id="term-status-text">Starting…</span>
          </div>
        </div>
      </div>
    </div>`;
  _modal.querySelector('#term-close').addEventListener('click', () => Modals.close(MODAL_ID));
  _modal.querySelector('#term-min').addEventListener('click', () => Modals.minimize(MODAL_ID));
  _modal.querySelector('#term-restart').addEventListener('click', () => _restart());
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  makeWindowDraggable(_modal, { content, header, skipSelector: 'button, input, select' });
  return _modal;
}

function _setStatus(state, text) {
  if (!_modal) return;
  const dot = _modal.querySelector('#term-status-dot');
  const txt = _modal.querySelector('#term-status-text');
  if (dot) dot.dataset.state = state;
  if (txt) txt.textContent = text;
  const restart = _modal.querySelector('#term-restart');
  if (restart) restart.hidden = (state === 'connected' || state === 'connecting');
}

// Close semantics: the X button ends the session — dropping the socket makes
// the backend terminate the shell — then hides the panel. (Minimize, by
// contrast, keeps the socket/shell alive and just stashes the panel to the
// dock.) Registered as the modal manager's closeFn.
function _endSession() {
  if (_ws) { try { _ws.close(); } catch {} _ws = null; }
  if (_modal) _modal.classList.add('hidden');
}

function _wsOpen() { return _ws && _ws.readyState === WebSocket.OPEN; }

// Refit xterm to its container, coalesced to one call per animation frame so a
// continuous drag-resize / dock / OS-window-snap stays smooth without spamming
// the PTY with SIGWINCH. fit() only resizes when the cell grid actually changes,
// so this is cheap.
function _scheduleFit() {
  if (_fitRaf) return;
  _fitRaf = requestAnimationFrame(() => {
    _fitRaf = 0;
    if (!_fit || !_modal || _modal.classList.contains('hidden')) return;
    try { _fit.fit(); } catch {}
  });
}

function _connect() {
  if (_wsOpen() || (_ws && _ws.readyState === WebSocket.CONNECTING)) return;
  // Reaching here means we're starting a brand-new shell (first open, after
  // Close, or after the previous one exited) — clear any stale output. Note
  // minimize/hide keep the socket open, so this path never runs for them and
  // a restored panel keeps its live scrollback.
  if (_term) _term.reset();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const cols = (_term && _term.cols) || 80;
  const rows = (_term && _term.rows) || 24;
  const url = `${proto}://${location.host}/api/workspace/pty?cols=${cols}&rows=${rows}`;
  _setStatus('connecting', 'Connecting…');
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    _setStatus('closed', 'Connection failed');
    return;
  }
  ws.binaryType = 'arraybuffer';
  _ws = ws;
  ws.onopen = () => {
    _setStatus('connected', 'Connected');
    try { _fit.fit(); } catch {}
    if (_wsOpen()) ws.send(JSON.stringify({ type: 'resize', cols: _term.cols, rows: _term.rows }));
    _term.focus();
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === 'exit') {
        const code = (m.code === null || m.code === undefined) ? '' : ` (code ${m.code})`;
        _term.write(`\r\n\x1b[38;5;244m[process exited${code}]\x1b[0m\r\n`);
      } else if (m.type === 'error') {
        _term.write(`\r\n\x1b[31m${m.message || 'terminal error'}\x1b[0m\r\n`);
      }
      return;
    }
    _term.write(new Uint8Array(ev.data));
  };
  ws.onclose = () => {
    if (_ws !== ws) return;  // superseded by a newer connection
    _ws = null;
    _setStatus('closed', 'Disconnected — Restart to start a new shell');
  };
  ws.onerror = () => { /* onclose fires next; status handled there */ };
}

function _restart() {
  if (_ws) { try { _ws.close(); } catch {} _ws = null; }
  _connect();  // _connect() resets the screen for the fresh shell
}

function _ensureTerm(host) {
  if (_term) return;
  _term = new window.Terminal({
    convertEol: false, cursorBlink: true, disableStdin: false, scrollback: 8000,
    fontFamily: "'Fira Code', ui-monospace, monospace", fontSize: 13, theme: _theme(),
  });
  _fit = new window.FitAddon.FitAddon();
  _term.loadAddon(_fit);
  _term.open(host);
  try { _fit.fit(); } catch {}

  // Keystrokes -> PTY (raw bytes). Dropped silently while disconnected.
  _term.onData((data) => { if (_wsOpen()) _ws.send(_enc.encode(data)); });
  // Geometry changes (from fit) -> PTY winsize.
  _term.onResize(({ cols, rows }) => {
    if (_wsOpen()) _ws.send(JSON.stringify({ type: 'resize', cols, rows }));
  });

  // Keep fitted on any size change: container resize (drag-resize, edge dock,
  // sidebar collapse) via ResizeObserver, and OS-window snap/resize via the
  // window resize event. min-width:0 on the flex chain lets the host actually
  // shrink so these fire — otherwise the grid would overflow and clip.
  _ro = new ResizeObserver(() => _scheduleFit());
  _ro.observe(host);

  if (!_wired) {
    _wired = true;
    window.addEventListener('resize', _scheduleFit);
    // Best-effort: close the shell when the app/page goes away.
    window.addEventListener('beforeunload', () => { try { _ws && _ws.close(); } catch {} });
  }
}

async function openTerminal() {
  if (!_buildModal()) return;
  _modal.classList.remove('hidden', 'modal-minimized');
  Modals.register(MODAL_ID, {
    railBtnId: 'rail-terminal', sidebarBtnId: 'tool-terminal-btn',
    label: 'Terminal', icon: TERM_ICON,
    closeFn: () => _endSession(),
    restoreFn: () => { _scheduleFit(); setTimeout(() => { _scheduleFit(); _term && _term.focus(); }, 60); },
  });
  const ok = await _ensureXterm();
  const host = _modal.querySelector('#term-host');
  if (!ok || !host) {
    if (host) host.innerHTML = '<div class="term-fallback">Could not load the terminal renderer (xterm.js).</div>';
    return;
  }
  _ensureTerm(host);

  // Connect on first open or after the previous shell ended; otherwise the
  // session is still live (panel was just hidden/minimized) — refit and refocus.
  if (!_wsOpen() && !(_ws && _ws.readyState === WebSocket.CONNECTING)) {
    _connect();
  } else {
    _scheduleFit();
  }
  setTimeout(() => { _scheduleFit(); _term && _term.focus(); }, 60);
}

export function initTerminalPanel() {
  const open = (e) => {
    e.preventDefault();
    // If minimized, restore (and refit) instead of re-opening a fresh panel.
    if (Modals.toggle && Modals.toggle(MODAL_ID)) return;
    openTerminal();
  };
  document.getElementById('rail-terminal')?.addEventListener('click', open);
  document.getElementById('tool-terminal-btn')?.addEventListener('click', open);
}

initTerminalPanel();
export { openTerminal };
