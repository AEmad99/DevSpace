/* Source picker widget for Deep Research (issue #2 / M4).

Renders a multi-selectable list of research sources above the query
textarea. Each source has a config form generated from the server's
config_schema. Returns the chosen sources as a `sources` array
compatible with the `/api/research/start` body shape:

  sources: [{ type: "folder", config: { path: "..." } }, ...]

When nothing is selected, the picker returns `null` so the server keeps
its default behavior (InternetSource only) — backward-compatible with
the pre-M4 single-source flow.

Usage from the research panel (panel.js):

  import { renderSourcePicker, readPickerSelection } from './source_picker.js';
  renderSourcePicker(paneEl, { defaultSources: ['internet'] });
  // ...later, when launching:
  const sources = readPickerSelection(paneEl);
  settings.sources = sources;   // null = default; [] = default too; non-empty = explicit

The picker is intentionally a small DOM-only widget — no framework, no
build step. It calls the existing /api/research/sources and
/api/knowledge_bases endpoints to populate the dropdowns.
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

  // Fetch sources + KBs in parallel.
  const [srcResp, kbResp] = await Promise.all([
    fetch('/api/research/sources', { credentials: 'same-origin' })
      .then(r => r.json()).catch(() => ({ sources: [], feature_enabled: false })),
    fetch('/api/knowledge_bases', { credentials: 'same-origin' })
      .then(r => r.json()).catch(() => ({ knowledge_bases: [] })),
  ]);

  state.sources = srcResp.sources || [];
  state.kbs = (kbResp.knowledge_bases || []);
  state.featureEnabled = !!srcResp.feature_enabled;

  // Pre-populate one row per default.
  for (const s of initial) addPickerRow(list, maxSources, s);
  // If nothing was added (e.g. empty defaults), add one Internet row.
  if (!list.children.length) addPickerRow(list, maxSources, 'internet');
}

function addPickerRow(list, maxSources, preset) {
  if (list.children.length >= maxSources) return;
  const row = document.createElement('div');
  row.className = 'research-source-picker-row';

  // Type selector
  const typeSel = document.createElement('select');
  typeSel.className = 'research-source-picker-type';
  typeSel.innerHTML = _buildTypeOptions(list._ownerState, preset);
  // If the preset is a kb, the dropdown selection needs to know which KB.
  if (preset && preset.startsWith('kb:')) {
    typeSel.value = 'kb';
    typeSel.dataset.kbId = preset.slice(3);
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
    const type = typeSel.value;
    if (type === 'kb') {
      _renderKBConfig(cfg, list._ownerState);
    } else {
      _renderSourceConfig(cfg, list._ownerState, type);
    }
  };
  typeSel.addEventListener('change', renderConfig);
  renderConfig();
}

function _buildTypeOptions(state, preset) {
  // Internet is always available; non-internet sources only when flag is on.
  const all = (state?.sources || []).filter(s =>
    s.type === 'internet' || state?.featureEnabled);
  const options = ['<option value="internet">Internet (default)</option>'];
  for (const s of all) {
    if (s.type === 'internet') continue;
    options.push(`<option value="${s.type}">${_escapeHtml(s.name)}</option>`);
  }
  if (state?.featureEnabled && (state?.kbs || []).length > 0) {
    options.push(`<optgroup label="Knowledge Bases">`);
    for (const k of state.kbs) {
      options.push(`<option value="kb" data-kb-id="${_escapeAttr(k.id)}">${_escapeHtml(k.name)}</option>`);
    }
    options.push(`</optgroup>`);
  }
  return options.join('');
}

function _renderSourceConfig(cfg, state, type) {
  const def = (state?.sources || []).find(s => s.type === type);
  if (!def) return;
  const schema = def.config_schema || {};
  for (const [key, spec] of Object.entries(schema)) {
    if (key === 'collection_name') continue;   // auto-derived; no UI input
    if (spec.default === undefined) continue;
    cfg.appendChild(_buildField(key, spec));
  }
  if (!cfg.children.length) {
    const note = document.createElement('span');
    note.className = 'research-source-picker-note';
    note.textContent = 'No configuration needed.';
    cfg.appendChild(note);
  }
}

function _renderKBConfig(cfg, state) {
  // KB selection is already in the type <select>. The config area shows
  // a read-only summary of the chosen KB's folders so the user knows
  // what they're researching over.
  const row = cfg.closest('.research-source-picker-row');
  const sel = row?.querySelector('.research-source-picker-type');
  const kbId = sel?.dataset?.kbId || sel?.selectedOptions?.[0]?.dataset?.kbId;
  const kb = (state?.kbs || []).find(k => k.id === kbId);
  const summary = document.createElement('div');
  summary.className = 'research-source-picker-kb-summary';
  if (!kb) {
    summary.textContent = 'Select a knowledge base from the dropdown.';
  } else {
    const folderList = (kb.folders || []).map(f => f.path).join('\n');
    summary.innerHTML = `
      <div class="research-source-picker-kb-name">${_escapeHtml(kb.name)}</div>
      <div class="research-source-picker-kb-folders" title="${_escapeAttr(folderList)}">${(kb.folders || []).length} folder(s)</div>
    `;
  }
  cfg.appendChild(summary);

  // Re-render when the KB changes.
  sel?.addEventListener('change', () => {
    sel.dataset.kbId = sel.selectedOptions[0]?.dataset?.kbId || '';
    _renderKBConfig(cfg, state);
  });
}

function _buildField(key, spec) {
  const wrap = document.createElement('label');
  wrap.className = 'research-source-picker-field';
  const lbl = document.createElement('span');
  lbl.className = 'research-source-picker-field-label';
  lbl.textContent = key;
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
