// static/js/toolOutputHooks.js
// Shared agent tool-output hooks, used by both chat.js (live streaming) and
// chatRenderer.js (history replay). Extracted to remove a byte-identical
// duplicate definition of these two functions across both files.

function escAttr(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/** Unified diff card for write_file / edit_file tool results. */
export function buildAgentDiffHtml(d, esc) {
  if (!d || !d.text) return '';
  const staged = !!d.staged;
  const isAuto = d.review === 'auto' || (!staged && d.review !== 'strict');
  const stat = [
    d.new_file ? '<span class="diff-stat-new">new</span>' : '',
    d.added ? `<span class="diff-stat-add">+${d.added}</span>` : '',
    d.removed ? `<span class="diff-stat-del">−${d.removed}</span>` : '',
  ].filter(Boolean).join(' ');
  const rows = d.text.split('\n').map(line => {
    let cls = 'diff-ctx';
    let text = line;
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) { cls = 'diff-add'; text = line.slice(1); }
    else if (line.startsWith('-')) { cls = 'diff-del'; text = line.slice(1); }
    else if (line.startsWith(' ')) { text = line.slice(1); }
    return `<span class="${cls}">${esc(text) || '&nbsp;'}</span>`;
  }).join('');
  const modeClass = staged ? 'diff-mode-staged' : (isAuto ? 'diff-mode-auto' : '');
  const statusPill = staged
    ? '<span class="diff-status-pill staged">Awaiting approval</span>'
    : (isAuto ? '<span class="diff-status-pill applied">Written</span>' : '');
  const openBtn = d.path
    ? `<button type="button" class="diff-open-editor diff-summary-link" data-path="${escAttr(d.path)}" title="Open in Code Workspace">Open</button>`
    : '';
  const pathTitle = d.path ? ` title="${escAttr(d.path)}"` : '';
  return `<details class="agent-tool-output agent-tool-diff ${modeClass}">` +
    `<summary${pathTitle}>` +
    `<span class="diff-file-icon" aria-hidden="true">⎘</span>` +
    `<span class="diff-file">${esc(d.file || 'diff')}</span>` +
    `<span class="diff-summary-stats">${stat}</span>` +
    `${statusPill}${openBtn}` +
    `</summary><pre class="diff-pre">${rows}</pre></details>`;
}

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
      import('./gitPanel.js').then(m => m.refreshGitPanel?.()).catch(() => {});
    } catch {}
  }
  // Auto-mode: edits are already on disk. The collapsible diff card carries
  // a "Written" pill — no extra approval bar (review diffs in Git / Code).
  if (diff && (diff.review === 'auto' || (!diff.staged && diff.review !== 'strict'))) {
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


