# Plan — DevSpace "Code Workspace" (coding/agentic cockpit)

> Status: PLANNED, not yet built (per user). Decisions locked: edit-review = toggle (auto + strict), terminal = v1 (streaming + agent mirror), build = parallel multi-agent (documented, deferred), workspace = server-persisted. Resume from this file.

## Context

DevSpace (single‑user Tauri desktop app; FastAPI backend at `D:\projects\DevSpace\backend`, vanilla‑JS ES‑module frontend in `backend/static`) already runs on the user's **OpenCode Zen/Go** models and has a powerful agent (read/write/edit files w/ unified diffs, `bash`, `python`, `grep/glob/ls`, web) — but those tools are **headless**: confined to a per‑turn workspace, output buried in chat, no file tree, no diff approval, no terminal, no git UI. The goal is to turn DevSpace into a real **agentic coding cockpit** for the user's chat + coding/agentic workflow, by adding a UI layer over the existing backend plus a few small backend additions.

Confirmed constraints driving the design:
- Agent loop is a **single SSE streaming generator with no pause/resume** (`src/agent_loop.py`; `src/agent_runs.py` = cancel + replay only).
- Active workspace is a **per‑turn form field → `_active_workspace` contextvar** (`routes/chat_routes.py:65`, `tool_execution.py:532`), persisted **client‑side only** (`static/js/workspace.js`, `storage.js:27`). No server‑side "current project".
- Edit/Write tools read old content but **discard** it (`filesystem_tools.py:84`, `:181`); **no undo/checkpoint** subsystem.
- Windows has **no PTY** today (`shell_routes.py` `_generate_win_detached` = log‑tail only).

## Cross‑cutting decisions

1. **Workspace persistence** — add a global setting `code_workspace_root` (`settings.py DEFAULT_SETTINGS`) + `GET/POST /api/workspace/current` (vet via existing `vet_workspace()`, persist via `save_settings`). Keep the per‑turn form field as the **agent's** source of truth (don't touch the contextvar). One chosen root, three readers: agent (form field), file‑tree/editor HTTP (`code_workspace_root`), terminal cwd (`code_workspace_root`) — all vetted by the single `vet_workspace()`.
2. **Edit review = toggle** (setting `agent_edit_review`, mirrors `agent_email_confirm` at `settings.py:39` (inside `DEFAULT_SETTINGS`, an append-friendly dict)):
   - `auto` (**default**): apply‑then‑revert. Edit writes immediately (agent keeps flowing); each diff in chat gets **Accept/Reject**; Reject restores from a checkpoint.
   - `strict`: stage‑then‑approve. `edit_file`/`write_file` do **not** write; they return a pending diff + the result text tells the model "EDIT STAGED — pending approval, do not assume applied". User Accepts in UI → applies. Honest tradeoff: since the loop can't pause, later same‑turn steps won't see a staged edit — best for careful single edits.
3. **Terminal = v1 now, v2 later.** v1: xterm.js write‑only renderer fed by the existing `POST /api/shell/stream` SSE + mirror the agent's bash/python output; cwd = workspace. v2 (future): full interactive ConPTY/`pywinpty` + bidirectional channel.
4. **Single‑user trusted posture.** Auth is disabled in the desktop app, so the new in‑app file/workspace/terminal endpoints are **owner‑authed, not admin‑gated**, but still **confined to the chosen workspace** + the existing sensitive‑path denylist (`tool_execution._resolve_tool_path_in_workspace`).

## Phased implementation

> Reuse > new code. Seams confirmed with file:line below.

**Phase 0 — Scaffolding pass (serial; collision keystone).** Add inert anchors so parallel streams never edit the same region (see Agent Distribution). Create empty modules: `routes/code_workspace_routes.py`, `static/js/codeWorkspace.js`, `terminalPanel.js`, `gitPanel.js`, `fileMentionAutocomplete.js`, `src/workspace_checkpoints.py`, `src/agent_tools/code_quality_tools.py`.

**Phase 1 — Workspace backbone (backend).** `code_workspace_root` setting + `/api/workspace/current`. New `routes/code_workspace_routes.py` (wired in `app.py` next to `setup_workspace_routes()` ~`app.py:709`): `GET /api/workspace/tree` (dir tree via `LsTool`/`os.scandir`), `GET /api/workspace/file` + `POST /api/workspace/file` (reuse `ReadFileTool`/`WriteFileTool` bodies), `GET /api/workspace/search?q=` (fuzzy filename via `GlobTool`). All confined via `_resolve_tool_path_in_workspace()`.

**Phase 2 — Code Workspace panel (frontend, headline).** `static/js/codeWorkspace.js`: draggable modal via `Modals.register('code-workspace-modal',{railBtnId:'tool-code-btn',…})` + `makeWindowDraggable` (`windowDrag.js`); 3 panes — **file tree**, **code editor** (reuse `document.js` `#doc-editor-pane`: textarea + hidden hljs overlay + language dropdown), **diff viewer** (reuse `.agent-tool-diff`). Tree click → `GET /file`; Save → `POST /file`. Subscribe to the same `tool_output` SSE `diff` events chat renders → live‑refresh the open file.

**Phase 3 — Diff approval (toggle).** New `src/workspace_checkpoints.py` (journal keyed by `session_id`+path; stores old bytes to `DATA_DIR/checkpoints/`). Additive capture in `filesystem_tools.py` (`original` at `:84` [read `:72`], `old_content` at `:181` [read `:171`] - capture at the return point). Add `checkpoint_id` to the `diff` dict. New `POST /api/workspace/revert {checkpoint_id}` + `/revert_all/{session_id}`. Frontend: inject `.diff-actions` (Accept/Reject) into `diffHtml` at `chat.js:2195` and `chatRenderer.js:2098`; add a new delegated `click` branch in `chat.js` `initListeners()` (alongside existing `.copy-code`/`.run-code`/`.edit-code` delegations ~`:3606`) for `.diff-accept`/`.diff-reject`; none exists today. Add matching `.diff-actions`/`.diff-accept`/`.diff-reject` CSS in `style.css` near `.agent-tool-diff` (`:9107`). Hash‑guard Reject (disable if file no longer matches `new_hash`). Implement `strict` staging path per decision 2.

**Phase 4 — Native tools `run_tests`/`lint`/`format`.** Add‑a‑tool recipe: schemas in `tool_schemas.py FUNCTION_TOOL_SCHEMAS` + clause in `function_call_to_tool_block` (~`:1254`); handler classes in `src/agent_tools/code_quality_tools.py` (auto‑detect `pytest`/`npm test`, `ruff`/`eslint`, `black`/`prettier`; run via subprocess with `cwd=agent_cwd()` `tool_execution.py:260`); register in `agent_tools/__init__.py` (`TOOL_HANDLERS` dict at `:27`, `TOOL_TAGS` **set** at `:58` - both append-only); docs in `agent_loop.py TOOL_SECTIONS` + add to the `"files"` group set (`:284`).

**Phase 5 — Terminal v1.** `static/js/terminalPanel.js` + `#tool-terminal-btn` modal; bundle **xterm.js** (vendor into `static/lib`) as a write‑only renderer over `POST /api/shell/stream` (SSE `stream`/`data`/`exit_code`). Backend: make `_exec_shell`/`generate()` honor workspace cwd (today `Path.home()` at `shell_routes.py:439,881` (the Windows-reachable `_exec_shell` + streaming `generate()` pipe fallback)). Mirror agent bash/python output into the panel via the `tool_output` SSE stream.

**Phase 6 — Git panel.** Lightweight: `gitPanel.js` (tab in the Workspace panel) + git endpoints in `code_workspace_routes.py` shelling `git` in the workspace (`status --porcelain`, `diff`, `branch`, `add`, `commit`). Reuse `.agent-tool-diff` styles for the diff view. (Agent can already `git` via bash; this is the human UI.)

**Phase 7 — Quick wins.** (a) **Highlight tool output**: at `chat.js`/`chatRenderer.js` render sites, run output/diff `<pre>` through `hljs.highlightElement` (in scope at `chatRenderer.js:2128-2130`; note the diff `<pre class="diff-pre">` has **no `<code>` child** so the existing `pre code` selector misses it - select `pre.diff-pre` directly or wrap rows in `<code>`), language from file extension. (b) **`@filename` mentions**: `fileMentionAutocomplete.js` forked from `slashAutocomplete.js` (trigger `/(?:^|\s)@([\w.\-/]*)$/`, query `GET /api/workspace/search`, insert path) wired to composer `#message`.

**Phase 8 — Codex files API.** In `routes/codex_routes.py` add `FILES_READ_SCOPES={"files:read","files:write"}` / `FILES_WRITE_SCOPES={"files:write"}` (mirror `:24‑37`) + `GET /api/codex/files/{list,read}`, `POST /api/codex/files/write` gated by `_scope_owner`, reusing the Phase‑1 confined helpers; surface the new scopes in the token‑mint UI.

## Agent distribution (parallel build design)

**Hot‑spot shared files:** `index.html`, `style.css`, `chatRenderer.js`, `chat.js`, `agent_loop.py`, `tool_schemas.py`, `agent_tools/__init__.py`, `app.py`.

**Collision avoidance:**
1. **Phase‑0 scaffold (1 agent, serial, merged before fan‑out)** adds *inert anchors*: rail buttons + empty modals in `index.html` (each `OWNED BY:` fenced); router import/include in `app.py` against the empty module; stub tool names in `agent_tools/__init__.py` + stub schemas/clauses in `tool_schemas.py` + docs in `agent_loop.py` (return `{"error":"not implemented"}`); fenced empty CSS blocks in `style.css`; named no‑op hooks `highlightToolOutput(node)` and `_attachDiffApprovalButtons(node,diff)` in `chat.js`/`chatRenderer.js`, plus a new delegated `click` handler branch for `.diff-accept`/`.diff-reject` (none exists today - see Phase 3); all empty modules created.
2. **Per‑workstream git worktrees** off the post‑scaffold commit; each stream edits only its own region/fence/function‑body of the shared anchors.
3. **Append‑only** on shared lists (`TOOL_TAGS`, `FUNCTION_TOOL_SCHEMAS`, `DEFAULT_SETTINGS`).
4. **Integration agent (serial, last)** rebases in dependency order, replaces stubs, resolves the small fenced conflicts, runs Verification.

**Recommended: 1 scaffold + 6 parallel + 1 integration = 8 agents.**

| # | Workstream | Owns | Depends on |
|---|---|---|---|
| 0 | Scaffold (serial, first) | all Phase‑0 anchors + empty modules | — |
| 1 | Workspace backbone + file/git HTTP | `code_workspace_routes.py`, `workspace_routes.py`/`settings.py` (append) | 0 |
| 2 | Code Workspace panel UI | `codeWorkspace.js`, its CSS fence + `index.html` modal body | 0,1 |
| 3 | Diff approval (toggle + checkpoints) | `workspace_checkpoints.py`; additive `filesystem_tools.py`; revert routes; fills `_attachDiffApprovalButtons` | 0,1 |
| 4 | run_tests/lint/format | `code_quality_tools.py`; fills tool stubs | 0 |
| 5 | Terminal v1 (xterm.js) | `terminalPanel.js`, its CSS fence + modal body; `shell_routes.py` cwd | 0,1 |
| 6 | Quick wins (highlight + @‑mention) + Codex files | fills `highlightToolOutput`; `fileMentionAutocomplete.js`; `codex_routes.py` (append) | 0,1 |
| — | Integration (serial, last) | rebase, replace stubs, verify | all |

Critical path = stream 1 (others start once its read/write/tree helper signatures land). Git panel rides with stream 1 (endpoints) + stream 2 (UI tab). If more parallelism wanted, split stream 6 into 6a/6b/6c (no shared files post‑scaffold) → up to 10 agents.

## Verification (Windows, single‑user)

Launch `app.exe`; drive UI; hit endpoints with PowerShell `Invoke-RestMethod` against `http://127.0.0.1:<port>`.
- **Workspace**: `POST /api/workspace/current` accepts a folder, rejects a file/`.ssh`/`C:\`; reload persists; `/tree` & `/file` outside root rejected.
- **Panel**: open via `#tool-code-btn`; tree → click file → editor loads w/ correct language; edit+Save round‑trips; agent edit to the open file updates the panel live.
- **Diff approval**: `auto` → Accept collapses, Reject restores on‑disk content; stale‑hash Reject disabled; `/revert_all` rolls back the session. `strict` → edit doesn't touch disk until Accept; tool result warns the model.
- **Native tools**: agent "run the tests" → `run_tests` runs `pytest` with workspace cwd, streams output + exit code; same for `lint`/`format`.
- **Terminal v1**: `git status`/`dir` render with ANSI color; cwd = workspace; agent bash mirrors into the panel; disconnect stops cleanly.
- **Git panel**: status lists changes; stage+commit works; diff renders.
- **Quick wins**: `read_file` output is highlighted (live + reload); `@` → file popup → path inserted; mint token `files:read` only → `/files/read` 200, `/files/write` 403; traversal outside workspace rejected.
- **Regression**: no‑workspace agent message still uses default data/tmp allowlist; email‑confirm unaffected; existing diffs still render (now with Accept/Reject).

## Reuse map / critical files
- Panels: `static/js/modalManager.js`, `windowDrag.js`, `toolWindowZOrder.js`
- Editor + hljs: `static/js/document.js` (`#doc-editor-pane`), `static/lib/highlight.min.js`
- Autocomplete: `static/js/slashAutocomplete.js` → `fileMentionAutocomplete.js`
- Diff render: `static/js/chat.js:2154‑2207`, `chatRenderer.js:2078‑2104`
- Tools: `src/tool_schemas.py`, `src/agent_tools/__init__.py`, `src/agent_tools/subprocess_tools.py`, `src/agent_loop.py` (`TOOL_SECTIONS`, `:284`)
- Workspace/fs: `src/tool_execution.py`, `src/agent_tools/filesystem_tools.py`, `routes/workspace_routes.py`
- Terminal: `routes/shell_routes.py`, `services/shell/service.py`
- Codex: `routes/codex_routes.py` (scope pattern `:24‑37`, `_scope_owner`)
- Settings/routing: `src/settings.py` (`agent_email_confirm:39`), `app.py` (`include_router` ~`:570‑790`)
