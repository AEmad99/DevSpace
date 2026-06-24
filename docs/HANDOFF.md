# Handoff: DevSpace Code Workspace Implementation

## Project overview

DevSpace is a single-user Tauri desktop app wrapping a Python/FastAPI backend
(`D:\projects\DevSpace\backend`) with a vanilla-JS ES-module frontend
(`backend/static`). The backend is a fork of the Odysseus project. The Tauri
shell (`src-tauri/`) spawns the backend as a sidecar and loads its UI in a
webview.

**Run dev:** `tauri dev` from repo root. The shell auto-spawns `backend/` with
`odysseus-ref/venv`'s Python. No env vars needed.

**Python venv for testing:** `D:\projects\DevSpace\odysseus-ref\venv\Scripts\python.exe`
(shareable because it's the same codebase deps).

**Plan document:** `docs/code-workspace-plan.md` — the full 8-phase plan with
file:line references. Read it first.

## What's been done (Phases 0–2)

### Phase 0 — Scaffolding (COMPLETE, verified)
Created 7 empty modules + inert anchors in shared files:
- `routes/code_workspace_routes.py` — empty router (now filled by Phase 1)
- `static/js/codeWorkspace.js` — now filled by Phase 2
- `static/js/terminalPanel.js`, `gitPanel.js`, `fileMentionAutocomplete.js` — still inert stubs
- `src/workspace_checkpoints.py` — stub functions (Phase 3 fills)
- `src/agent_tools/code_quality_tools.py` — 3 stub tool classes (Phase 4 fills)
- `app.py:715` — new router wired in
- `src/agent_tools/__init__.py` — `run_tests`/`lint`/`format` registered in `TOOL_HANDLERS` + `TOOL_TAGS`
- `src/tool_schemas.py` — 3 schemas appended + 3 `elif` clauses in converter
- `src/agent_loop.py` — 3 tool docs in `TOOL_SECTIONS` + added to `"files"` domain set
- `index.html` — `rail-code` + `rail-terminal` rail buttons, `tool-code-btn` + `tool-terminal-btn` sidebar entries, `code-workspace-modal` + `terminal-modal` hidden containers
- `style.css` — fenced CSS blocks for each stream
- `chat.js` / `chatRenderer.js` — no-op hooks `highlightToolOutput(node)` + `_attachDiffApprovalButtons(node,diff)` + delegated `.diff-accept`/`.diff-reject` click branch
- `modalManager.js` — `code-workspace-modal` added to `_AUTO_WIRE` map

### Phase 1 — Workspace backbone backend (COMPLETE, verified)
- `src/settings.py:39` — added `code_workspace_root` + `agent_edit_review` to `DEFAULT_SETTINGS`
- `routes/code_workspace_routes.py` (236 lines) — 5 endpoints:
  - `GET/POST /api/workspace/current` — get/set persisted root (vetted via `vet_workspace()`)
  - `GET /api/workspace/tree` — one-level dir listing (lazy expand, skips .git/node_modules)
  - `GET /api/workspace/file` — read file (truncated at `MAX_READ_CHARS`, returns language hint)
  - `POST /api/workspace/file` — write file (returns unified diff)
  - `GET /api/workspace/search?q=` — recursive fuzzy filename search
  - All paths confined via `_resolve_tool_path_in_workspace()` (sensitive-path denylist + containment)
- **Verified:** 14 live endpoint assertions pass (tree, file round-trip, search, 4 security rejections)

### Phase 2 — Code Workspace panel UI (COMPLETE, polished & verified 2026-06-19)
- `static/js/codeWorkspace.js` (458 lines) — full panel:
  - Draggable modal via `Modals.register` + `makeWindowDraggable`
  - 3 panes: file tree (left) | code editor (center) | diff viewer (right)
  - Workspace picker (folder input + "Set Workspace" button + browse link)
  - File tree with lazy directory navigation + search
  - Code editor: textarea + hljs overlay + language dropdown
  - Save → writes file + shows diff in right pane
  - Live-refresh: listens for `workspace:diff-applied` CustomEvent
  - Self-initializes at module load (`initCodeWorkspace()` called at bottom)
- `static/index.html` — `<script type="module" src="/static/js/codeWorkspace.js">` added before `chat.js`
- `chat.js` / `chatRenderer.js` — `_attachDiffApprovalButtons` hook now dispatches `workspace:diff-applied` CustomEvent
- `style.css` — full CSS for the modal, tree, editor, diff viewer, picker (added after `.agent-tool-diff` styles, ~line 9170)

## Phase 2 display issues — RESOLVED (2026-06-19)

The handoff's "black box over the text" framing was only partly right; a full
browser-render audit (headless Chrome against the live backend, dark + light)
found the real issues and they are now fixed in `style.css` / `codeWorkspace.js`
/ `sw.js`:

1. **Editor pane collapsed (the worst one — was undocumented).** The 3-pane
   flex chain had no definite height (`content.style.height` is only set on a
   resize-drag, never on open) and the editor's children are `position:absolute`,
   so the editor only got height incidentally from the tallest sibling pane —
   open a long file in a small folder and most of it was cut off.
   Fix: `#code-workspace-modal .modal-content { height: min(880px, 90vh); }`.

2. **Editor textarea painted an opaque field over the hljs `<pre>` in LIGHT
   theme and on HOVER (any theme).** The real culprit was NOT the global
   `pre{…!important}` (in the default theme that only yields the readable
   `--hl-bg`). It was the global form rules `:root.light textarea{background:#eaeaea}`
   and `…:hover{background:var(--panel)}` (both specificity 0,1,1) outranking the
   panel's single-class `.cw-editor-textarea` (0,1,0). Fix: scope the panel's
   form-control rules with `#code-workspace-modal` (→ 1,1,0) so they win in every
   theme/state. `.cw-editor-highlight` / `.cw-diff-body pre` also set
   `background: transparent !important` to defeat the global `pre` rule cleanly.

3. **Dead `var(--accent)`.** `--accent` is undefined app-wide (only
   `--accent-primary` exists), so every CW `color-mix(... var(--accent) ...)`
   (active row, hover, focus rings, picker button, diff hunk) was inert. Now uses
   `var(--accent, var(--red))` like the rest of the app. Also `.cw-picker-error`
   was `var(--red)` (which is BLUE here) → now literal `#e74c3c`.

4. **Full-bleed + active marking.** `#code-workspace-modal .modal-content` now
   sets `padding:0; overflow:hidden` (+ header padding); `_openFile` now marks the
   opened row active via `data-path` instead of just clearing all rows.

5. **Polish** (approved design): definite-height panel, accent active-bar, crisp
   SVG file/folder icons (replacing emoji), refined toolbars/search/save, accent-
   dotted diff header with add/del rails, polished picker. Verified dark + light.

6. **Service worker:** `/static/js/codeWorkspace.js` added to `PRECACHE`,
   `CACHE_NAME` bumped `odysseus-v327` → `odysseus-v328`. After pulling, a
   hard-refresh (Ctrl+Shift+R) or SW unregister may be needed once.

Editor language handling (done 2026-06-19): the manual language dropdown was
replaced with automatic detection — `_resolveLang()` maps the file extension to
an hljs language id (`_LANG_BY_EXT`), falling back to a one-time high-confidence
`highlightAuto()` content guess, and the result is shown in a read-only
`.cw-editor-lang` badge. Verified: `.py`→python, `.md`→markdown highlight live.

## How to test each phase

### Phase 0 & 1 (backend)
Boot uvicorn directly (faster than tauri dev for backend testing):
```powershell
$env:AUTH_ENABLED="false"
$env:PYTHONPATH="D:\projects\DevSpace\backend"
& "D:\projects\DevSpace\odysseus-ref\venv\Scripts\python.exe" -m uvicorn app:app --port 8123 --host 127.0.0.1
# (from D:\projects\DevSpace\backend)
```
Then hit endpoints with `Invoke-RestMethod`:
```powershell
$B = "http://127.0.0.1:8123"
Invoke-RestMethod "$B/api/workspace/current"
Invoke-RestMethod "$B/api/workspace/tree"
Invoke-RestMethod "$B/api/workspace/file?path=app.py"
Invoke-RestMethod "$B/api/workspace/search?q=readme"
```

### Phase 2 (frontend)
```
tauri dev
```
Click the `</>` rail button (or "Code" in the Tools sidebar). The Code
Workspace panel should open. Check:
- File tree loads on the left
- Clicking a file opens it in the editor with syntax highlighting
- Save writes the file and shows a diff
- The `⇄` button in the tree toolbar lets you change the workspace folder

**Current settings state:** `data/settings.json` has
`"code_workspace_root": "D:\\projects\\DevSpace"` (the repo root, not
`backend/`).

## Phases 3–8 — all COMPLETE & verified (2026-06-19)

- **Phase 3** — Diff approval — DONE (2026-06-19, verified). `workspace_checkpoints.py` (text/`\n`-normalised journal under `data/checkpoints/`, stale-guarded); `filesystem_tools.py` captures (auto) or stages (strict) per `agent_edit_review`, annotating the diff with `checkpoint_id`/`new_hash`/`staged`; `_active_session_id` contextvar in `tool_execution.py`; `POST /api/workspace/{revert,apply,discard,revert_all/{session_id}}`; `_attachDiffApprovalButtons` + delegated handler in chat.js, mirror in chatRenderer.js; `.diff-actions` CSS.
- **Phase 4** — Native `run_tests`/`lint`/`format` — DONE (2026-06-19, verified). `code_quality_tools.py` auto-detects the runner/linter/formatter (pytest/npm·yarn·pnpm/go/cargo, ruff/eslint/flake8, black/prettier/ruff-format) and runs it via the shell with `cwd=agent_cwd()`, streamed through `_run_subprocess_streaming`; optional `target` is metacharacter-guarded.
- **Phase 5** — Terminal v1 — DONE (2026-06-19, verified). xterm.js + addon-fit vendored in `static/lib`, lazy-loaded; `terminalPanel.js` (write-only xterm renderer, command input + history, SSE parse, ANSI colours). NOTE: `/api/shell/stream` is ADMIN-gated and 403s in single-user mode (AUTH_ENABLED=false installs no auth middleware → no admin), so the terminal uses a NEW **owner-authed** `POST /api/workspace/shell` (streaming, cwd = workspace root) added to `code_workspace_routes.py` — matches plan decision 4. `_shell_cwd()` also points the legacy shell endpoints at the workspace. Verified live: `getcwd()` → the workspace root.
- **Phase 6** — Git panel — DONE (2026-06-19, verified). `git/{status,diff,stage,unstage,commit}` endpoints in `code_workspace_routes.py` (shell `git -C root`, paths confined); `gitPanel.js` (branch bar, status list with X/Y badges + stage/unstage, file diff, commit box); Files/Git tabs added to the panel header in `codeWorkspace.js`; `.cw-git-*` + `.cw-tab*` CSS (form controls panel-scoped).
- **Phase 7** — Quick wins — DONE (2026-06-19, verified). `highlightToolOutput` hljs-highlights tool-output `<pre>` (high-confidence auto-detect, diff `<pre>` excluded) in chat.js + chatRenderer.js; `fileMentionAutocomplete.js` (`@`-mention popup over `/api/workspace/search`, wired to `#message` in chat.js); `.file-ac-*` CSS.
- **Phase 8** — Codex files API (external-token file access). Adds scopes + endpoints to `codex_routes.py`.

## Key files & line references

| File | Purpose | Key lines |
|---|---|---|
| `docs/code-workspace-plan.md` | The full plan | — |
| `routes/code_workspace_routes.py` | Workspace API (Phase 1 done, Phase 3/6 to add) | — |
| `static/js/codeWorkspace.js` | Panel UI (Phase 2, has bug) | — |
| `src/settings.py` | `code_workspace_root` + `agent_edit_review` settings | `:39` |
| `src/tool_execution.py` | `vet_workspace()` `:236`, `_resolve_tool_path_in_workspace()` `:182`, `agent_cwd()` `:260` | — |
| `src/agent_tools/filesystem_tools.py` | `_unified_diff()` `:19`, `edit_file` `:47`, `write_file` `:157` | — |
| `src/agent_tools/__init__.py` | `TOOL_HANDLERS` `:27`, `TOOL_TAGS` `:58` (set, not dict) | — |
| `src/tool_schemas.py` | `FUNCTION_TOOL_SCHEMAS` `:23`, `function_call_to_tool_block` `:1198` | — |
| `src/agent_loop.py` | `TOOL_SECTIONS` `:302`, `_DOMAIN_TOOL_MAP["files"]` `:284` | — |
| `app.py` | Router registration `:574-795`, code workspace router `:715` | — |
| `static/style.css` | `.modal` `:4977`, `.modal-content` `:5019`, `pre { !important }` `:6213`, `.agent-tool-diff` `:9107`, Code Workspace CSS `~:9170` | — |
| `static/index.html` | Rail buttons `:663-688`, sidebar Tools `:821-920`, modal containers `:2422`, script tags `:2425-2459` | — |
| `static/js/modalManager.js` | `Modals.register()` `:1146`, `_AUTO_WIRE` `:1404` | — |
| `static/js/windowDrag.js` | `makeWindowDraggable()` `:57` | — |
| `static/js/chat.js` | Diff render `:2154-2222`, `initListeners()` `:3606`, delegated handlers `:3608-3697` | — |
| `static/js/chatRenderer.js` | Diff render `:2078-2104`, hljs scope `:2128` | — |
| `static/sw.js` | Service worker precache `:15-64`, `CACHE_NAME` `:10` | — |

## Conventions to follow

- **Tool handler signature:** `async def execute(self, content: str, ctx: dict) -> dict`
- **Add-a-tool recipe:** schema in `FUNCTION_TOOL_SCHEMAS` + `elif` in `function_call_to_tool_block` + handler class + register in `TOOL_HANDLERS` + add to `TOOL_TAGS` + docs in `TOOL_SECTIONS` + add to `_DOMAIN_TOOL_MAP["files"]`
- **Append-only** on shared lists (`TOOL_TAGS`, `FUNCTION_TOOL_SCHEMAS`, `DEFAULT_SETTINGS`)
- **Path confinement:** all file/workspace endpoints must use `_resolve_tool_path_in_workspace(root, path)` or `vet_workspace()`
- **Auth posture:** owner-authed (not admin-gated) via `get_current_user(request)`, but confined to workspace
- **Modal pattern:** `Modals.register(id, {railBtnId, sidebarBtnId, closeFn, restoreFn})` + `makeWindowDraggable(modal, {content, header})`
- **Editor pattern:** textarea (transparent text, `caret-color: var(--fg)`) + `<pre>` hljs overlay (absolute positioned, synced scroll)
- **Self-init pattern:** call `initXxx()` at the bottom of the module (ES modules are deferred, DOM is ready)
- **CSS:** avoid `!important` unless overriding a global `!important` rule (like `pre { background: ... !important }`)

## First thing Claude should do

Phases 0–2 are complete and verified (Phase 2 display issues resolved 2026-06-19
— see the "RESOLVED" section above). **Proceed to Phase 3 (Diff approval +
checkpoints).**

Quick re-verify of Phase 2 if needed (no `tauri dev` build required — exercises
the real JS + CSS in a browser):
1. Boot the backend per "How to test" below (uvicorn on 127.0.0.1:8123,
   `AUTH_ENABLED=false`).
2. Open `http://127.0.0.1:8123/` in Chrome → click the `</>` rail button (or
   "Code" in Tools). Confirm: file tree loads, clicking a file fills the editor
   full-height with syntax highlighting (no collapse, no opaque box), the open
   file shows the accent active-bar, Save writes + shows a diff. Toggle the
   theme and re-check (light was the regression case).
