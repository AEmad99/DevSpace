// static/js/toolOutputHooks.js
// Shared agent tool-output hooks, used by both chat.js (live streaming) and
// chatRenderer.js (history replay). Extracted to remove a byte-identical
// duplicate definition of these two functions across both files.

// Highlight tool output / diffs with hljs.
export function highlightToolOutput(node) {
  if (!node || !window.hljs) return;
  // Syntax-highlight tool OUTPUT <pre> (read_file / grep / cat-style results).
  // The diff <pre class="diff-pre"> keeps its own add/del colouring (excluded).
  // Auto-detect the language and only apply a high-confidence result, so logs,
  // errors and short output stay plain rather than getting mis-coloured.
  node.querySelectorAll('.agent-tool-output > pre:not(.diff-pre)').forEach(pre => {
    if (pre.dataset.hl) return;
    pre.dataset.hl = '1';
    const text = pre.textContent || '';
    if (text.length < 24 || text.indexOf('\n') === -1) return;
    try {
      const r = window.hljs.highlightAuto(text);
      if (r && r.language && r.relevance >= 10) {
        pre.innerHTML = r.value;
        pre.classList.add('hljs');
      }
    } catch {}
  });
}

// Inject Accept/Reject diff-approval buttons into a rendered diff node.
// `diff` is the json.diff / ev.diff object (may be undefined).
export function _attachDiffApprovalButtons(node, diff) {
  // Auto/applied edits are already on disk → tell the Code Workspace panel to
  // reload the file. Staged (strict-mode) edits are NOT written yet, so don't.
  if (diff && diff.file && !diff.staged) {
    try {
      document.dispatchEvent(new CustomEvent('workspace:diff-applied', {
        detail: { file: diff.file, path: diff.path || '' },
      }));
    } catch {}
  }
  // Auto-mode edits are silent: no Apply/Discard bar, just a small "Applied ✓"
  // badge so the user has a visual breadcrumb. The actual write already
  // happened on the backend.
  if (diff && diff.review === 'auto') {
    if (!diff.path) return;
    const det = node.querySelector('.agent-tool-diff');
    const host = (det && det.parentNode) || node;
    if (!host || host.querySelector('.diff-applied-badge')) return;
    const badge = document.createElement('div');
    badge.className = 'diff-applied-badge';
    const file = diff.file || diff.path;
    badge.innerHTML =
      `<span class="diff-applied-tick" aria-hidden="true">✓</span>` +
      `<span class="diff-applied-text">Applied — ${escText(file)}</span>` +
      (diff.path
        ? `<button type="button" class="diff-open-editor" data-path="${escAttr(diff.path)}" title="Open ${escAttr(file)} in the Code Workspace editor">Open in editor</button>`
        : '');
    host.appendChild(badge);
    return;
  }
  // No checkpoint → nothing to accept/reject (capture failed or non-edit diff).
  if (!diff || !diff.checkpoint_id) return;
  const det = node.querySelector('.agent-tool-diff');
  const host = (det && det.parentNode) || node;
  if (!host || host.querySelector('.diff-actions')) return;
  const staged = !!diff.staged;
  const bar = document.createElement('div');
  bar.className = 'diff-actions' + (staged ? ' staged' : '');
  bar.dataset.cp = diff.checkpoint_id;
  bar.dataset.staged = staged ? '1' : '0';
  bar.dataset.path = diff.path || '';
  bar.dataset.file = diff.file || '';
  // "Open in editor" deep-links to codeWorkspace.js for the changed file —
  // lets the user jump straight to the file from any edit card without
  // having to navigate the file tree. The click handler is delegated from
  // chat.js (see initListeners → .open-in-editor click).
  const openBtnHtml = diff.path
    ? `<button type="button" class="diff-open-editor" data-path="${escAttr(diff.path)}" title="Open ${escAttr(diff.file || diff.path)} in the Code Workspace editor">Open in editor</button>`
    : '';
  bar.innerHTML =
    `<span class="diff-actions-label">${staged ? 'Staged — not yet applied' : 'Applied'}</span>` +
    openBtnHtml +
    `<button type="button" class="diff-accept">${staged ? 'Apply' : 'Keep'}</button>` +
    `<button type="button" class="diff-reject">${staged ? 'Discard' : 'Revert'}</button>`;
  host.appendChild(bar);
}

// Tiny attribute escaper for the "Open in editor" data-path. Avoids pulling
// a full escaper into a shared hooks module.
function escAttr(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Tiny text escaper for the auto-mode "Applied — <file>" label.
function escText(s) {
  return String(s || '').replace(/[&<>]/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;',
  }[c]));
}
