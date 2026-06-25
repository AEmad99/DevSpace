/* Source picker widget for Deep Research.

Renders a multi-selectable list of research sources above the query
textarea. Each source has a config form generated from the server's
config_schema. Returns the chosen sources as a `sources` array
compatible with the `/api/research/start` body shape:

  sources: [{ type: "folder", config: { path: "..." } }, ...]

When nothing is selected, the picker returns `null` so the server keeps
its default behavior (InternetSource only) — backward-compatible with
the pre-M4 single-source flow.

Source types (all always shown — RESEARCH_SOURCES_ENABLED defaults true):
  - internet          (default; no config)
  - folder            (Local Folder; path + filters)
  - codebase          (Local Codebase; workspace selector + path)
  - kb                (Knowledge Base; picks a saved KB)
  - library           (Library (Research Reports); multi-select reports)
  - chats             (Previous Chats; always all sessions)

Usage from the research panel (panel.js):

  import { renderSourcePicker, readPickerSelection } from './source_picker.js';
  renderSourcePicker(paneEl, { defaultSources: ['internet'] });
  // ...later, when launching:
  const sources = readPickerSelection(paneEl);
  settings.sources = sources;   // null = default; [] = default too; non-empty = explicit
*/

const _ROOT_CLASS = 'research-source-picker';
const _LIST_CLASS = 'research-source-picker-list';

// State: one entry per picker row in the DOM. Keyed by an internal id
// so additions/removals don't fight the DOM nodes.
const _pickers = new WeakMap();

/**
 * Render the source picker into `rootEl`.
 * @param {HTMLElement} rootEl  container to inject into
 * @param {Object} opts
 *   - defaultSources: ['internet', 'folder:abc', 'kb:xyz']
 *     Pre-populates the picker rows.
 *   - maxSources:     cap on the number of rows (default 4)
 */
export async function renderSourcePicker(rootEl, opts = {}) {
  if (!rootEl) return;
  const maxSources = opts.maxSources || 4;
  const initial = opts.defaultSources || ['internet'];

  rootEl.innerHTML = '';
  rootEl.classList.add(_ROOT_CLASS);

  const header = document.createElement('div');
  header.className = 'research-source-picker-header';
  header.innerHTML = `
    <span class="research-source-picker-label">Sources</span>
    <span class="research-source-picker-hint">Pick where research pulls evidence from. Leave as Internet for the default.</span>
  `;
  rootEl.appendChild(header);

  const list = document.createElement('div');
  list.className = _LIST_CLASS;
  rootEl.appendChild(list);

  const addRow = document.createElement('button');
  addRow.type = 'button';
  addRow.className = 'research-source-picker-add';
  addRow.textContent = '+ Add source';
  addRow.addEventListener('click', () => addPickerRow(list, maxSources));
  rootEl.appendChild(addRow);

  const state = { list, maxSources };
  _pickers.set(rootEl, state);

  // Fetch sources + KBs + research library in parallel — each powers a
  // different option in the dropdown.
  const [srcResp, kbResp, libResp] = await Promise.all([
    fetch('/api/research/sources', { credentials: 'same-origin' })
      .then(r => r.json()).catch(() => ({ sources: [], feature_enabled: true })),
    fetch('/api/knowledge_bases', { credentials: 'same-origin' })
      .then(r => r.json()).catch(() => ({ knowledge_bases: [] })),
    fetch('/api/research/library?limit=200', { credentials: 'same-origin' })
      .then(r => r.json()).catch(() => ({ research: [] })),
  ]);

  state.sources = srcResp.sources || [];
  state.kbs = (kbResp.knowledge_bases || []);
  // The /api/research/library endpoint returns its rows under `research`
  // (see research_routes.py). Accept `items` too for forward-compat.
  state.libraryItems = (libResp.research || libResp.items || []);
  state.featureEnabled = srcResp.feature_enabled !== false;  // default true

  // Pre-populate one row per default.
  for (const s of initial) addPickerRow(list, maxSources, s);
  // If nothing was added (e.g. empty defaults), add one Internet row.
  if (!list.children.length) addPickerRow(list, maxSources, 'internet');
}

function addPickerRow(list, maxSources, preset) {
  if (list.children.length >= maxSources) return;
  const row = document.createElement('div');
  row.className = 'research-source-picker-row';

  // Stash the picker state on the list so _buildTypeOptions can reach
  // it without threading it through every helper.
  const state = list._ownerState || (list._ownerState = _pickers.get(list.closest('.' + _ROOT_CLASS)));

  // Type selector
  const typeSel = document.createElement('select');
  typeSel.className = 'research-source-picker-type';
  typeSel.innerHTML = _buildTypeOptions(state, preset);
  if (preset && preset.startsWith('kb:')) {
    typeSel.value = 'kb';
    typeSel.dataset.kbId = preset.slice(3);
  } else if (preset && preset.startsWith('library:')) {
    typeSel.value = 'library';
    // report_ids after the colon, comma-separated
    const ids = preset.slice('library:'.length);
    if (ids) row.dataset.pendingReportIds = ids;
  }
  row.appendChild(typeSel);

  // Config container
  const cfg = document.createElement('div');
  cfg.className = 'research-source-picker-config';
  row.appendChild(cfg);

  // Remove button
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'research-source-picker-remove';
  remove.textContent = '×';
  remove.title = 'Remove this source';
  remove.addEventListener('click', () => {
    row.remove();
    _syncAddButtonVisibility(list);
  });
  row.appendChild(remove);

  list.appendChild(row);
  _syncAddButtonVisibility(list);

  // Render the initial config form for this row.
  const renderConfig = () => {
    cfg.innerHTML = '';
    cfg.className = 'research-source-picker-config';
    const type = typeSel.value;
    if (type === 'kb') {
      _renderKBConfig(cfg, state);
    } else if (type === 'library') {
      _renderLibraryConfig(cfg, state, row.dataset.pendingReportIds || null);
      delete row.dataset.pendingReportIds;
    } else if (type === 'chats') {
      _renderChatsConfig(cfg, state);
    } else if (type === 'codebase' || type === 'folder') {
      _renderCodebaseConfig(cfg, state);
    } else {
      _renderSourceConfig(cfg, state, type);
    }
  };
  typeSel.addEventListener('change', renderConfig);
  renderConfig();
}

function _buildTypeOptions(state, preset) {
  // Show all registered source types the user is allowed to pick from.
  // (RESEARCH_SOURCES_ENABLED is unconditionally true; the server still
  // returns the full list.)
  const all = (state?.sources || []).filter(s =>
    s.type === 'internet' || state?.featureEnabled);
  const options = [];
  // The hardcoded order: internet first (default), then user-content sources
  // (chats, library, codebase, folder, kb). The order matters because the
  // dropdown is the user's mental model of "what can I search?".
  const order = ['internet', 'chats', 'library', 'codebase', 'folder', 'kb'];
  const byType = new Map(all.map(s => [s.type, s]));
  for (const t of order) {
    if (t === 'internet') {
      options.push('<option value="internet">Internet (default)</option>');
      continue;
    }
    if (t === 'chats') {
      // chats is a Source registered in src/research_sources/previous_chats.py
      options.push('<option value="chats">Previous Chats</option>');
      continue;
    }
    if (t === 'library') {
      options.push('<option value="library">Library (Research Reports)</option>');
      continue;
    }
    if (t === 'codebase') {
      const def = byType.get('codebase');
      if (def) options.push(`<option value="codebase">${_escapeHtml(def.name || 'Local Codebase')}</option>`);
      continue;
    }
    const def = byType.get(t);
    if (def) options.push(`<option value="${_escapeAttr(t)}">${_escapeHtml(def.name || t)}</option>`);
  }
  return options.join('');
}

function _renderSourceConfig(cfg, state, type) {
  const def = (state?.sources || []).find(s => s.type === type);
  if (!def) return;
  if (type === 'internet') {
    const note = document.createElement('span');
    note.className = 'note research-source-picker-summary';
    note.textContent = 'Uses the search engine, format, and model settings above.';
    cfg.appendChild(note);
    return;
  }
  const schema = def.config_schema || {};
  for (const [key, spec] of Object.entries(schema)) {
    if (key === 'collection_name') continue;   // auto-derived; no UI input
    if (key === 'owner') continue;             // injected server-side
    if (spec.default === undefined) continue;
    cfg.appendChild(_buildField(key, spec));
  }
  if (!cfg.children.length) {
    const note = document.createElement('span');
    note.className = 'note';
    note.textContent = 'No configuration needed.';
    cfg.appendChild(note);
  }
}

function _renderKBConfig(cfg, state) {
  // KB selection is already in the type <select>. The config area shows
  // a read-only summary of the chosen KB's folders so the user knows
  // what they're researching over.
  cfg.classList.add('research-source-picker-config-workspace');
  const row = cfg.closest('.research-source-picker-row');
  const sel = row?.querySelector('.research-source-picker-type');
  const kbId = sel?.dataset?.kbId || sel?.selectedOptions?.[0]?.dataset?.kbId;
  const kb = (state?.kbs || []).find(k => k.id === kbId);
  const summary = document.createElement('div');
  summary.className = 'note';
  if (!kb) {
    summary.textContent = 'Select a knowledge base from the dropdown.';
  } else {
    summary.textContent = `${kb.name} — ${(kb.folders || []).length} folder(s)`;
  }
  cfg.appendChild(summary);

  // Re-render when the KB changes.
  sel?.addEventListener('change', () => {
    sel.dataset.kbId = sel.selectedOptions[0]?.dataset?.kbId || '';
    _renderKBConfig(cfg, state);
  });
}

function _renderLibraryConfig(cfg, state, preselectedIds) {
  cfg.classList.add('research-source-picker-config-multiselect');
  const items = state?.libraryItems || [];
  if (!items.length) {
    const note = document.createElement('span');
    note.className = 'note';
    note.textContent = 'No research reports in your Library yet.';
    cfg.appendChild(note);
    return;
  }
  // `preselectedIds` is a comma-separated list captured when the preset
  // was something like 'library:abc,def' (used by edit/restart flows).
  const preselected = new Set(
    (preselectedIds || '').split(',').map(s => s.trim()).filter(Boolean)
  );
  // Default: select the first 5 most-recent reports so the picker is
  // immediately useful out of the box. The user can uncheck anything.
  const defaultSelected = preselected.size
    ? preselected
    : new Set(items.slice(0, 5).map(it => it.id));

  const wrap = document.createElement('div');
  wrap.className = 'ms';
  for (const it of items) {
    const lbl = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = it.id;
    cb.checked = defaultSelected.has(it.id);
    cb.dataset.libraryId = it.id;
    const q = document.createElement('span');
    q.className = 'q';
    q.textContent = it.query || '(untitled)';
    q.title = it.query || it.id;
    const m = document.createElement('span');
    m.className = 'm';
    m.textContent = `${it.source_count || 0} src`;
    lbl.appendChild(cb);
    lbl.appendChild(q);
    lbl.appendChild(m);
    wrap.appendChild(lbl);
  }
  // Hint at the top of the checklist so the user knows unchecked = no findings.
  const note = document.createElement('span');
  note.className = 'note';
  note.style.cssText = 'align-self:flex-start;font-size:10px;opacity:0.55;';
  note.textContent = 'Uncheck everything to skip this source.';
  cfg.appendChild(note);
  cfg.appendChild(wrap);
}

function _renderChatsConfig(cfg, state) {
  // No config UI — the source always searches all of the user's chat
  // sessions. Show a small note so the user knows what they're getting.
  const items = (state?.libraryItems || []);   // unused; just to keep state referenced
  void items;
  const note = document.createElement('span');
  note.className = 'note';
  note.textContent = 'Searches all of your chat sessions.';
  cfg.appendChild(note);
}

function _renderCodebaseConfig(cfg, state) {
  // The codebase/folder sources take a `path` config. Rather than
  // making the user type one, render a workspace dropdown that lists
  // (a) the current chat-input workspace, (b) a "browse" sentinel that
  // opens the existing workspace browser, and (c) a "custom path"
  // sentinel that reveals a text input. The Browse button is always
  // present so the user can pick a folder with the familiar modal.
  cfg.classList.add('research-source-picker-config-workspace');
  const row = cfg.closest('.research-source-picker-row');
  const sel = row?.querySelector('.research-source-picker-type');

  const wrap = document.createElement('div');
  wrap.className = 'research-source-picker-workspace';

  const wsSel = document.createElement('select');
  wsSel.className = 'research-source-picker-workspace-select';
  // Pull the current workspace from localStorage (same key the chat
  // input pill uses). When the user hasn't set one, the dropdown falls
  // straight to "browse" so they can pick.
  const current = _readCurrentWorkspace();
  if (current) {
    const opt = document.createElement('option');
    opt.value = current;
    opt.textContent = `Current workspace: ${_basename(current)}`;
    opt.dataset.fullPath = current;
    wsSel.appendChild(opt);
  }
  const browseOpt = document.createElement('option');
  browseOpt.value = '__browse__';
  browseOpt.textContent = current ? 'Pick a different workspace…' : 'Pick a workspace…';
  wsSel.appendChild(browseOpt);
  const customOpt = document.createElement('option');
  customOpt.value = '__custom__';
  customOpt.textContent = 'Custom path…';
  wsSel.appendChild(customOpt);
  wrap.appendChild(wsSel);

  const browseBtn = document.createElement('button');
  browseBtn.type = 'button';
  browseBtn.className = 'browse';
  browseBtn.textContent = 'Browse…';
  browseBtn.title = 'Open the workspace browser';
  browseBtn.addEventListener('click', () => _openWorkspaceBrowser((picked) => {
    if (!picked) return;
    // Add (or replace) the chosen workspace as the selected option.
    let opt = wsSel.querySelector(`option[value="${CSS.escape(picked)}"]`);
    if (!opt) {
      opt = document.createElement('option');
      opt.value = picked;
      opt.textContent = `Workspace: ${_basename(picked)}`;
      opt.dataset.fullPath = picked;
      wsSel.insertBefore(opt, wsSel.firstChild);
    }
    wsSel.value = picked;
    _refreshCodebasePathCaption(row, picked);
  }));
  wrap.appendChild(browseBtn);
  cfg.appendChild(wrap);

  // The path caption below the dropdown.
  const caption = document.createElement('div');
  caption.className = 'ws-path';
  caption.textContent = current ? `Resolves to: ${current}` : 'Resolves to: (no workspace set)';
  cfg.appendChild(caption);

  // Hidden state on the row so _readFieldValues can find the path.
  // The data attr is the source of truth — the select is just a UX aid.
  row.dataset.workspacePath = current || '';
  wsSel.addEventListener('change', () => {
    const v = wsSel.value;
    if (v === '__browse__') {
      _openWorkspaceBrowser((picked) => {
        if (!picked) { wsSel.value = current || '__browse__'; return; }
        let opt = wsSel.querySelector(`option[value="${CSS.escape(picked)}"]`);
        if (!opt) {
          opt = document.createElement('option');
          opt.value = picked;
          opt.textContent = `Workspace: ${_basename(picked)}`;
          opt.dataset.fullPath = picked;
          wsSel.insertBefore(opt, wsSel.firstChild);
        }
        wsSel.value = picked;
        row.dataset.workspacePath = picked;
        _refreshCodebasePathCaption(row, picked);
      });
      return;
    }
    if (v === '__custom__') {
      _revealCustomPathInput(row, caption, current);
      return;
    }
    row.dataset.workspacePath = v;
    _refreshCodebasePathCaption(row, v);
  });
  // Keep a ref so the field reader can find the workspace path even
  // after a custom-path detour rewrites the children.
  row._workspaceSelect = wsSel;
}

function _refreshCodebasePathCaption(row, path) {
  const cap = row.querySelector('.ws-path');
  if (cap) cap.textContent = path ? `Resolves to: ${path}` : 'Resolves to: (none)';
}

function _revealCustomPathInput(row, caption, fallback) {
  // Replace the workspace dropdown with a plain text input so the user
  // can type any path. The "path" config key is what FolderSource /
  // CodebaseSource actually read.
  const wrap = row.querySelector('.research-source-picker-workspace');
  if (!wrap) return;
  wrap.innerHTML = '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'research-source-picker-workspace-select';
  input.placeholder = 'C:\\path\\to\\codebase';
  input.value = row.dataset.workspacePath || fallback || '';
  input.addEventListener('input', () => {
    row.dataset.workspacePath = input.value.trim();
    _refreshCodebasePathCaption(row, input.value.trim());
  });
  wrap.appendChild(input);
  // Re-attach the Browse button so the user can still use the modal.
  const browseBtn = document.createElement('button');
  browseBtn.type = 'button';
  browseBtn.className = 'browse';
  browseBtn.textContent = 'Browse…';
  browseBtn.addEventListener('click', () => _openWorkspaceBrowser((picked) => {
    if (!picked) return;
    input.value = picked;
    row.dataset.workspacePath = picked;
    _refreshCodebasePathCaption(row, picked);
  }));
  wrap.appendChild(browseBtn);
  row.dataset.workspacePath = input.value.trim();
  input.focus();
}

function _readCurrentWorkspace() {
  try {
    return localStorage.getItem('odysseus-workspace') || '';
  } catch { return ''; }
}

function _basename(p) {
  if (!p) return '';
  const parts = p.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

async function _openWorkspaceBrowser(onPicked) {
  // Reuse the existing workspace browser (used by the chat input pill).
  // Lazy-import so the picker doesn't pull in the whole workspace.js
  // (and its modal DOM) just to render.
  try {
    const mod = await import('../workspace.js');
    if (mod && typeof mod.openWorkspaceBrowser === 'function') {
      mod.openWorkspaceBrowser({
        onSelect: (path) => { try { onPicked && onPicked(path); } catch {} },
      });
      return;
    }
  } catch (e) {
    // fall through to manual browse
  }
  // Fallback: call the workspace browse endpoint and prompt.
  try {
    const r = await fetch('/api/workspace/browse?path=', { credentials: 'same-origin' });
    if (!r.ok) { alert('Workspace browser unavailable'); return; }
    const data = await r.json();
    const path = prompt('Path to workspace folder:', data.path || '');
    if (path) onPicked && onPicked(path);
  } catch (e) {
    alert('Workspace browser unavailable: ' + e.message);
  }
}

function _buildField(key, spec) {
  const wrap = document.createElement('label');
  wrap.className = 'research-source-picker-field';
  const lbl = document.createElement('span');
  lbl.className = 'research-source-picker-field-label';
  lbl.textContent = _humanFieldLabel(key);
  wrap.appendChild(lbl);

  let input;
  if (spec.type === 'boolean') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!spec.default;
  } else if (spec.type === 'integer') {
    input = document.createElement('input');
    input.type = 'number';
    input.value = String(spec.default ?? '');
    if (spec.minimum !== undefined) input.min = spec.minimum;
    if (spec.maximum !== undefined) input.max = spec.maximum;
  } else if (spec.type === 'array') {
    input = document.createElement('input');
    input.type = 'text';
    input.value = Array.isArray(spec.default) ? spec.default.join(', ') : '';
    input.placeholder = 'comma-separated';
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.value = String(spec.default ?? '');
  }
  input.dataset.fieldKey = key;
  input.className = 'research-source-picker-field-input';
  wrap.appendChild(input);
  return wrap;
}

function _syncAddButtonVisibility(list) {
  const root = list.closest('.' + _ROOT_CLASS);
  const addBtn = root?.querySelector('.research-source-picker-add');
  const max = list._ownerState?.maxSources || 4;
  if (addBtn) addBtn.style.display = list.children.length >= max ? 'none' : '';
  const rows = Array.from(list.querySelectorAll('.research-source-picker-row'));
  for (const row of rows) {
    const remove = row.querySelector('.research-source-picker-remove');
    if (remove) remove.style.display = rows.length <= 1 ? 'none' : '';
  }
}

/**
 * Read the current selection back out of the picker DOM.
 * @param {HTMLElement} rootEl  the same container that was passed to renderSourcePicker
 * @returns {Array<{type:string, config:object}>|null}
 *   null if the picker isn't rendered; otherwise the array (possibly empty).
 */
export function readPickerSelection(rootEl) {
  if (!rootEl) return null;
  const state = _pickers.get(rootEl);
  if (!state) return null;
  const rows = state.list.querySelectorAll('.research-source-picker-row');
  const out = [];
  for (const row of rows) {
    const sel = row.querySelector('.research-source-picker-type');
    const type = sel?.value;
    if (!type) continue;
    const config = {};
    if (type === 'kb') {
      const kbId = sel?.dataset?.kbId || sel?.selectedOptions?.[0]?.dataset?.kbId;
      if (!kbId) continue;
      config.kb_id = kbId;
    } else if (type === 'library') {
      const checked = Array.from(
        row.querySelectorAll('.research-source-picker-config input[type=checkbox]')
      ).filter(cb => cb.checked).map(cb => cb.dataset.libraryId || cb.value);
      config.report_ids = checked;
    } else if (type === 'chats') {
      // No config — the source always searches every session.
    } else if (type === 'codebase' || type === 'folder') {
      // The workspace selector writes the chosen path to row.dataset.workspacePath
      // (or the user typed a custom path into the text input). Empty path = skip.
      const path = (row.dataset.workspacePath || '').trim();
      if (!path) continue;
      config.path = path;
    } else {
      for (const input of row.querySelectorAll('.research-source-picker-field-input')) {
        const key = input.dataset.fieldKey;
        if (!key) continue;
        const spec = (state.sources.find(s => s.type === type) || {}).config_schema?.[key] || {};
        if (spec.type === 'boolean') {
          config[key] = input.checked;
        } else if (spec.type === 'integer') {
          const n = parseInt(input.value, 10);
          if (!Number.isNaN(n)) config[key] = n;
        } else if (spec.type === 'array') {
          config[key] = input.value.split(',').map(s => s.trim()).filter(Boolean);
        } else if (input.value) {
          config[key] = input.value;
        }
      }
    }
    out.push({ type, config });
  }
  return out;
}

function _escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function _escapeAttr(s) { return _escapeHtml(s).replace(/\n/g, '&#10;'); }

function _humanFieldLabel(key) {
  const labels = {
    max_urls_per_round: 'URLs / round',
    max_content_chars: 'Content chars',
    extraction_concurrency: 'Concurrency',
    extraction_timeout: 'Timeout',
    max_file_bytes: 'Max file bytes',
    respect_gitignore: 'Gitignore',
    max_chunks: 'Max chunks',
    use_tree_sitter: 'Tree-sitter',
    limit_per_folder: 'Per folder',
    limit_per_report: 'Per report',
    exclude_dirs: 'Exclude dirs',
  };
  return labels[key] || String(key || '').replace(/_/g, ' ');
}
