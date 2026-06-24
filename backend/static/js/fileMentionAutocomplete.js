// static/js/fileMentionAutocomplete.js
// @filename mention autocomplete for the chat composer. Forked from
// slashAutocomplete.js. Triggers on "@partial" at the caret (preceded by start
// or whitespace), queries GET /api/workspace/search?q=, and inserts the chosen
// workspace-relative path as "@<path> " so the agent gets a concrete reference.

const POPUP_ID = 'file-mention-autocomplete';
const MAX_VISIBLE = 12;
// Capture the "@partial" immediately before the caret. The partial may be empty
// right after "@" (we wait for >=1 char before querying — the search endpoint
// requires a non-empty query).
const TRIGGER_RE = /(^|\s)@([\w.\-/\\]*)$/;

const ICON_DIR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.82 1.2a2 2 0 0 0 1.69.9H19a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICON_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

function _esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\'': '&#39;' }[c]));
}

function _rel(full, root) {
  let r = full || '';
  if (root && r.startsWith(root)) r = r.slice(root.length).replace(/^[\\/]+/, '');
  return r.replace(/\\/g, '/');
}

function _dirOf(rel) {
  const i = rel.lastIndexOf('/');
  return i >= 0 ? rel.slice(0, i + 1) : '';
}

function _ensurePopup() {
  let el = document.getElementById(POPUP_ID);
  if (el) return el;
  el = document.createElement('div');
  el.id = POPUP_ID;
  el.className = 'file-ac-popup';
  el.setAttribute('role', 'listbox');
  el.setAttribute('aria-label', 'Workspace files');
  document.body.appendChild(el);
  return el;
}

function _position(popup, textarea) {
  const r = textarea.getBoundingClientRect();
  const maxH = Math.min(window.innerHeight * 0.5, 320);
  popup.style.maxHeight = maxH + 'px';
  popup.style.left = Math.round(r.left) + 'px';
  popup.style.width = Math.max(280, Math.round(Math.min(r.width, 520))) + 'px';
  if (r.top > maxH + 20) {
    popup.style.bottom = (window.innerHeight - r.top + 6) + 'px';
    popup.style.top = '';
  } else {
    popup.style.top = (r.bottom + 6) + 'px';
    popup.style.bottom = '';
  }
}

function _render(popup, items, sel, query) {
  if (!items.length) {
    popup.innerHTML = `<div class="file-ac-empty">No files match <code>${_esc(query)}</code></div>`;
    return;
  }
  let html = '';
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const s = i === sel ? ' file-ac-row-sel' : '';
    html += `<div class="file-ac-row${s}" role="option" data-idx="${i}" data-path="${_esc(it.rel)}">`
         +    `<span class="file-ac-icon">${it.is_dir ? ICON_DIR : ICON_FILE}</span>`
         +    `<span class="file-ac-name">${_esc(it.name)}</span>`
         +    `<span class="file-ac-path">${_esc(it.dir)}</span>`
         + `</div>`;
  }
  popup.innerHTML = html;
  const selEl = popup.querySelector('.file-ac-row-sel');
  if (selEl) selEl.scrollIntoView({ block: 'nearest' });
}

export function initFileMentionAutocomplete(textarea) {
  if (!textarea || textarea._fileAcWired) return;
  textarea._fileAcWired = true;

  let popup = null, visible = false, items = [], sel = 0, root = null, seq = 0, debounceT = null;

  const hide = () => { if (visible) { visible = false; if (popup) popup.style.display = 'none'; } };
  const show = () => {
    if (!popup) popup = _ensurePopup();
    visible = true; popup.style.display = 'block'; _position(popup, textarea);
  };

  // The "@partial" ending at the caret, or null.
  const currentMatch = () => {
    const caret = textarea.selectionStart != null ? textarea.selectionStart : textarea.value.length;
    const upto = textarea.value.slice(0, caret);
    const m = upto.match(TRIGGER_RE);
    if (!m) return null;
    return { atIdx: m.index + m[1].length, partial: m[2], caret };
  };

  const query = async () => {
    const m = currentMatch();
    if (!m || !m.partial) { hide(); return; }
    const mySeq = ++seq;
    try {
      const res = await fetch('/api/workspace/search?q=' + encodeURIComponent(m.partial), { credentials: 'same-origin' });
      if (mySeq !== seq) return;  // a newer keystroke superseded this
      if (!res.ok) { hide(); return; }  // 409 = no workspace set, etc.
      const data = await res.json();
      root = data.root || root;
      items = (data.hits || []).slice(0, MAX_VISIBLE).map(h => {
        const rel = _rel(h.path, data.root || root);
        return { name: h.name, rel, dir: _dirOf(rel), is_dir: h.is_dir };
      });
      if (!currentMatch()) { hide(); return; }  // caret moved off the trigger
      sel = 0; show(); _render(popup, items, sel, m.partial);
    } catch { hide(); }
  };

  const refresh = () => { clearTimeout(debounceT); debounceT = setTimeout(query, 150); };

  const insert = (relpath) => {
    const m = currentMatch();
    if (!m) { hide(); return; }
    const before = textarea.value.slice(0, m.atIdx);
    const after = textarea.value.slice(m.caret);
    const ins = '@' + relpath + ' ';
    textarea.value = before + ins + after;
    const pos = (before + ins).length;
    textarea.setSelectionRange(pos, pos);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    textarea.focus();
    hide();
  };

  textarea.addEventListener('input', refresh);
  textarea.addEventListener('blur', () => setTimeout(hide, 120));

  textarea.addEventListener('keydown', (e) => {
    if (!visible || !items.length) return;
    const q = currentMatch()?.partial || '';
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = (sel + 1) % items.length; _render(popup, items, sel, q); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = (sel - 1 + items.length) % items.length; _render(popup, items, sel, q); }
    else if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); insert(items[sel].rel); }
    else if (e.key === 'Escape') { e.preventDefault(); hide(); }
  });

  window.addEventListener('resize', () => { if (visible) _position(popup, textarea); });

  document.addEventListener('mousedown', (e) => {
    if (!visible || !popup) return;
    const row = e.target.closest && e.target.closest('.file-ac-row');
    if (row && popup.contains(row)) { e.preventDefault(); if (row.dataset.path) insert(row.dataset.path); }
  });
}

export default { initFileMentionAutocomplete };
