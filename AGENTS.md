# AGENTS.md — DevSpace

Instructions for AI coding agents working in this repository.

## What this project is

**DevSpace** is a single-user **desktop AI workspace**: a Tauri (Rust) shell that
spawns a local Python/FastAPI backend and loads its vanilla-JS frontend in a
webview. It runs on the user's machine with no login, and supports local and
API LLM providers.

It is a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
(AGPL-3.0), restructured as a native Windows desktop app with an NSIS installer.

**Not multi-user. Not a server product.** uvicorn binds to `127.0.0.1`. The
Tauri shell owns backend lifecycle (Windows Job Object kill-on-close).

Deeper architecture: `docs/APPLICATION_OVERVIEW.md`. Code Workspace history:
`docs/HANDOFF.md`, `docs/code-workspace-plan.md`.

---

## Feature Surface & Capabilities

### 1. Code Workspace (Built-in IDE & Developer Surface)
- **3-Pane Modal UI**: File tree with lazy expansion & fuzzy search, Monaco 0.52.2 Code Editor (offline vendored bundle), Diff viewer, Terminal, and Git panel.
- **Language Server Protocol (LSP)**: WebSocket bridge (`/api/lsp/{lang}`) supporting Python (`pylsp`), TypeScript (`typescript-language-server`), and Rust (`rust-analyzer`) for diagnostics, hover, autocomplete, and go-to-definition.
- **Interactive Terminal**: Real PTY WebSocket (`WS /api/workspace/pty`) backed by xterm.js and `pywinpty` for interactive shell access, plus owner-authed streaming shell (`POST /api/workspace/shell`).
- **First-Class Git Integration**: Native structured tools (`git_status`, `git_diff`, `git_log`, `git_blame`, `git_commit`, `git_branch`) and in-app Git Panel for staging, unstage, and committing.
- **Native Code Quality Tools**: Auto-detected execution of test runners (`run_tests` for pytest, npm, go, cargo), linters (`lint` for ruff, eslint, flake8), and formatters (`format` for black, prettier, ruff).
- **Edit Review & Checkpoints**: Review mode (`agent_edit_review`: `"strict"` default staging vs `"auto"` keep/revert), `GET /api/workspace/checkpoints` endpoint, session rollback (`POST /api/workspace/revert_all`), and open-in-editor deep-links from chat diff cards.
- **Direct File Mentions**: `@filename` autocomplete popup in chat (`fileMentionAutocomplete.js`) searching workspace files.

### 2. Autonomous AI Agent & Execution Loop
- **Multi-Provider LLM Streaming**: Streaming SSE loop supporting Ollama, LM Studio, llama.cpp, OpenAI, Anthropic, OpenRouter, GitHub Copilot, ChatGPT subscription, Venice, Gemini, and Groq.
- **Soft Auto-Continue**: `agent_auto_continue` allowing turns beyond `agent_max_rounds` up to `agent_max_rounds_ceiling` (default 150) with status chips and TODO state re-anchoring.
- **Sub-Agent Spawning**: `spawn_agent` tool launching nested sub-agents with isolated contexts and domain presets (`explore`, `code`, `general`).
- **Agent Todo List**: `manage_todos` tool to maintain progress checklists on multi-step turns.
- **Output Summarization**: Automatic head/tail/interesting-error line preservation for large stdout/stderr outputs (>10 KB) from bash and Python executions.
- **Coding Verifier & Nudges**: Heuristic-driven completion verifier subagent for coding turns and loop-side `edit-then-verify` reminders after unverified file modifications.
- **Project Orientation**: `project_bootstrap` tool providing instant one-call project architecture, dependency, test, and instruction file detection.
- **Semantic Tool Selection**: RAG indexing over tool schemas (`src/tool_index.py`) and prompt-budget enforcement (`tests/test_agent_prompt_budget.py`).

### 3. Deep Research & Knowledge Systems
- **Deep Web Research**: Multi-step background research engine (`deep_research.py`, `/api/research`) with recursive web navigation, source extraction, and interactive browser reports.
- **Vector Document RAG**: Embedded ChromaDB persistent store for personal documents, codebase RAG, and semantic context retrieval.
- **Knowledge Base & Sources**: Knowledge Base CRUD operations and pluggable research source registry.

### 4. Cookbook & Hardware-Aware Model Management
- **Hardware Fitting (`hwfit`)**: Automatic local GPU, CPU, and RAM analysis tab ("What Fits?") recommending model sizes and quantizations.
- **HuggingFace Explorer & Downloader**: Search, download management, and local model caching.
- **Local Model Serving**: Launching and managing local inference instances via llama.cpp, vLLM, and SGLang presets.

### 5. Document Editor & Writing Suite
- **Artifact Canvas**: Writing-first document editor with inline AI edits, structural suggestions, and live Markdown/HTML/CSV preview.
- **Document Library**: Categorization by language and topic with multi-format export capabilities (PDF, Word `.docx`, HTML).

### 6. Email & Inbox Management
- **Full IMAP/SMTP Client**: Multi-account email inbox, triage tags, AI email summaries, reminders, drafts, reply composition, and Gmail OAuth integration.

### 7. Notes, Tasks & Calendar Sync
- **Google Keep-Style Notes**: Quick note-taking, checklists, and note search.
- **Task Scheduler**: Persistent background job runner (`task_scheduler.py`) and internal event bus.
- **Calendar & CalDAV**: Full calendar view with CalDAV synchronization and Gmail focus-time event writebacks.

### 8. Gallery & Creative Tools
- **Image Library**: AI image generation (`generate_image`), image editor (`edit_image`) with layer drafts, and reusable image signatures/stamps.

### 9. Compare (A/B Model Evaluation)
- **Model Arena**: Blind side-by-side model comparison with automated synthesis and comparative scoring.

### 10. Skills & Memory Engine
- **User-Editable Skills**: `SKILL.md` library with owner backfill and background nightly self-audit/auto-fix loops.
- **Persistent & Vector Memory**: Long-term fact storage, memory recall, and memory auditing.

### 11. MCP, API Tokens & External Integration Bridges
- **Model Context Protocol (MCP)**: Built-in MCP server manager and client connection handler (`/api/mcp/*`).
- **Scoped API Tokens**: External integration support via bearer tokens (`ody_...`) with fine-grained permission scopes.
- **External Bridges**: HTTP bridges for Codex (`/api/codex/*`) and downloadable Claude Code plugin bundle (`/api/claude/plugin.zip`).
- **Webhooks & Companion Devices**: Custom incoming/outgoing webhooks and companion device pairing endpoints.

---

## Layout

| Path | Role |
|---|---|
| `src-tauri/` | Tauri shell — spawns uvicorn, owns window + backend lifecycle |
| `backend/` | FastAPI app + vanilla-JS UI (`static/`) forked from Odysseus |
| `backend/src/` | Core domain: agent loop, tools, settings, research, RAG |
| `backend/routes/` | HTTP routers (~60+) |
| `backend/static/js/` | ES-module frontend (no React/Vue build step) |
| `dist/` | Splash shown while the backend starts |
| `scripts/` | Installer + resource sync PowerShell scripts |
| `docs/` | Architecture notes and handoffs |

Runtime data (`backend/data/`), secrets (`.env`), and bundled resources
(`src-tauri/resources/`) are **gitignored** — never commit them.

---

## Version sources (keep in sync)

Bump **all** of these together on a release:

- `package.json` → `version`
- `src-tauri/tauri.conf.json` → `version`
- `src-tauri/Cargo.toml` → `[package].version` (Cargo.lock updates on build)
- `backend/src/constants.py` → `APP_VERSION` (served by `GET /api/version`)

Installer artifact name comes from Tauri: `DevSpace_<version>_x64-setup.exe`.

---

## Dev commands

### Run the desktop app

```powershell
# from repo root — needs Rust, Node, Tauri CLI v2, Python venv with backend deps
tauri dev
```

Overrides: `DEVSPACE_PYTHON`, `DEVSPACE_BACKEND_DIR`, `DEVSPACE_PORT`.

### Backend only (no Tauri)

```powershell
$env:AUTH_ENABLED = "false"
$env:PYTHONPATH = "D:\projects\DevSpace\backend"
# use the project venv python
python -m uvicorn app:app --port 8123 --host 127.0.0.1
```

### Tests

```powershell
cd backend
python -m pytest tests/ -q
# Prefer targeted slices while iterating, e.g.:
python -m pytest tests/test_welcome_screen_ui.py tests/test_edit_file.py -q
```

### Installer (Windows NSIS)

```powershell
# full pipeline: uv venv + pip install + mirror backend + tauri build
npm run installer

# skip slow dep reinstall when python resources are already current
npm run installer:skip-deps

# stage resources only (no tauri build)
npm run installer:resources-only
npm run bundle:sync
```

Output: `src-tauri/target/release/bundle/nsis/DevSpace_*_x64-setup.exe`.

Requires on PATH: `uv`, `cargo`, Tauri CLI (`tauri` or `cargo-tauri`).

---

## Coding conventions

### Backend (Python)

- Prefer existing patterns in `backend/src/` and `backend/routes/` over new frameworks.
- Path confinement is security-critical: use workspace resolve helpers
  (`_resolve_tool_path_in_workspace` and related) — never raw open paths from the model.
- Agent tools live in `backend/src/agent_tools/`; schemas in `tool_schemas.py`;
  loop policy in `agent_loop.py` / `agent_harness.py`.
- Settings defaults: `backend/src/settings.py` (`DEFAULT_SETTINGS`).
- Auth is still wired for token bridges, but desktop mode runs with
  `AUTH_ENABLED=false` for the local owner.

### Frontend (vanilla JS)

- ES modules under `backend/static/js/` — no bundler for the main UI.
- Wire new modules with `<script type="module">` or import from an existing module.
- Prefer semantic buttons, ARIA where interactive, and existing CSS variables
  (`--accent-primary`, `--red`, theme tokens). There is no global `--accent`
  alone in all themes — use `var(--accent, var(--red))` when needed.
- Welcome screen: `static/js/welcomeScreen.js` owns configured/unconfigured
  state; `models.js` calls `setWelcomeModelState`.

### Tauri / Rust

- Shell logic is concentrated in `src-tauri/src/lib.rs`.
- Bundle resources map is in `tauri.conf.json` (`python`, `backend`, `fastembed_cache`).
- Do not commit `src-tauri/resources/` or `src-tauri/target/`.

### Agent mode behavior (product, not just this repo)

When changing agent tools or prompts:

- Default is action-biased: edits should not be blocked by a hard verify gate
  unless `agent_edit_verify_enforce` is on.
- Sensitive paths (`.ssh`, `.gnupg`, keys, etc.) stay denied even when HOME is
  in the workspace allowlist.
- Prefer first-class tools (`git_status`, `run_tests`, …) over raw `bash` for
  common operations so output stays structured and confined.

---

## Safety / do not

- Do not commit `.env`, API keys, PATs, cookies, or `backend/data/`.
- Do not force-push `main` or rewrite published release tags without explicit ask.
- Do not add multi-user / public-bind server assumptions to the desktop path.
- Do not bundle secrets into the installer resources tree.
- Skip local agent scratch (`.codex/`, `.claude/`) and one-off patch scripts
  unless the user asks to keep them.

---

## Where to look first

| Task | Start here |
|---|---|
| Overall architecture | `docs/APPLICATION_OVERVIEW.md` |
| Code Workspace UI/backend | `docs/HANDOFF.md`, `static/js/codeWorkspace.js`, `routes/code_workspace_routes.py` |
| Agent loop / tools | `backend/src/agent_loop.py`, `backend/src/agent_tools/` |
| Desktop lifecycle | `src-tauri/src/lib.rs` |
| Installer pipeline | `scripts/build_installer.ps1` |
| Release notes pattern | GitHub releases + `RELEASE-v1.1.1.md` (historical packaging notes) |

---

## Bias to action

Read only what you need, make the smallest correct change, and verify with a
targeted test or a quick manual path (`tauri dev` / pytest slice). Prefer
updating existing modules over inventing parallel systems.

