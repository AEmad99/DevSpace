// static/js/codeWorkspace.js
// Code Workspace panel — file tree, code editor, diff viewer.
// Phase 2: draggable modal + 3 panes. Tree click → load file; Save → write + diff.
// Live-refresh: listens for 'workspace:diff-applied' CustomEvent (dispatched
// from chat.js _attachDiffApprovalButtons hook) and re-loads the open file
// when the agent edits it.
//
// Editor: the textarea+highlight.js overlay is the *fallback*. On the first
// file open we lazy-load Monaco from /static/lib/monaco/vs/loader.js (AMD,
// vendored — no bundler) and mount a real Monaco editor inside
// #cw-editor-wrap. The textarea is kept in the DOM (hidden) so the
// fallback path stays intact when Monaco fails to load or is blocked
// (e.g. CSP, offline Tauri build). `_getEditorValue()` is the only place
// the rest of this module reads the buffer from — it picks whichever
// editor is live.
//
// LSP: when a Python/TypeScript/Rust file is opened AND the server
// reports the corresponding /api/lsp/availability entry === true, we
// open a WebSocket to /api/lsp/{lang}?path=<workspace_root> and proxy
// JSON-RPC frames between Monaco and the language-server subprocess.
// JavaScript files share the TypeScript server (typescript-language-server
// handles both). Adding a new language is a config-only change on the
// server side (see backend/src/lsp_bridge.py:LSP_SERVERS) plus a branch
// in `_lspLanguageFor` here.

import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';
import { openWorkspaceBrowser } from './workspace.js';

const API = '/api/workspace';
const MONACO_BASE = '/static/lib/monaco';  // vendored AMD bundle
const MONACO_LOADER_URL = `${MONACO_BASE}/vs/loader.js`;

let _modal = null;
let _root = null;          // canonical workspace root path
let _currentPath = null;   // currently open file path
let _dirty = false;        // editor has unsaved changes
let _currentLang = 'plaintext'; // auto-detected highlight language for the open file
let _treeSearchTimeout = null;
let _gitMounted = false;        // lazy-mount the Git tab on first open
let _activeTab = 'files';

// Monaco state — populated lazily on first file open.
let _monacoLoading = null;      // Promise<boolean>: true when load succeeded
let _monacoEditor = null;       // the IFStandaloneCodeEditor instance
let _monacoModel = null;        // current TextModel (one per file)
let _lspSocket = null;          // active WebSocket for the open file
let _lspLanguage = null;        // 'python' | null
let _lspState = 'pending';      // 'pending' | 'ok' | 'unavailable' | 'error'
let _lspUnavailableReason = '';
// Set by the cross-file goto-def editor opener when the target file
// isn't mounted yet. After `openCodeWorkspaceAt` finishes, `_openFile`
// consumes this to scroll the cursor to the target location. Null at
// every other time. The opener clears it in a `finally` so a failed
// open doesn't strand the reveal.
let _pendingReveal = null;

// Crisp SVG glyphs (theme-colored via currentColor — see .cw-tree-icon rules)
// instead of emoji, so folders/files read as one coherent icon set.
const ICON_DIR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.82 1.2a2 2 0 0 0 1.69.9H19a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICON_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

function _treeIcon(isDir) { return isDir ? ICON_DIR : ICON_FILE; }

// Extension → highlight.js language id. Covers the languages bundled in
// highlight.min.js; anything not here (or not registered) falls back to hljs
// content auto-detection in _resolveLang().
const _LANG_BY_EXT = {
  py: 'python', pyw: 'python', pyi: 'python',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript',
  json: 'json', jsonc: 'json', json5: 'json',
  html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', xhtml: 'xml', vue: 'xml',
  css: 'css', scss: 'scss', sass: 'scss', less: 'less',
  md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  sh: 'bash', bash: 'bash', zsh: 'bash', ksh: 'bash',
  rs: 'rust', go: 'go', sql: 'sql',
  yml: 'yaml', yaml: 'yaml',
  toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', env: 'ini', properties: 'ini',
  java: 'java', kt: 'kotlin', kts: 'kotlin', scala: 'scala', groovy: 'groovy',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp', hxx: 'cpp', hh: 'cpp',
  cs: 'csharp', rb: 'ruby', php: 'php', pl: 'perl', lua: 'lua', r: 'r', dart: 'dart', swift: 'swift',
  ps1: 'powershell', psm1: 'powershell', bat: 'dos', cmd: 'dos',
  dockerfile: 'dockerfile', makefile: 'makefile', mk: 'makefile',
  diff: 'diff', patch: 'diff', graphql: 'graphql', gql: 'graphql', proto: 'protobuf',
  txt: 'plaintext', text: 'plaintext', log: 'plaintext',
};

// Resolve the highlight language for a file: special filenames first, then the
// extension map, then a one-time content guess (high-confidence only). Returns
// a registered hljs language id, or 'plaintext' when nothing fits.
function _resolveLang(path, extHint, content) {
  const hljs = window.hljs;
  if (!hljs) return 'plaintext';
  const name = (path || '').split(/[\\/]/).pop().toLowerCase();
  if (name === 'dockerfile' || name.startsWith('dockerfile.')) {
    if (hljs.getLanguage('dockerfile')) return 'dockerfile';
  }
  if (name === 'makefile' || name === 'gnumakefile') {
    if (hljs.getLanguage('makefile')) return 'makefile';
  }
  const ext = (extHint || name.split('.').pop() || '').toLowerCase();
  const mapped = _LANG_BY_EXT[ext];
  if (mapped === 'plaintext') return 'plaintext';
  if (mapped && hljs.getLanguage(mapped)) return mapped;
  // Unknown/unregistered extension → let hljs guess, but only trust a
  // confident result so we don't mislabel prose or config as code.
  try {
    const auto = hljs.highlightAuto(content || '');
    if (auto && auto.language && auto.relevance >= 7) return auto.language;
  } catch { /* fall through */ }
  return 'plaintext';
}

// --- helpers ---

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function relPath(full) {
  if (!_root) return full;
  // Show paths relative to the workspace root for readability.
  if (full === _root) return '.';
  if (full.startsWith(_root + '/') || full.startsWith(_root + '\\')) {
    return full.slice(_root.length + 1);
  }
  return full;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = j.detail || detail; } catch {}
    const e = new Error(detail);
    e.status = r.status;
    throw e;
  }
  return r.json();
}

// --- modal setup ---

function _buildModal() {
  if (_modal) return _modal;
  _modal = document.getElementById('code-workspace-modal');
  if (!_modal) return null;
  _modal.innerHTML = `
    <div class="modal-content" role="dialog" aria-label="Code Workspace" style="background:var(--bg)">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>Code Workspace</h4>
        <div class="cw-tabs hidden" id="cw-tabs">
          <button class="cw-tab active" data-tab="files" type="button">Files</button>
          <button class="cw-tab" data-tab="git" type="button">Git</button>
        </div>
        <button class="close-btn" id="cw-close" aria-label="Close Code Workspace">✖</button>
      </div>
      <div class="modal-body">
        <div class="cw-layout" id="cw-layout"></div>
        <div class="cw-git-view hidden" id="cw-git-view"></div>
      </div>
    </div>`;
  // Close button — route through the modal manager so the registered closeFn
  // (which hides the modal) runs and the manager's teardown stays in sync,
  // mirroring the terminal panel's close handling.
  _modal.querySelector('#cw-close').addEventListener('click', () => Modals.close('code-workspace-modal'));
  // Tabs (Files | Git)
  _modal.querySelectorAll('.cw-tab').forEach(tab =>
    tab.addEventListener('click', () => _switchTab(tab.dataset.tab)));
  // Draggable
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  makeWindowDraggable(_modal, { content, header, skipSelector: 'button, input, select' });
  return _modal;
}

function _close() {
  if (_modal) _modal.classList.add('hidden');
}

function _switchTab(name) {
  if (!_modal) return;
  _activeTab = name;
  _modal.querySelectorAll('.cw-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  const layout = _modal.querySelector('#cw-layout');
  const git = _modal.querySelector('#cw-git-view');
  if (layout) layout.classList.toggle('hidden', name !== 'files');
  if (git) git.classList.toggle('hidden', name !== 'git');
  if (name === 'git' && git) {
    import('./gitPanel.js').then(mod => {
      if (!_gitMounted) { mod.mountGitPanel(git); _gitMounted = true; }
      else mod.refreshGitPanel();
    }).catch(() => {});
  }
}

// --- workspace picker ---

// Persist `path` as the Code Workspace root (server-side) and open the panel.
// Errors surface inline in the picker when it's visible, else as an alert.
async function _setRoot(path) {
  const errEl = _modal && _modal.querySelector('#cw-picker-error');
  if (errEl) errEl.style.display = 'none';
  try {
    const res = await api('POST', '/current', { path });
    _root = res.path;
    _showPanel();
    return true;
  } catch (e) {
    const msg = e.message || 'Could not set workspace.';
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
    else alert(msg);
    return false;
  }
}

async function _pickerSet() {
  const input = _modal.querySelector('#cw-picker-input');
  const errEl = _modal.querySelector('#cw-picker-error');
  const path = (input.value || '').trim();
  if (errEl) errEl.style.display = 'none';
  if (!path) { errEl.textContent = 'Enter a folder path.'; errEl.style.display = 'block'; return; }
  await _setRoot(path);
}

// Browse for the workspace root: native OS folder picker in the desktop app, or
// the in-app directory browser as a fallback. The chosen folder becomes the new
// Code Workspace root.
function _browseForFolder() {
  openWorkspaceBrowser({
    startPath: _root || '',
    title: 'Select code workspace folder',
    onSelect: (p) => _setRoot(p),
  });
}

async function _loadRoot() {
  try {
    const res = await api('GET', '/current');
    if (res.ok && res.path) {
      _root = res.path;
      return true;
    }
  } catch {}
  return false;
}

// --- 3-pane panel (tree | editor | diff) ---

function _showPanel() {
  const layout = _modal.querySelector('#cw-layout');
  // Workspace is set → enable the Files/Git tabs, start on Files, and re-mount
  // Git fresh next time it opens (the root may have changed).
  _gitMounted = false;
  _activeTab = 'files';
  _modal.querySelector('#cw-tabs')?.classList.remove('hidden');
  _modal.querySelectorAll('.cw-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'files'));
  const _gv = _modal.querySelector('#cw-git-view');
  if (_gv) { _gv.classList.add('hidden'); _gv.innerHTML = ''; }
  layout.classList.remove('hidden');
  const rootShort = _root ? _root.split(/[\\/]/).pop() : '';
  layout.innerHTML = `
    <div class="cw-tree-pane">
      <div class="cw-tree-toolbar">
        <input type="text" class="cw-tree-search" id="cw-tree-search" placeholder="Search files..." autocomplete="off">
        <button class="cw-ws-change" id="cw-ws-change" title="Change workspace folder">⇄</button>
      </div>
      <div class="cw-tree-list" id="cw-tree-list"></div>
    </div>
    <div class="cw-editor-pane">
      <div class="cw-editor-toolbar">
        <span class="cw-editor-path" id="cw-editor-path">No file selected</span>
        <span class="cw-editor-lang" id="cw-editor-lang" title="Auto-detected language" hidden></span>
        <span id="cw-lsp-pill" class="cw-lsp-pill cw-lsp-state-pending" title="Language server status">LSP: pending</span>
        <label class="cw-lsp-toggle" title="Enable or disable the language server for this session. Persists in localStorage.">
          <input type="checkbox" id="cw-lsp-enabled" checked>
          <span>LSP</span>
        </label>
        <button class="cw-save-btn" id="cw-save-btn" disabled>Save</button>
      </div>
      <div class="cw-editor-wrap" id="cw-editor-wrap">
        <pre class="cw-editor-highlight"><code id="cw-editor-code"></code></pre>
        <textarea class="cw-editor-textarea" id="cw-editor-textarea" spellcheck="false"></textarea>
      </div>
      <div class="cw-editor-empty" id="cw-editor-empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        <span>Select a file from the tree to edit</span>
      </div>
    </div>
    <div class="cw-diff-pane" id="cw-diff-pane">
      <div class="cw-diff-header" id="cw-diff-header" style="display:none">
        <span id="cw-diff-title">Diff</span>
        <span class="cw-diff-close" id="cw-diff-close">✖</span>
      </div>
      <div class="cw-diff-body" id="cw-diff-body"></div>
    </div>`;
  // Wire workspace change button
  layout.querySelector('#cw-ws-change').addEventListener('click', () => _showPickerOnly(true));
  // Wire tree search
  const search = layout.querySelector('#cw-tree-search');
  search.addEventListener('input', () => {
    clearTimeout(_treeSearchTimeout);
    _treeSearchTimeout = setTimeout(() => _treeSearch(search.value), 250);
  });
  // Wire save
  layout.querySelector('#cw-save-btn').addEventListener('click', () => _save());
  // Wire the Enable-LSP toggle. Persistence is localStorage under the
  // ``code.lsp_enabled`` key (default true). When the user flips it
  // off, ``_attachLsp`` short-circuits with state=unavailable and
  // reason "LSP disabled in toolbar"; when flipped back on, the
  // current file's LSP is re-attached automatically.
  try {
    const toggle = layout.querySelector('#cw-lsp-enabled');
    const stored = localStorage.getItem('code.lsp_enabled');
    if (stored !== null) toggle.checked = stored === 'true';
    toggle.addEventListener('change', () => {
      localStorage.setItem('code.lsp_enabled',
                            toggle.checked ? 'true' : 'false');
      // Re-attach (or detach) LSP for the currently-open file.
      _attachLsp(_currentLang);
    });
  } catch (_) { /* localStorage blocked; toggle is non-functional but
                   the rest of the editor still works. */ }
  // Wire textarea input → dirty + sync highlight overlay. This handler is
  // only the FALLBACK path — once Monaco is mounted, the textarea is
  // hidden and Monaco's onDidChangeModelContent callback drives _dirty.
  const ta = layout.querySelector('#cw-editor-textarea');
  const code = layout.querySelector('#cw-editor-code');
  ta.addEventListener('input', () => {
    if (_monacoEditor) return;  // Monaco is the live editor; ignore
    _dirty = true;
    _updateSaveBtn();
    code.textContent = ta.value;
    _highlight();
  });
  ta.addEventListener('scroll', () => {
    const hl = layout.querySelector('.cw-editor-highlight');
    if (hl) { hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; }
  });
  // Diff close
  layout.querySelector('#cw-diff-close').addEventListener('click', () => _closeDiff());
  // Load top-level tree
  _loadTree('');
}

// --- file tree ---

async function _loadTree(path) {
  const list = _modal.querySelector('#cw-tree-list');
  if (!list) return;
  list.innerHTML = '<div class="cw-tree-empty">Loading…</div>';
  try {
    const res = await api('GET', '/tree' + (path ? '?path=' + encodeURIComponent(path) : ''));
    _renderTreeList(list, res.entries, res.path, res.parent);
  } catch (e) {
    list.innerHTML = `<div class="cw-tree-empty">Error: ${esc(e.message)}</div>`;
  }
}

function _renderTreeList(list, entries, currentPath, parent) {
  list.innerHTML = '';
  // ".." to go up (but not above root)
  if (parent && _root && currentPath !== _root) {
    const up = document.createElement('div');
    up.className = 'cw-tree-item cw-dir';
    up.innerHTML = `<span class="cw-tree-chevron">▸</span><span class="cw-tree-icon">${ICON_DIR}</span>..`;
    up.addEventListener('click', () => _loadTree(parent));
    list.appendChild(up);
  }
  if (!entries.length) {
    list.innerHTML = '<div class="cw-tree-empty">(empty)</div>';
    return;
  }
  for (const e of entries) {
    const item = document.createElement('div');
    item.className = 'cw-tree-item' + (e.is_dir ? ' cw-dir' : '');
    item.dataset.path = e.path;
    if (_currentPath === e.path) item.classList.add('active');
    const chevron = e.is_dir ? '▸' : '';
    item.innerHTML = `<span class="cw-tree-chevron">${chevron}</span><span class="cw-tree-icon">${_treeIcon(e.is_dir)}</span>${esc(e.name)}`;
    item.addEventListener('click', () => {
      if (e.is_dir) {
        _loadTree(e.path);
      } else {
        _openFile(e.path);
      }
    });
    list.appendChild(item);
  }
}

async function _treeSearch(q) {
  const list = _modal.querySelector('#cw-tree-list');
  if (!list) return;
  if (!q.trim()) { _loadTree(''); return; }
  list.innerHTML = '<div class="cw-tree-empty">Searching…</div>';
  try {
    const res = await api('GET', '/search?q=' + encodeURIComponent(q.trim()));
    list.innerHTML = '';
    if (!res.hits.length) { list.innerHTML = '<div class="cw-tree-empty">No matches</div>'; return; }
    for (const h of res.hits) {
      const item = document.createElement('div');
      item.className = 'cw-tree-item' + (h.is_dir ? ' cw-dir' : '');
      item.dataset.path = h.path;
      if (_currentPath === h.path) item.classList.add('active');
      item.innerHTML = `<span class="cw-tree-chevron"></span><span class="cw-tree-icon">${_treeIcon(h.is_dir)}</span>${esc(relPath(h.path))}`;
      item.addEventListener('click', () => {
        if (h.is_dir) _loadTree(h.path);
        else _openFile(h.path);
      });
      list.appendChild(item);
    }
  } catch (e) {
    list.innerHTML = `<div class="cw-tree-empty">Error: ${esc(e.message)}</div>`;
  }
}

// --- file editor ---

async function _openFile(path) {
  if (_dirty && !confirm('Discard unsaved changes to the current file?')) return;
  try {
    const res = await api('GET', '/file?path=' + encodeURIComponent(relPath(path)));
    _currentPath = res.path;
    _dirty = false;
    const ta = _modal.querySelector('#cw-editor-textarea');
    const code = _modal.querySelector('#cw-editor-code');
    const pathEl = _modal.querySelector('#cw-editor-path');
    const empty = _modal.querySelector('#cw-editor-empty');
    const wrap = _modal.querySelector('#cw-editor-wrap');
    const saveBtn = _modal.querySelector('#cw-save-btn');
    ta.value = res.content;
    code.textContent = res.content;
    pathEl.textContent = relPath(res.path) + (res.truncated ? ' (truncated)' : '');
    pathEl.title = res.path;
    // Show editor, hide empty state
    if (empty) empty.classList.add('hidden');
    if (wrap) wrap.classList.add('cw-active');
    saveBtn.disabled = false;
    // Auto-detect the language (extension map → content guess) and show it in the
    // read-only badge. _highlight() reuses _currentLang on every keystroke, so no
    // re-detection (or flicker) while typing.
    _currentLang = _resolveLang(res.path, res.language, res.content);
    _setLangBadge(_currentLang);
    _highlight();
    // If Monaco is loaded (or can be loaded), mount it on this file. The
    // textarea+hljs overlay is the fallback when Monaco fails to load or
    // when the user has explicitly disabled the new editor.
    await _mountMonacoForFile(res.path, res.content, _currentLang);
    // If a cross-file goto-def opener stashed a reveal position for this
    // file, honour it now: the new model is on the editor. Clearing the
    // pending reveal is the opener's responsibility (in a `finally`),
    // so a navigation that fails still leaves us in a clean state.
    if (_pendingReveal && _monacoEditor) {
      try {
        _monacoEditor.setSelection(_pendingReveal);
        _monacoEditor.revealRangeInCenterIfOutsideViewport(_pendingReveal);
      } catch (_) { /* non-fatal: a bad range just doesn't scroll */ }
    }
    // (Re)attach the language server to the new file (Python only in v1).
    _attachLsp(_currentLang);
    _updateSaveBtn();
    // Mark the opened file active in the tree (match on the stored path so the
    // highlight lands on the right row instead of just clearing every row).
    _modal.querySelectorAll('.cw-tree-item').forEach(el =>
      el.classList.toggle('active', el.dataset.path === _currentPath));
  } catch (e) {
    alert('Could not open file: ' + e.message);
  }
}

function _setLangBadge(lang) {
  const badge = _modal?.querySelector('#cw-editor-lang');
  if (!badge) return;
  badge.textContent = (!lang || lang === 'plaintext') ? 'text' : lang;
  badge.hidden = false;
}

/**
 * Render the LSP status pill in the editor toolbar. Maps the
 * `_lspState` machine ('pending' | 'ok' | 'unavailable' | 'error')
 * to a colour-coded label and a tooltip explaining the reason. Called
 * from every state-transition point in the LSP lifecycle
 * (`_attachLsp`, `_openLspSocket`, the `ws.onerror` handler). Safe to
 * call before the modal is built — the function no-ops until the
 * element exists.
 *
 * The four states were chosen to mirror the user-facing concept of
 * "is the language server helping me right now or not":
 *   - pending:    grey   "LSP: pending"  (loading / connecting)
 *   - ok:         green  "LSP: ok"       (initialize round-trip done)
 *   - unavailable:yellow "LSP: off"      (server binary not installed)
 *   - error:      red    "LSP: error"    (socket errored)
 */
function _renderLspPill() {
  const pill = _modal?.querySelector('#cw-lsp-pill');
  if (!pill) return;
  pill.classList.remove('cw-lsp-state-pending', 'cw-lsp-state-ok',
                        'cw-lsp-state-unavailable', 'cw-lsp-state-error');
  const lang = _lspLanguage ? _lspLanguage.toUpperCase() : '';
  switch (_lspState) {
    case 'ok':
      pill.classList.add('cw-lsp-state-ok');
      pill.textContent = `LSP: ok (${lang})`;
      pill.title = `Language server connected for ${lang}.`;
      break;
    case 'unavailable':
      pill.classList.add('cw-lsp-state-unavailable');
      pill.textContent = `LSP: off`;
      pill.title = _lspUnavailableReason
        || `Language server for ${lang || 'this file'} is not installed.`;
      break;
    case 'error':
      pill.classList.add('cw-lsp-state-error');
      pill.textContent = 'LSP: error';
      pill.title = _lspUnavailableReason || 'LSP socket error.';
      break;
    case 'pending':
    default:
      pill.classList.add('cw-lsp-state-pending');
      pill.textContent = 'LSP: pending';
      pill.title = 'Connecting to language server…';
      break;
  }
}

function _highlight() {
  const code = _modal?.querySelector('#cw-editor-code');
  if (!code || !window.hljs) return;
  code.className = '';
  code.removeAttribute('data-highlighted');
  try {
    if (_currentLang && _currentLang !== 'plaintext' && window.hljs.getLanguage(_currentLang)) {
      code.innerHTML = window.hljs.highlight(code.textContent, { language: _currentLang }).value;
    } else {
      // plaintext / undetected — render text without token coloring
      code.textContent = code.textContent;
    }
  } catch {
    code.textContent = code.textContent;
  }
}

async function _save() {
  if (!_currentPath) return;
  const saveBtn = _modal.querySelector('#cw-save-btn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';
  try {
    const content = _getEditorValue();
    const res = await api('POST', '/file', { path: relPath(_currentPath), content });
    _dirty = false;
    _updateSaveBtn();
    // Tell the LSP server the file changed on disk so it can clear stale
    // diagnostics and re-run the formatter. Without this, pylsp keeps
    // the in-memory text but doesn't know the file is now persisted —
    // it still works for diagnostics, but a `textDocument/formatting`
    // request would be unaware that the buffer is in sync with disk.
    _lspNotifyDidSave(content);
    // Show the diff if there is one
    if (res.diff && res.diff.text) {
      _showDiff(res.diff);
    } else {
      _closeDiff();
    }
  } catch (e) {
    alert('Save failed: ' + e.message);
  } finally {
    saveBtn.textContent = 'Save';
    _updateSaveBtn();
  }
}

/**
 * Send `textDocument/didSave` to the active LSP socket, if any. No-op
 * when the LSP isn't attached (file isn't Python/TS/Rust) or the
 * socket isn't open. The URI is read from a side-channel we attach to
 * the socket during `_openLspSocket` — see `_lspSocket.__fileUri`.
 */
function _lspNotifyDidSave(text) {
  if (!_lspSocket || _lspSocket.readyState !== WebSocket.OPEN) return;
  const uri = _lspSocket.__fileUri;
  if (!uri) return;
  try {
    _lspSocket.send(JSON.stringify({
      jsonrpc: '2.0', method: 'textDocument/didSave',
      params: { textDocument: { uri }, text: text || '' },
    }));
  } catch (e) {
    // Save notifications are best-effort. A send failure usually means
    // the socket just closed; the next `didChange` (item 1) will detect
    // that and stop sending. Don't surface this to the user.
    console.warn('[codeWorkspace] didSave notify failed:', e);
  }
}

function _updateSaveBtn() {
  const btn = _modal?.querySelector('#cw-save-btn');
  if (!btn) return;
  btn.disabled = !_currentPath || !_dirty;
}

// --- diff viewer ---

function _showDiff(diff) {
  const pane = _modal.querySelector('#cw-diff-pane');
  const header = _modal.querySelector('#cw-diff-header');
  const body = _modal.querySelector('#cw-diff-body');
  const title = _modal.querySelector('#cw-diff-title');
  if (!pane) return;
  title.textContent = diff.file || 'Diff';
  const rows = diff.text.split('\n').map(line => {
    let cls = 'diff-ctx', text = line;
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) { cls = 'diff-add'; text = line.slice(1); }
    else if (line.startsWith('-')) { cls = 'diff-del'; text = line.slice(1); }
    else if (line.startsWith(' ')) text = line.slice(1);
    return `<span class="${cls}">${esc(text) || '&nbsp;'}</span>`;
  }).join('');
  body.innerHTML = `<pre>${rows}</pre>`;
  pane.classList.add('cw-diff-open');
  header.style.display = 'flex';
}

function _closeDiff() {
  const pane = _modal?.querySelector('#cw-diff-pane');
  const header = _modal?.querySelector('#cw-diff-header');
  if (pane) pane.classList.remove('cw-diff-open');
  if (header) header.style.display = 'none';
}

// --- live refresh: listen for agent edits to the open file ---

function _wireLiveRefresh() {
  document.addEventListener('workspace:diff-applied', (e) => {
    if (!_currentPath || !_modal || _modal.classList.contains('hidden')) return;
    const detail = e.detail || {};
    // The diff event carries the file basename (diff.file) and optionally the
    // full path (diff.path). Match on whichever is available — don't clobber
    // the user's unsaved edits.
    if (_dirty) return;
    const changedFile = detail.file || '';
    const changedPath = detail.path || '';
    const currentBasename = _currentPath.split(/[\\/]/).pop();
    if ((changedPath && _currentPath === changedPath) ||
        (changedFile && currentBasename === changedFile)) {
      _openFile(_currentPath);
    }
  });
}

// --- open / init ---

async function openCodeWorkspace() {
  // Back-compat: `openCodeWorkspace(path)` (called from chat.js's edit-card
  // "Open in editor" button) deep-links to a file. The bare form
  // `openCodeWorkspace()` still works the same as before.
  return openCodeWorkspaceAt.apply(null, arguments);
}

async function openCodeWorkspaceAt(filePath) {
  _buildModal();
  if (!_modal) return;
  _modal.classList.remove('hidden');
  Modals.register('code-workspace-modal', {
    railBtnId: 'rail-code',
    sidebarBtnId: 'tool-code-btn',
    closeFn: () => _close(),
    restoreFn: () => {},
  });
  // Load the workspace root; show picker if not set.
  const hasRoot = await _loadRoot();
  if (hasRoot) {
    _showPanel();
    // If we were called with a file path, open it after the panel mounts.
    // _openFile handles the no-workspace / path-outside-workspace cases
    // (it surfaces a toast and bails).
    if (filePath) {
      try { await _openFile(filePath); } catch (_) { /* toast already shown */ }
    }
  } else {
    _showPickerOnly(false);
  }
}

function _showPickerOnly(fromPanel = false) {
  const layout = _modal.querySelector('#cw-layout');
  if (!layout) return;
  // We're about to replace #cw-layout with the picker, destroying the editor
  // DOM. Clear the open-file state so a stray `workspace:diff-applied` event
  // (from an agent edit) doesn't try to refresh a file into elements that no
  // longer exist — _wireLiveRefresh guards on _currentPath, so resetting it
  // here prevents a null-deref in _openFile.
  _currentPath = null;
  _dirty = false;
  // No usable workspace in picker mode → hide the Files/Git tabs + git view.
  _modal.querySelector('#cw-tabs')?.classList.add('hidden');
  _modal.querySelector('#cw-git-view')?.classList.add('hidden');
  layout.classList.remove('hidden');
  const currentPath = _root || '';
  layout.innerHTML = `
    <div class="cw-picker" id="cw-picker">
      <svg class="cw-picker-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h20"/><path d="M2 12c0-5 4-9 9-9h2c5 0 9 4 9 9s-4 9-9 9h-2c-5 0-9-4-9-9z"/><path d="M12 2v20"/></svg>
      <p>${fromPanel ? 'Change your workspace folder' : 'No workspace folder is set.'}<br>Pick a project folder to browse and edit its files.</p>
      <div class="cw-picker-input-row">
        <input type="text" id="cw-picker-input" value="${esc(currentPath)}" placeholder="D:\\projects\\my-project" autocomplete="off" spellcheck="false">
        <button class="cw-picker-btn" id="cw-picker-btn">${fromPanel ? 'Change' : 'Set Workspace'}</button>
      </div>
      <span class="cw-picker-browse" id="cw-picker-browse">Or browse for a folder…</span>
      <span class="cw-picker-error" id="cw-picker-error" style="display:none"></span>
    </div>`;
  _modal.querySelector('#cw-picker-btn').addEventListener('click', () => _pickerSet());
  const input = _modal.querySelector('#cw-picker-input');
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') _pickerSet(); });
  input.focus();
  input.select();
  // Browse link — native OS folder picker (desktop) or the in-app directory
  // browser as a fallback; the chosen folder is set as the workspace root.
  _modal.querySelector('#cw-picker-browse').addEventListener('click', () => _browseForFolder());
}

export function initCodeWorkspace() {
  // Wire the rail + sidebar buttons to open the modal.
  const railBtn = document.getElementById('rail-code');
  const sideBtn = document.getElementById('tool-code-btn');
  const open = (e) => { e.preventDefault(); openCodeWorkspace(); };
  railBtn?.addEventListener('click', open);
  sideBtn?.addEventListener('click', open);
  // Listen for agent file edits (dispatched by chat.js _attachDiffApprovalButtons).
  _wireLiveRefresh();
}

// Self-initialize — ES modules are deferred, so the DOM is ready.
initCodeWorkspace();

export { openCodeWorkspace, openCodeWorkspaceAt };

// ── Monaco editor + LSP bridge ─────────────────────────────────────────────
// Lazy-load Monaco from the vendored AMD bundle. The loader is a separate
// script tag because the AMD module system is the easiest way to consume
// Monaco's split modules without a bundler.

/**
 * Load Monaco from /static/lib/monaco/vs/loader.js. Resolves to `true` when
 * Monaco is ready (and `window.monaco` is populated), `false` on failure
 * (network error, CSP, vendored bundle missing). The result is cached in
 * `_monacoLoading` so concurrent file opens don't race.
 */
function _ensureMonaco() {
  if (_monacoLoading) return _monacoLoading;
  _monacoLoading = new Promise((resolve) => {
    if (window.monaco && window.monaco.editor) { resolve(true); return; }
    // Inject the AMD loader. Monaco's loader is global; it sets `require`
    // and (after our config call) resolves `vs/editor/editor.main`.
    const s = document.createElement('script');
    s.src = MONACO_LOADER_URL;
    s.async = true;
    s.onload = () => {
      try {
        // Configure the AMD loader so all `vs/...` imports resolve to the
        // vendored bundle. The `vs` prefix is Monaco's namespace.
        window.require.config({ paths: { vs: `${MONACO_BASE}/vs` } });
        // Monaco's editor.main pulls in the basic-languages we need via
        // sub-requires; we don't need to enumerate them here. If a more
        // exotic language is required, add it to the list.
        window.require(['vs/editor/editor.main'], () => {
          if (window.monaco && window.monaco.editor) {
            resolve(true);
          } else {
            console.warn('[codeWorkspace] Monaco loaded but window.monaco is empty');
            resolve(false);
          }
        }, (err) => {
          console.warn('[codeWorkspace] Monaco AMD load failed:', err);
          resolve(false);
        });
      } catch (e) {
        console.warn('[codeWorkspace] Monaco init threw:', e);
        resolve(false);
      }
    };
    s.onerror = () => {
      console.warn('[codeWorkspace] could not load Monaco AMD loader from', MONACO_LOADER_URL);
      resolve(false);
    };
    document.head.appendChild(s);
  });
  return _monacoLoading;
}

/**
 * Mount Monaco over the textarea for the given file. Idempotent: a second
 * call with a different file swaps the model; a call with the same file is
 * a no-op. Returns `true` when Monaco is live, `false` when the fallback
 * (textarea + highlight.js) is in use.
 */
async function _mountMonacoForFile(absPath, content, lang) {
  const ok = await _ensureMonaco();
  if (!ok) return false;
  if (!_modal) return false;
  const wrap = _modal.querySelector('#cw-editor-wrap');
  if (!wrap) return false;
  const ta = _modal.querySelector('#cw-editor-textarea');
  const code = _modal.querySelector('#cw-editor-code');
  const monacoLang = _monacoLanguageId(lang);
  if (!_monacoEditor) {
    // First mount — allocate the container + editor. The textarea stays in
    // the DOM (hidden) so the fallback path works on the next reload.
    const host = document.createElement('div');
    host.id = 'cw-monaco-host';
    host.style.cssText = 'position:absolute;inset:0;';
    wrap.appendChild(host);
    // Define a theme that reads the page's CSS variables so it matches the
    // DevSpace chrome. Registered once; on theme change we'd need to
    // re-register (not done in v1 — vs-dark is the safe default).
    try {
      const cs = getComputedStyle(document.documentElement);
      const bg = cs.getPropertyValue('--bg-elevated').trim()
        || cs.getPropertyValue('--bg-secondary').trim() || '#1e1e1e';
      const fg = cs.getPropertyValue('--text').trim() || '#d4d4d4';
      window.monaco.editor.defineTheme('devspace-dark', {
        base: 'vs-dark', inherit: true, rules: [],
        colors: {
          'editor.background': bg,
          'editor.foreground': fg,
          'editorLineNumber.foreground': '#666',
          'editorCursor.foreground': fg,
        },
      });
    } catch (_) { /* ignore — fall back to vs-dark */ }
    _monacoEditor = window.monaco.editor.create(host, {
      value: content || '',
      language: monacoLang,
      theme: 'devspace-dark',
      automaticLayout: true,
      fontSize: 13,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      wordWrap: 'off',
      lineNumbers: 'on',
      renderWhitespace: 'selection',
      tabSize: 4,
      insertSpaces: true,
      fontFamily: 'Menlo, Consolas, "Liberation Mono", monospace',
    });
    // Drive _dirty from Monaco. We also keep the (now-hidden) textarea's
    // `value` in sync so anything that still reads `ta.value` (e.g. a
    // bookmarklet) sees the latest content.
    _monacoEditor.onDidChangeModelContent(() => {
      _dirty = true;
      _updateSaveBtn();
      if (ta) ta.value = _monacoEditor.getValue();
    });
    // Install the cross-file navigation opener exactly once per Monaco
    // load. Subsequent file opens reuse it. The opener intercepts the
    // default Monaco Ctrl+Click / F12 handler; if the goto-def target
    // URI is already a mounted model we let Monaco handle it natively,
    // otherwise we navigate via the existing `openCodeWorkspaceAt`
    // entry point.
    if (!window._lspEditorOpenerInstalled) {
      try {
        window.monaco.editor.registerEditorOpener({
          openCodeEditor: async (_editor, payload) => {
            const targetUri = payload && payload.targetUri;
            if (!targetUri) return false;
            // Same-file: let Monaco handle it (no-op for us).
            const existing = window.monaco.editor.getModel(targetUri);
            if (existing && _monacoEditor &&
                _monacoModel && existing.uri.toString() ===
                _monacoModel.uri.toString()) {
              return true;
            }
            // Already-mounted different file: switch the editor's
            // model. This is the common cross-file case once the user
            // has visited both files in this session.
            if (existing) {
              _monacoEditor.setModel(existing);
              _monacoModel = existing;
              const sel = payload.targetSelectionRange
                          || payload.selection
                          || (payload.range && { startLineNumber: payload.range.startLineNumber,
                                                 startColumn: payload.range.startColumn,
                                                 endLineNumber: payload.range.endLineNumber,
                                                 endColumn: payload.range.endColumn });
              if (sel) {
                _monacoEditor.setSelection(sel);
                _monacoEditor.revealRangeInCenterIfOutsideViewport(sel);
              }
              return true;
            }
            // Not yet mounted: open via the workspace entry point, then
            // hand the cursor position to `_openFile` via a one-shot
            // pending-reveal so it scrolls the new model into view.
            const targetPath = _uriToFilePath(targetUri.toString());
            if (!targetPath) return false;
            _pendingReveal = payload.targetSelectionRange
                            || payload.selection
                            || (payload.range && {
                              startLineNumber: payload.range.startLineNumber,
                              startColumn: payload.range.startColumn,
                              endLineNumber: payload.range.endLineNumber,
                              endColumn: payload.range.endColumn });
            try {
              await openCodeWorkspaceAt(targetPath);
            } finally {
              _pendingReveal = null;
            }
            return true;
          },
        });
        window._lspEditorOpenerInstalled = true;
      } catch (e) {
        console.warn('[codeWorkspace] registerEditorOpener failed:', e);
      }
    }
  } else {
    // Already mounted — just swap the model.
    _monacoModel = window.monaco.editor.createModel(content || '', monacoLang);
    _monacoEditor.setModel(_monacoModel);
  }
  // Hide the textarea+highlight overlay; show Monaco.
  if (ta) ta.style.display = 'none';
  if (code) code.style.display = 'none';
  const host = _modal.querySelector('#cw-monaco-host');
  if (host) host.style.display = '';
  // Capture the model so we can dispose it on file change.
  _monacoModel = _monacoEditor.getModel();
  // Bind Ctrl+Shift+F to Monaco's built-in "format document" action.
  // That action in turn calls the LSP DocumentFormattingEditProvider
  // registered in `_registerLspProviders` — if no provider is
  // registered for the current language, the action silently no-ops
  // (Monaco's contract, not ours).
  try {
    _monacoEditor.addCommand(
      window.monaco.KeyMod.CtrlShift | window.monaco.KeyCode.KeyF,
      'editor.action.formatDocument',
    );
  } catch (_) { /* KeyMod/KeyCode missing in some Monaco builds; rare */ }
  return true;
}

/**
 * Map our internal `lang` (a highlight.js id) to a Monaco language id. The
 * sets overlap heavily but aren't identical. Returns 'plaintext' for
 * anything Monaco doesn't know.
 */
function _monacoLanguageId(hljsLang) {
  const m = {
    python: 'python', py: 'python', pyw: 'python', pyi: 'python',
    javascript: 'javascript', js: 'javascript', jsx: 'javascript',
    typescript: 'typescript', ts: 'typescript', tsx: 'typescript',
    json: 'json', jsonc: 'json',
    xml: 'xml', html: 'html', htm: 'html', xhtml: 'html', vue: 'html', svg: 'xml',
    css: 'css', scss: 'scss', sass: 'scss', less: 'less',
    markdown: 'markdown', md: 'markdown', mdx: 'markdown',
    bash: 'shell', sh: 'shell', zsh: 'shell', ksh: 'shell',
    rust: 'rust', rs: 'rust',
    go: 'go', golang: 'go',
    sql: 'sql',
    yaml: 'yaml', yml: 'yaml',
    ini: 'ini', toml: 'ini', cfg: 'ini', conf: 'ini', env: 'ini', properties: 'ini',
    java: 'java', kt: 'kotlin', kts: 'kotlin', scala: 'scala', groovy: 'groovy',
    c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp', hxx: 'cpp', hh: 'cpp',
    csharp: 'csharp', cs: 'csharp',
    ruby: 'ruby', rb: 'ruby',
    php: 'php',
    perl: 'perl', pl: 'perl',
    lua: 'lua',
    r: 'r',
    dart: 'dart',
    swift: 'swift',
    powershell: 'powershell', ps1: 'powershell', psm1: 'powershell',
    dockerfile: 'dockerfile',
    makefile: 'makefile', mk: 'makefile',
    diff: 'plaintext', patch: 'plaintext',
    plaintext: 'plaintext', text: 'plaintext', log: 'plaintext',
  };
  return m[hljsLang] || 'plaintext';
}

/**
 * Return the current editor buffer, regardless of whether Monaco or the
 * textarea is the live editor. Used by Save so the rest of the module
 * doesn't need to know which is mounted.
 */
function _getEditorValue() {
  if (_monacoEditor) return _monacoEditor.getValue();
  const ta = _modal?.querySelector('#cw-editor-textarea');
  return ta ? ta.value : '';
}

/**
 * Convert an absolute filesystem path to a `file://` URI that the LSP
 * server can decode back to the same path. Mirrors
 * ``backend/src/lsp_bridge.py:path_to_uri`` exactly:
 *   - Forward slashes (Windows: ``C:\foo`` → ``C:/foo``)
 *   - Drive letter gets a leading ``/`` (``file:///C:/foo``)
 *   - Each path segment is URL-encoded (spaces, parentheses, etc.)
 *
 * The function is intentionally a no-op on inputs that already start
 * with ``file://`` so the helpers can be called on either shape.
 */
function _pathToFileUri(absPath) {
  if (!absPath) return null;
  if (absPath.startsWith('file://')) return absPath;
  let fwd = absPath.replace(/\\/g, '/');
  // On Windows, paths look like `C:/foo`; the URI needs `/C:/foo`.
  if (!fwd.startsWith('/') && /^[A-Za-z]:/.test(fwd)) {
    fwd = '/' + fwd;
  }
  // Encode each segment, leaving '/' as a path separator.
  return 'file://' + fwd.split('/').map(encodeURIComponent).join('/');
}

/**
 * Inverse of ``_pathToFileUri``. Mirrors
 * ``backend/src/lsp_bridge.py:uri_to_path`` — strips the ``file://``
 * prefix and the leading slash on Windows so the result is usable
 * with ``os.path`` / the editor's `_openFile` (which expects the
 * workspace-root-relative path).
 */
function _uriToFilePath(uri) {
  if (!uri) return null;
  if (!uri.startsWith('file://')) return null;
  let p = decodeURIComponent(uri.slice('file://'.length));
  // On Windows, the URI looks like `/C:/...`; the editor and the file
  // system both want `C:/...`. We sniff the user agent rather than
  // reaching into a platform module so this file stays import-clean.
  const isWin = /Win/.test((typeof navigator !== 'undefined' &&
                            navigator.platform) || '');
  if (isWin && p.startsWith('/') && p.length >= 3 && p[2] === ':') {
    p = p.slice(1);
  }
  return p;
}

// ── LSP wiring ──────────────────────────────────────────────────────────
//
// The WebSocket contract is documented in backend/routes/lsp_routes.py.
// Each frame is a full JSON-RPC message — no extra envelope. We translate
// Monaco's model events to LSP `textDocument/*` notifications and pipe
// server responses (publishDiagnostics, completion, hover, etc.) back
// through Monaco's language-service hooks.
//
// v1 surface:
//   * diagnostics  → `monaco.editor.setModelMarkers`
//   * completion   → `monaco.languages.registerCompletionItemProvider`
//   * hover        → `monaco.languages.registerHoverProvider`
//   * go-to-def    → `monaco.languages.registerDefinitionProvider`
// Other features (rename, references, formatting) plug in here later.

let _lspProviders = [];  // IDisposable[] — disposed when LSP detaches

function _lspLanguageFor(hljsLang) {
  // Map our highlight.js language id to the matching LSP language id.
  // Both TS and JS use the same server (`typescript-language-server`),
  // but we return distinct ids so the LSP status pill can show "TS" vs
  // "JS" to the user. The `monacoLanguageId` for either is decided by
  // the Monaco syntax-highlight registration; the bridge doesn't care.
  if (hljsLang === 'python' || hljsLang === 'py' ||
      hljsLang === 'pyw' || hljsLang === 'pyi') {
    return 'python';
  }
  if (hljsLang === 'typescript' || hljsLang === 'ts' || hljsLang === 'tsx') {
    return 'typescript';
  }
  if (hljsLang === 'javascript' || hljsLang === 'js' || hljsLang === 'jsx') {
    return 'javascript';
  }
  if (hljsLang === 'rust' || hljsLang === 'rs') {
    return 'rust';
  }
  return null;
}

async function _attachLsp(hljsLang) {
  // Detach any existing socket — opening a new file closes the old session.
  _detachLsp();
  if (!_monacoEditor) return;            // Monaco not loaded; nothing to wire
  const lang = _lspLanguageFor(hljsLang);
  if (!lang) { _lspState = 'unavailable'; _lspLanguage = null; _renderLspPill(); return; }
  if (!_root) { _lspState = 'pending'; _lspLanguage = null; _renderLspPill(); return; }
  // Honour the Enable-LSP toggle (item 3). When off, short-circuit
  // with state=unavailable so the pill turns yellow and the bridge
  // never opens a WebSocket — saves a roundtrip and a pylsp startup
  // for users who want the editor but don't need the language server.
  const lspEnabled = (() => {
    try { return localStorage.getItem('code.lsp_enabled') !== 'false'; }
    catch (_) { return true; }
  })();
  if (!lspEnabled) {
    _lspLanguage = lang;
    _lspState = 'unavailable';
    _lspUnavailableReason = 'LSP disabled in toolbar (Enable LSP toggle).';
    _renderLspPill();
    return;
  }
  // Check availability first; the server returns 200 with {python, typescript, rust: bool}
  // even when the corresponding server is missing, so the frontend can
  // render a clean "LSP unavailable" state instead of hanging on a WS
  // upgrade. JavaScript files share the TS server (`typescript-language-server`
  // handles both), so the availability key for a JS file is the same as
  // for a TS file — we look up under `typescript` in that case.
  let available = false;
  try {
    const r = await fetch(`${API}/../lsp/availability`, { credentials: 'same-origin' });
    if (r.ok) {
      const m = await r.json();
      const lookupKey = (lang === 'javascript') ? 'typescript' : lang;
      available = !!m[lookupKey];
    }
  } catch (_) { /* server not reachable; fall through with available=false */ }
  if (!available) {
    _lspState = 'unavailable';
    _lspLanguage = lang;
    _lspUnavailableReason = `${lang} language server is not installed on the server.`;
    _renderLspPill();
    return;
  }
  _lspLanguage = lang;
  _lspState = 'pending';
  _renderLspPill();
  _openLspSocket(lang);
}

function _detachLsp() {
  if (_lspSocket) {
    try { _lspSocket.close(); } catch (_) {}
    _lspSocket = null;
  }
  if (_monacoModel && _lspProviders.length) {
    for (const d of _lspProviders) {
      try { d.dispose(); } catch (_) {}
    }
    _lspProviders = [];
  }
  _lspState = 'pending';
  _lspLanguage = null;
  _lspUnavailableReason = '';
  _renderLspPill();
}

function _openLspSocket(lang) {
  const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // path= is the workspace root; the server uses it to scope the session
  // and the URL confinement check. We send the absolute root.
  const url = `${wsProto}//${window.location.host}/api/lsp/${lang}?path=${encodeURIComponent(_root)}`;
  const ws = new WebSocket(url);
  _lspSocket = ws;
  let nextId = 1;
  const pending = new Map();   // id → {resolve, reject}
  let serverCaps = null;
  let initialized = false;
  let fileVersion = 0;
  let fileUri = null;
  let openFileLang = null;

  function send(msg) {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(msg));
  }
  function request(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      send({ jsonrpc: '2.0', id, method, params });
    });
  }
  function notify(method, params) {
    send({ jsonrpc: '2.0', method, params });
  }

  // Build the file:// URI for the open file so the server can vet it.
  if (_currentPath) {
    // Use the shared path↔uri helper so the URI shape here matches the
    // shape the server expects (`uri_to_path` in lsp_bridge.py) and
    // the shape we generate on cross-file goto-def responses.
    fileUri = _pathToFileUri(_currentPath);
    openFileLang = lang;
  }
  // Expose the active file URI on the socket as a side-channel so
  // out-of-closure callers (e.g. _lspNotifyDidSave from _save) can
  // send a `textDocument/didSave` without re-deriving the URI.
  ws.__fileUri = fileUri;

  ws.addEventListener('open', async () => {
    try {
      // LSP handshake.
      const initResult = await request('initialize', {
        processId: null,
        clientInfo: { name: 'devspace-code-workspace', version: '1.0' },
        rootUri: null,            // pylsp uses rootPath; we send that instead
        rootPath: _root || null,
        capabilities: {
          textDocument: {
            synchronization: { didSave: true, willSave: false, dynamicRegistration: false },
            completion: { completionItem: { snippetSupport: false } },
            hover: { contentFormat: ['markdown', 'plaintext'] },
            definition: { linkSupport: true },
          },
        },
        initializationOptions: {},
        workspaceFolders: null,
      });
      serverCaps = (initResult && initResult.capabilities) || {};
      initialized = true;
      notify('initialized', {});
      // Open the current file (if any) so the server starts analysing.
      if (fileUri && _monacoModel) {
        fileVersion = 1;
        notify('textDocument/didOpen', {
          textDocument: {
            uri: fileUri, languageId: openFileLang, version: fileVersion,
            text: _monacoModel.getValue(),
          },
        });
        // Forward every keystroke to the server as `didChange` so its
        // diagnostic and completion state stay in sync with the editor.
        // Without this, pylsp only ever sees the file at open time and
        // its red-squiggle map drifts the moment the user starts typing.
        // Gated on the file URI being set: changing files in mid-session
        // recreates this whole closure, so a stale listener is impossible.
        _monacoModel.onDidChangeContent((e) => {
          if (ws.readyState !== WebSocket.OPEN) return;
          if (!fileUri) return;
          fileVersion += 1;
          // Full-text replacement on every change. pylsp prefers
          // incremental `contentChanges[]` but for typing the simpler
          // whole-text payload is correct and trivially small (<20K chars
          // after the file-read cap). Build the LSP range with the
          // required 0-based line/character positions.
          const lineCount = _monacoModel.getLineCount();
          const lastLineLen = _monacoModel.getLineMaxColumn(lineCount) - 1;
          const fullRange = {
            start: { line: 0, character: 0 },
            end: { line: lineCount - 1, character: lastLineLen },
          };
          notify('textDocument/didChange', {
            textDocument: { uri: fileUri, version: fileVersion },
            contentChanges: [{ range: fullRange, rangeLength: 0,
                               text: _monacoModel.getValue() }],
          });
        });
      }
      _lspState = 'ok';
      _lspUnavailableReason = '';
      _renderLspPill();
      _registerLspProviders();
    } catch (e) {
      console.warn('[codeWorkspace] LSP initialize failed:', e);
    }
  });

  ws.addEventListener('message', (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    // Response to a request?
    if (typeof msg.id === 'number' && (msg.result !== undefined || msg.error !== undefined)) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message || 'lsp error'));
        else p.resolve(msg.result);
      }
      return;
    }
    // Notification from the server.
    const method = msg.method;
    if (method === 'textDocument/publishDiagnostics' && _monacoModel) {
      const diags = (msg.params && msg.params.diagnostics) || [];
      const markers = diags.map((d) => {
        const start = d.range && d.range.start || { line: 0, character: 0 };
        const end = d.range && d.range.end || { line: 0, character: 0 };
        return {
          startLineNumber: (start.line || 0) + 1,
          startColumn: (start.character || 0) + 1,
          endLineNumber: (end.line || 0) + 1,
          endColumn: (end.character || 0) + 1,
          message: d.message || '',
          severity: ({
            1: 8,  // Error
            2: 4,  // Warning
            3: 2,  // Information
            4: 1,  // Hint
          })[d.severity] || 2,
          source: d.source || 'lsp',
        };
      });
      window.monaco.editor.setModelMarkers(_monacoModel, 'lsp', markers);
    } else if (method === 'window/logMessage' || method === 'window/showMessage') {
      const text = (msg.params && msg.params.message) || '';
      if (text) console.info('[lsp]', text);
    }
  });

  ws.addEventListener('close', () => {
    if (_lspSocket === ws) _lspSocket = null;
  });
  ws.addEventListener('error', (e) => {
    _lspState = 'error';
    _lspUnavailableReason = 'LSP socket error';
    _renderLspPill();
  });
}

/**
 * Register Monaco language-service providers that talk to the open LSP
 * socket. Each provider is an LSP client: it sends a request, awaits the
 * response, and translates the result into Monaco's shape. Disposables
 * are tracked so we can clean up on file change.
 */
function _registerLspProviders() {
  if (!window.monaco || !_monacoModel) return;
  const monaco = window.monaco;
  const self = this;

  // ── Completion ──
  _lspProviders.push(monaco.languages.registerCompletionItemProvider(
    _monacoLanguageId(_currentLang), {
      triggerCharacters: ['.', ':', '<', '"', "'"],
      provideCompletionItems: async (model, position) => {
        if (!_lspSocket || _lspSocket.readyState !== WebSocket.OPEN) return { suggestions: [] };
        // We don't have a direct ref to the active socket from here; use
        // a simple roundtrip via the same _lspSocket variable.
        // LSP wants 0-based line/char; Monaco's position is 1-based.
        const params = {
          textDocument: { uri: _pathToFileUri(_currentPath) },
          position: { line: position.lineNumber - 1, character: position.column - 1 },
          context: { triggerKind: 1 },
        };
        const id = _lspNextId();
        const p = new Promise((resolve, reject) => {
          _lspPending.set(id, { resolve, reject });
          _lspSocket.send(JSON.stringify({
            jsonrpc: '2.0', id, method: 'textDocument/completion', params,
          }));
        });
        let result;
        try { result = await p; } catch (_) { return { suggestions: [] }; }
        const items = Array.isArray(result) ? result : (result && result.items) || [];
        return {
          suggestions: items.map((it) => ({
            label: it.label,
            kind: it.kind || 0,
            detail: it.detail,
            documentation: it.documentation,
            insertText: it.insertText || it.label,
            range: {
              startLineNumber: position.lineNumber,
              startColumn: position.column,
              endLineNumber: position.lineNumber,
              endColumn: position.column,
            },
          })),
        };
      },
    }));

  // ── Hover ──
  _lspProviders.push(monaco.languages.registerHoverProvider(
    _monacoLanguageId(_currentLang), {
      provideHover: async (model, position) => {
        if (!_lspSocket || _lspSocket.readyState !== WebSocket.OPEN) return null;
        const params = {
          textDocument: { uri: _pathToFileUri(_currentPath) },
          position: { line: position.lineNumber - 1, character: position.column - 1 },
        };
        const id = _lspNextId();
        const p = new Promise((resolve, reject) => {
          _lspPending.set(id, { resolve, reject });
          _lspSocket.send(JSON.stringify({
            jsonrpc: '2.0', id, method: 'textDocument/hover', params,
          }));
        });
        let result;
        try { result = await p; } catch (_) { return null; }
        if (!result || !result.contents) return null;
        const contents = Array.isArray(result.contents) ? result.contents : [result.contents];
        const md = contents.map((c) => typeof c === 'string' ? c : (c.value || '')).join('\n\n');
        return { contents: [{ value: md, isTrusted: true }] };
      },
    }));

  // ── Go-to-definition ──
  _lspProviders.push(monaco.languages.registerDefinitionProvider(
    _monacoLanguageId(_currentLang), {
      provideDefinition: async (model, position) => {
        if (!_lspSocket || _lspSocket.readyState !== WebSocket.OPEN) return [];
        const params = {
          textDocument: { uri: _pathToFileUri(_currentPath) },
          position: { line: position.lineNumber - 1, character: position.column - 1 },
        };
        const id = _lspNextId();
        const p = new Promise((resolve, reject) => {
          _lspPending.set(id, { resolve, reject });
          _lspSocket.send(JSON.stringify({
            jsonrpc: '2.0', id, method: 'textDocument/definition', params,
          }));
        });
        let result;
        try { result = await p; } catch (_) { return []; }
        const locs = Array.isArray(result) ? result : (result ? [result] : []);
        return locs.filter(Boolean).map((loc) => {
          // The LSP server returns a Location (uri+range) or Location[].
          // pylsp returns Location; some servers return LocationLink
          // (uri+range+targetSelectionRange). Handle both shapes.
          //
          // Cross-file: emit the URI as a monaco.Uri so the registered
          // editor opener (installed in _mountMonacoForFile) navigates
          // to the target. Same-file: emit the open model's URI so
          // Monaco uses the already-mounted model and skips the
          // open-code-editor path.
          const r = loc.range || (loc.targetSelectionRange)
                    || { start: { line: 0, character: 0 } };
          const uri = loc.uri || loc.targetUri;
          if (!uri) return null;
          const targetModel = window.monaco.editor.getModel(
            window.monaco.Uri.parse(uri));
          return {
            uri: targetModel ? targetModel.uri
                              : window.monaco.Uri.parse(uri),
            range: {
              startLineNumber: (r.start.line || 0) + 1,
              startColumn: (r.start.character || 0) + 1,
              endLineNumber: (r.end.line || 0) + 1,
              endColumn: (r.end.character || 0) + 1,
            },
          };
        }).filter(Boolean);
      },
    }));

  // ── Document formatting ──
  // pylsp ships with the `yapf` plugin (pulled in by the `[all]` extra)
  // which provides a real formatter for Python. typescript-language-server
  // and rust-analyzer both also support `textDocument/formatting`. The
  // server may not advertise `documentFormattingProvider` in its
  // capabilities — in that case the call still goes out, the server
  // returns an empty list, and the user sees "no changes" silently.
  _lspProviders.push(monaco.languages.registerDocumentFormattingEditProvider(
    _monacoLanguageId(_currentLang), {
      provideDocumentFormattingEdits: async (model) => {
        if (!_lspSocket || _lspSocket.readyState !== WebSocket.OPEN) return [];
        const params = {
          textDocument: { uri: _pathToFileUri(_currentPath) },
          // Sensible defaults; pylsp/yapf ignores unknown options. A
          // future iteration can surface tab-size / quote-style controls
          // in the editor toolbar and pass them here.
          options: { tabSize: 4, insertSpaces: true },
        };
        const id = _lspNextId();
        const p = new Promise((resolve, reject) => {
          _lspPending.set(id, { resolve, reject });
          _lspSocket.send(JSON.stringify({
            jsonrpc: '2.0', id, method: 'textDocument/formatting', params,
          }));
        });
        let result;
        try { result = await p; } catch (_) { return []; }
        const edits = Array.isArray(result) ? result : [];
        return edits.map((e) => ({
          range: new monaco.Range(
            (e.range.start.line || 0) + 1,
            (e.range.start.character || 0) + 1,
            (e.range.end.line || 0) + 1,
            (e.range.end.character || 0) + 1,
          ),
          text: e.newText || '',
        }));
      },
    }));
}

// ── Per-socket request bookkeeping (kept module-level because the
// providers close over them). Each WebSocket creates its own map but we
// share a single id counter and pending map for simplicity; the current
// socket is the only one whose `pending` is consulted (the previous one
// is closed and discarded). On file change we tear down + recreate, so
// stale `pending` entries are simply never resolved.
let _lspNextIdNum = 1;
function _lspNextId() { return _lspNextIdNum++; }
let _lspPending = new Map();
