// static/js/gitPanel.js
// Git status/diff/stage/commit UI — a tab inside the Code Workspace panel.
// Talks to /api/workspace/git/* (shells git in the workspace root server-side).
// Mounted lazily by codeWorkspace.js the first time the Git tab is opened.

const API = '/api/workspace/git';
let _container = null;

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

async function _api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

// status code → short badge char + semantic class (mirrors git's XY codes).
function _badge(f) {
  if (f.untracked) return { ch: 'U', cls: 'untracked' };
  const c = f.staged ? f.x : f.y;
  if (c === 'D') return { ch: 'D', cls: 'del' };
  if (c === 'A') return { ch: 'A', cls: 'add' };
  if (c === 'R') return { ch: 'R', cls: 'mod' };
  return { ch: c && c.trim() ? c : 'M', cls: f.staged ? 'staged' : 'mod' };
}

function _err(msg) {
  const el = _container && _container.querySelector('#cw-git-error');
  if (!el) return;
  el.textContent = msg || '';
  el.style.display = msg ? 'block' : 'none';
}

function _renderDiff(text) {
  const diffEl = _container.querySelector('#cw-git-diff');
  if (!text || !text.trim()) {
    diffEl.innerHTML = '<div class="cw-git-diff-empty">No textual diff (binary or no changes).</div>';
    return;
  }
  const rows = text.split('\n').map(line => {
    let cls = 'diff-ctx', t = line;
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file')) cls = 'diff-meta';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) { cls = 'diff-add'; t = line.slice(1); }
    else if (line.startsWith('-')) { cls = 'diff-del'; t = line.slice(1); }
    else if (line.startsWith(' ')) t = line.slice(1);
    return `<span class="${cls}">${esc(t) || '&nbsp;'}</span>`;
  }).join('');
  diffEl.innerHTML = `<pre>${rows}</pre>`;
}

async function _loadDiff(f, rowEl) {
  _container.querySelectorAll('.cw-git-file.active').forEach(e => e.classList.remove('active'));
  if (rowEl) rowEl.classList.add('active');
  const diffEl = _container.querySelector('#cw-git-diff');
  diffEl.innerHTML = '<div class="cw-git-diff-empty">Loading…</div>';
  try {
    const res = await _api('GET', '/diff?path=' + encodeURIComponent(f.path) + (f.staged ? '&staged=true' : ''));
    _renderDiff(res.diff);
  } catch (e) {
    diffEl.innerHTML = `<div class="cw-git-diff-empty">${esc(e.message)}</div>`;
  }
}

async function _toggleStage(f) {
  try {
    // The backend returns HTTP 200 with {ok:false, error} on a git failure, so
    // a non-throwing _api call isn't proof of success — check {ok} explicitly.
    const r = await _api('POST', f.staged ? '/unstage' : '/stage', { path: f.path });
    if (r && r.ok === false) { _err(r.error || 'Stage failed'); return; }
    _err('');
    refreshGitPanel();
  } catch (e) { _err(e.message); }
}

// Stage/unstage everything at once. The backend treats a blank path as
// `git add -A` (stage all) / `git reset` (unstage all).
async function _stageAll(stage) {
  try {
    const r = await _api('POST', stage ? '/stage' : '/unstage', { path: '' });
    if (r && r.ok === false) { _err(r.error || 'Stage failed'); return; }
    _err('');
    refreshGitPanel();
  } catch (e) { _err(e.message); }
}

async function _commit() {
  const msgEl = _container.querySelector('#cw-git-msg');
  const msg = (msgEl.value || '').trim();
  if (!msg) { _err('Enter a commit message.'); return; }
  const btn = _container.querySelector('#cw-git-commit-btn');
  btn.disabled = true; btn.textContent = 'Committing…';
  try {
    const r = await _api('POST', '/commit', { message: msg });
    if (r.ok) { msgEl.value = ''; _err(''); refreshGitPanel(); }
    else _err(r.error || 'Commit failed');
  } catch (e) { _err(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Commit staged'; }
}

export function mountGitPanel(container) {
  _container = container;
  container.innerHTML = `
    <div class="cw-git">
      <div class="cw-git-side">
        <div class="cw-git-bar">
          <svg class="cw-git-branch-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/><path d="M6 8.5v7"/><path d="M18 10.5a6 6 0 0 1-6 6H8.5"/></svg>
          <span class="cw-git-branch" id="cw-git-branch">…</span>
          <button class="cw-git-refresh" id="cw-git-refresh" title="Refresh">⟳</button>
        </div>
        <div class="cw-git-actions" id="cw-git-actions" hidden>
          <button class="cw-git-allbtn" id="cw-git-stage-all" type="button" title="Stage every change">+ Stage all</button>
          <button class="cw-git-allbtn" id="cw-git-unstage-all" type="button" title="Unstage every change">− Unstage all</button>
        </div>
        <div class="cw-git-list" id="cw-git-list"></div>
        <div class="cw-git-commit">
          <textarea class="cw-git-msg" id="cw-git-msg" placeholder="Commit message…" rows="2" spellcheck="false"></textarea>
          <button class="cw-git-commit-btn" id="cw-git-commit-btn">Commit staged</button>
          <div class="cw-git-error" id="cw-git-error" style="display:none"></div>
        </div>
      </div>
      <div class="cw-git-diff" id="cw-git-diff"><div class="cw-git-diff-empty">Select a changed file to see its diff.</div></div>
    </div>`;
  container.querySelector('#cw-git-refresh').addEventListener('click', () => refreshGitPanel());
  container.querySelector('#cw-git-stage-all').addEventListener('click', () => _stageAll(true));
  container.querySelector('#cw-git-unstage-all').addEventListener('click', () => _stageAll(false));
  container.querySelector('#cw-git-commit-btn').addEventListener('click', () => _commit());
  refreshGitPanel();
}

export async function refreshGitPanel() {
  if (!_container) return;
  const list = _container.querySelector('#cw-git-list');
  const branchEl = _container.querySelector('#cw-git-branch');
  const actions = _container.querySelector('#cw-git-actions');
  if (actions) actions.hidden = true;   // re-shown below only when there are changes
  list.innerHTML = '<div class="cw-git-msg-line">Loading…</div>';
  let res;
  try { res = await _api('GET', '/status'); }
  catch (e) { list.innerHTML = `<div class="cw-git-msg-line">${esc(e.message)}</div>`; return; }
  if (!res.is_repo) {
    branchEl.textContent = 'no repo';
    list.innerHTML = '<div class="cw-git-msg-line">This workspace folder is not a git repository.</div>';
    return;
  }
  branchEl.textContent = res.branch || '(detached)';
  if (!res.files.length) {
    list.innerHTML = '<div class="cw-git-msg-line">✓ Working tree clean</div>';
    _renderDiff('');
    return;
  }
  // Have changes → reveal the bulk actions, enabling each only when it applies.
  if (actions) {
    actions.hidden = false;
    const stageAllBtn = _container.querySelector('#cw-git-stage-all');
    const unstageAllBtn = _container.querySelector('#cw-git-unstage-all');
    if (stageAllBtn) stageAllBtn.disabled = !res.files.some(f => f.unstaged);
    if (unstageAllBtn) unstageAllBtn.disabled = !res.files.some(f => f.staged);
  }
  list.innerHTML = '';
  for (const f of res.files) {
    const b = _badge(f);
    const row = document.createElement('div');
    row.className = 'cw-git-file' + (f.staged ? ' staged' : '');
    row.innerHTML =
      `<span class="cw-git-badge cw-git-${b.cls}" title="${f.staged ? 'staged' : 'unstaged'}">${esc(b.ch)}</span>` +
      `<span class="cw-git-path">${esc(f.path)}</span>` +
      `<button class="cw-git-stage" title="${f.staged ? 'Unstage' : 'Stage'}">${f.staged ? '−' : '+'}</button>`;
    row.querySelector('.cw-git-path').addEventListener('click', () => _loadDiff(f, row));
    row.querySelector('.cw-git-stage').addEventListener('click', (e) => { e.stopPropagation(); _toggleStage(f); });
    list.appendChild(row);
  }
}

// Back-compat export name from the Phase 0 scaffold.
export function initGitPanel(container) { if (container) mountGitPanel(container); }

export default { mountGitPanel, refreshGitPanel, initGitPanel };
