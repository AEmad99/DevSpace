# DevSpace — Application Overview

> **Audience:** AI agents, developers, or reviewers picking up the project.
> **Purpose:** single-file orientation — what DevSpace is, how it is wired
> together, what has been built, what is in flight, and what to read next.

---

## 1. What DevSpace is

**DevSpace** is a single-user **desktop AI workspace**: a Tauri (Rust) shell
wrapping a local **Python/FastAPI** backend, with a vanilla-JS ES-module frontend
served by that backend. It runs entirely on the user's machine — no login, no
remote service required — and supports both local (Ollama, LM Studio, llama.cpp,
etc.) and API LLM providers (OpenAI, Anthropic, OpenRouter, GitHub Copilot,
ChatGPT subscription, Venice, …).

It is a fork of **[Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)**
(AGPL-3.0), restructured from a Docker-deployable web app into a native
desktop application (`tauri build` → NSIS `.exe`/MSI installer).

### Feature surface (as of v1.0.0, post 2026-06-20 coding-agent pass)

- **Chat + Agents** — local/API models, streamed SSE, full tool surface (files,
  shell, Python, web search/fetch, MCP, skills, memory, sub-agents, todos,
  auto-continue).
- **Deep Research** — multi-step web research with source reading and report
  generation; visual reports open in the system browser.
- **Cookbook** — hardware-aware model recommendations, downloads, serving
  (llama.cpp / vLLM / SGLang profiles), What-Fits? tab, model scan.
- **Documents** — writing-first editor with AI edits, suggestions, Markdown,
  HTML, CSV, and syntax highlighting. Library with language facets.
- **Email** — IMAP/SMTP inbox with triage, tags, summaries, reminders, drafts,
  reply, OAuth (Gmail).
- **Notes, Tasks, Calendar** — reminders, todos, scheduled agent tasks,
  CalDAV sync, Gmail focus-time-driven calendar writes.
- **Gallery / Image editor** — image library with editor, signatures, drafts.
- **Compare** — blind side-by-side model A/B testing with synthesis.
- **Skills** — user-editable SKILL.md library, nightly self-audit/auto-fix.
- **API tokens** — external integration with scoped bearer tokens
  (`ody_…`); Codex + Claude Code bridges.
- **Code Workspace (built-in)** — see §6.

### What it explicitly is NOT

- **Not multi-user.** The desktop build uses the single local owner; the
  in-app auth middleware is still wired in (so a token-based external bridge
  works), but a multi-user deployment is not a supported posture.
- **Not a server.** uvicorn binds to `127.0.0.1`. The Tauri shell terminates
  the backend tree on exit (Windows Job Object, see §4).
- **Not a build of Odysseus.** Odysseus is a Docker/web product; DevSpace is
  a Tauri/desktop fork that shares most of its code. (`odysseus-ref/` is a
  reference clone of the upstream repo kept for `diff`/`venv` reuse — not
  part of the build.)

---

## 2. Repository layout

```
DevSpace/
├── README.md                    ← top-level intro, run/build commands
├── package.json                 ← root npm metadata (only `playwright-core`
│                                  as a dev dep; this is not a Node app)
├── LICENSE                      ← AGPL-3.0 (inherited from Odysseus)
├── .gitignore                   ← excludes node_modules, venv, backend/data,
│                                  src-tauri/target, src-tauri/resources,
│                                  odysseus-ref, .env, *.db, *.log
│
├── src-tauri/                   ← Rust/Tauri desktop shell
│   ├── Cargo.toml               ← tauri 2.11.2 + plugins: log, dialog,
│   │                              opener, single-instance, notification;
│   │                              windows-sys for Win32 Job Objects
│   ├── tauri.conf.json          ← window 1280×832, productName "DevSpace",
│   │                              bundle targets ["nsis"],
│   │                              bundle.resources maps python/, backend/,
│   │                              fastembed_cache/ to the install tree
│   ├── capabilities/default.json ← scoped permissions for loopback origin
│   ├── icons/                   ← all Windows + iOS + Android icon sizes
│   ├── resources/               ← bundled self-contained runtime (NOT in
│   │   git): relocatable CPython, backend tree, FastEmbed model cache
│   └── src/
│       ├── main.rs              ← entry; #![cfg_attr(... windows_subsystem)]
│       └── lib.rs               ← run() — spawns uvicorn, polls for
│                                  readiness, navigates the webview, owns
│                                  backend lifecycle (Windows kill-on-close
│                                  Job Object + PID file sweep)
│
├── backend/                     ← Python/FastAPI backend (forked Odysseus)
│   ├── app.py                   ← slim orchestrator (~1,200 lines): lifespan,
│   │                              CORS, gzip, security headers, request
│   │                              timeout middleware, AuthMiddleware, static
│   │                              mount, ALL router includes (~60+ routers),
│   │                              startup/shutdown hooks
│   ├── pyproject.toml / requirements.txt
│   ├── .env(.example)           ← config (LLM hosts, SearXNG, APP_PORT, …)
│   ├── core/                    ← cross-cutting singletons
│   │   ├── constants.py         ← shim → src/constants.py
│   │   ├── database.py          ← SQLAlchemy + SessionLocal (~108 KB)
│   │   ├── auth.py              ← AuthManager (bcrypt + 7-day cookie session)
│   │   ├── middleware.py        ← SecurityHeadersMiddleware, internal-tool
│   │   │                         bypass header constants
│   │   ├── session_manager.py   ← conversation persistence (JSON + DB rows)
│   │   ├── platform_compat.py   ← Windows/macOS/Linux shims
│   │   └── pty_session.py      ← cross-platform PTY wrapper
│   │
│   ├── routes/                  ← FastAPI routers (~60 files)
│   │   ├── auth_routes.py       ← /api/auth/* (setup, login, logout)
│   │   ├── chat_routes.py       ← /api/chat (SSE streaming, tool dispatch)
│   │   ├── chat_helpers.py
│   │   ├── research_routes.py   ← /api/research (background jobs + reports)
│   │   ├── model_routes.py      ← /api/model/* (discovery, probe, download)
│   │   ├── code_workspace_routes.py ← /api/workspace/* + /api/workspace/pty
│   │   ├── codex_routes.py      ← /api/codex/* (files/cookbook/memory/etc.)
│   │   ├── shell_routes.py      ← /api/shell/stream (admin-gated streaming)
│   │   ├── mcp_routes.py        ← /api/mcp/*
│   │   ├── memory_routes.py, skills_routes.py, note_routes.py,
│   │   ├── calendar_routes.py, email_routes.py, contact_routes.py,
│   │   ├── document_routes.py, gallery_routes.py, signature_routes.py,
│   │   ├── task_routes.py (incl. webhook bridge), compare_routes.py,
│   │   ├── api_token_routes.py, webhook_routes.py, vault_routes.py,
│   │   ├── backup_routes.py, prefs_routes.py, preset_routes.py,
│   │   ├── mcp_routes.py, admin_wipe_routes.py, cleanup_routes.py,
│   │   ├── diagnostics_routes.py, embedding_routes.py, hwfit_routes.py,
│   │   ├── font_routes.py, emoji_routes.py, upload_routes.py, …
│   │
│   ├── src/                     ← the bulk of the actual logic (~100 files)
│   │   ├── agent_loop.py        ← streaming SSE agent loop (~218 KB);
│   │   │                          TOOL_SECTIONS, _DOMAIN_TOOL_MAP,
│   │   │                          auto-continue, sub-agents, plan mode
│   │   ├── llm_core.py          ← multi-provider LLM client (Anthropic /
│   │   │                          OpenAI-compat / Ollama / etc.) + streaming
│   │   ├── tool_schemas.py      ← FUNCTION_TOOL_SCHEMAS + native→text converter
│   │   ├── tool_execution.py    ← execute_tool_block; vet_workspace();
│   │   │                          _resolve_tool_path_in_workspace();
│   │   │                          _active_workspace + _active_session_id
│   │   │                          ContextVars; format_tool_result
│   │   ├── tool_implementations.py ← every "do_*" function (~202 KB)
│   │   ├── tool_index.py        ← RAG over tool schemas (semantic selection)
│   │   ├── tool_policy.py
│   │   ├── tool_parsing.py
│   │   ├── tool_security.py, prompt_security.py, url_safety.py
│   │   ├── agent_tools/         ← Tool classes (one per tool, plus
│   │   │   │                      composite tool_kit functions)
│   │   │   ├── __init__.py      ← TOOL_HANDLERS dict + TOOL_TAGS set
│   │   │   ├── filesystem_tools.py ← ReadFile, WriteFile, EditFile, Ls, Glob,
│   │   │   │                       Grep, GetWorkspace; with checkpoint
│   │   │   │                       capture/stage wiring
│   │   │   ├── subprocess_tools.py ← Bash, Python (with streaming + #!bg)
│   │   │   ├── web_tools.py     ← WebSearch, WebFetch
│   │   │   ├── document_tools.py, model_interaction_tools.py,
│   │   │   └── code_quality_tools.py ← RunTests, Lint, Format (auto-detect)
│   │   ├── workspace_checkpoints.py ← journal of file content for diff approval
│   │   ├── settings.py          ← DEFAULT_SETTINGS; load/save; per-user
│   │   │                          override layer; features.json
│   │   ├── constants.py         ← APP_VERSION, DATA_DIR, paths, FASTEMBED
│   │   ├── runtime_paths.py     ← get_app_root / get_default_data_dir
│   │   ├── chat_handler.py, chat_processor.py, chat_helpers.py
│   │   ├── mcp_manager.py, mcp_oauth.py, builtin_mcp.py
│   │   ├── memory.py, memory_vector.py, memory_provider.py
│   │   ├── rag_manager.py, rag_vector.py, rag_singleton.py
│   │   ├── chroma_client.py     ← embedded PersistentClient by default
│   │   ├── deep_research.py, research_handler.py
│   │   ├── document_processor.py, document_actions.py
│   │   ├── caldav_sync.py, caldav_writeback.py
│   │   ├── email_*.py, contacts.py
│   │   ├── model_*.py, model_discovery.py, model_media.py
│   │   ├── service_health.py, app_initializer.py
│   │   ├── task_scheduler.py, event_bus.py, bg_jobs.py, bg_monitor.py
│   │   ├── webhook_manager.py, api_key_manager.py
│   │   ├── ai_interaction.py    ← debates / pipelines / teacher / UI control
│   │   ├── teacher_escalation.py, context_compactor.py, context_budget.py
│   │   ├── llm_core.py, prompt_security.py, url_security.py
│   │   ├── upload_handler.py, upload_limits.py
│   │   ├── secret_storage.py, vault_routes support
│   │   ├── readiness.py, service_health.py, tls_overrides.py
│   │   ├── tts_*, stt_*, voice support, youtube_handler.py
│   │   └── … (≈100 files total)
│   │
│   ├── services/                ← long-running/background services
│   │   ├── docs/, faces/, hwfit/, memory/, research/, search/, shell/,
│   │   ├── stt/, tts/, youtube/
│   │
│   ├── integrations/            ← external integrations
│   │   ├── claude/              ← Claude Code plugin zip + SKILL.md
│   │   └── codex/               ← Codex plugin zip + SKILL.md
│   │
│   ├── static/                  ← frontend (vanilla JS, ES modules, no build)
│   │   ├── index.html           ← ~2,300 lines; modal containers, rail
│   │   │                          buttons (incl. `rail-code`, `rail-terminal`),
│   │   │                          <script type="module"> tags
│   │   ├── login.html           ← first-run setup screen
│   │   ├── style.css            ← ~1.26 MB single-file CSS (every modal,
│   │   │                          theme variant, page — high debt, tracked)
│   │   ├── app.js               ← legacy glue
│   │   ├── sw.js                ← service worker (precache + cache strategies;
│   │   │                          CACHE_NAME = 'odysseus-v335')
│   │   ├── manifest.json
│   │   ├── js/                  ← ~80 ES-module files
│   │   │   ├── chat.js          ← main chat flow (~264 KB); initListeners,
│   │   │   │                      diff approval delegation, tool-output
│   │   │   │                      rendering, agent_continue chip
│   │   │   ├── chatRenderer.js  ← history replay (~127 KB)
│   │   │   ├── toolOutputHooks.js ← highlightToolOutput + diff Accept/Reject
│   │   │   │                       injection (shared between chat.js /
│   │   │   │                       chatRenderer.js)
│   │   │   ├── codeWorkspace.js ← Code Workspace panel — file tree +
│   │   │   │                       code editor + diff viewer + Git tabs
│   │   │   ├── terminalPanel.js ← xterm.js write-only renderer + WS to
│   │   │   │                       /api/workspace/pty (real PTY)
│   │   │   ├── gitPanel.js      ← Git tab (status/stage/commit)
│   │   │   ├── fileMentionAutocomplete.js ← `@filename` popup
│   │   │   ├── modelPicker.js, models.js, providers.js, providerDeviceFlow.js
│   │   │   ├── sessions.js, memory.js, skills.js
│   │   │   ├── document.js, documentLibrary.js
│   │   │   ├── emailLibrary.js, emailInbox.js
│   │   │   ├── calendar.js, notes.js, tasks.js
│   │   │   ├── gallery.js, galleryEditor.js, group.js
│   │   │   ├── settings.js, admin.js, presets.js, themes.js
│   │   │   ├── modalManager.js, windowDrag.js, windowResize.js,
│   │   │   ├── modalSnap.js, tileManager.js, escMenuStack.js
│   │   │   ├── nativeDialog.js  ← `__TAURI__.dialog` folder picker bridge
│   │   │   ├── desktopNotifications.js ← native OS notification bridge
│   │   │   ├── codeRunner.js, slashCommands.js, slashAutocomplete.js
│   │   │   ├── search-chat.js, search.js
│   │   │   ├── compare/index.js, compare/…
│   │   │   ├── cookbook.js, cookbook-*.js
│   │   │   ├── highlight via /static/lib/highlight.min.js (hljs)
│   │   │   └── … (~80 modules total)
│   │   ├── lib/                 ← vendored browser libs
│   │   │   ├── highlight.min.js
│   │   │   ├── xterm.js + xterm-addon-fit.js + xterm.css (Phase 5)
│   │   │   ├── docx, mammoth, html2pdf, xlsx, qrcode
│   │   ├── icons/, fonts/
│   │
│   ├── companion/               ← companion device pairing routes
│   ├── config/searxng/          ← SearXNG instance config
│   ├── tests/                   ← ~600 pytest files (huge coverage)
│   ├── docs/                    ← backend-specific docs (agent-migration,
│   │                              backup-restore, email-outlook, etc.)
│   ├── data/                    ← runtime state (gitignored): app.db,
│   │   │                          sessions.json, settings.json, auth.json,
│   │   │                          chroma/, generated_images/, fastembed_cache/,
│   │   │                          uploads/, logs/, etc.
│   ├── logs/, mcp_servers/, scripts/, licenses/, specs/
│
├── dist/                        ← local splash HTML shown while the backend
│   │                              starts (foreground logo, spinner,
│   │                              backend-status listener)
│
├── design/                      ← icon/source assets + design previews
│   ├── devspace-icon.svg
│   ├── bubble-preview.html, mark-options.html
│   └── cw-review/               ← Phase-2 Code Workspace before/after PNGs
│
├── docs/                        ← project-level docs (THIS directory)
│   ├── APPLICATION_OVERVIEW.md  ← you are here
│   ├── HANDOFF.md               ← Phases 0–8 narrative for code-workspace
│   ├── code-workspace-plan.md   ← the full 8-phase plan with line refs
│   └── phase2-decisions.md      ← desktop-ify decisions (Chroma, notifs, …)
│
├── odysseus-ref/                ← upstream Odysseus clone (NOT part of build);
│   │                              reused only for `venv/` Python deps via
│   │                              `tauri dev` fallback path
│
└── src-tauri/target/            ← cargo build output (gitignored)
```

---

## 3. How it runs (dev and prod)

### Cold start, end-to-end

```
[user launches app.exe / `tauri dev`]
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│ src-tauri/src/lib.rs :: run()                               │
│                                                            │
│  1. tauri_plugin_single_instance  (focus existing window   │
│     if a second launch is attempted)                       │
│  2. plugins: dialog, opener, notification, log (debug only) │
│  3. manage(BackendProcess)                                 │
│  4. setup():                                               │
│       sweep_stale_backend() — kill any orphan tree from    │
│         a previous unclean exit (Windows-only PID walk     │
│         guarded by PID-reuse + exe-name check)             │
│       pick_free_port() OR honor DEVSPACE_PORT               │
│       spawn_backend() — start uvicorn with AUTH_ENABLED=   │
│         true, PYTHONUNBUFFERED=1, ODYSSEUS_DATA_DIR=<app-  │
│         data>/data, FASTEMBED_CACHE_PATH=<bundled cache>   │
│         (CREATE_NO_WINDOW on Windows)                      │
│       write backend.pid to %TEMP%/devspace_backend.pid     │
│       assign_to_kill_on_close_job() — Windows Job Object   │
│         with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so the     │
│         whole backend tree dies with us                    │
│       spawn a thread that:                                 │
│         wait_for_backend() — poll 127.0.0.1:port for 120 s │
│         window.navigate(http://127.0.0.1:<port>/)          │
│  5. RunEvent::Exit: child.kill() + remove pidfile          │
│                                                            │
│  Until the navigate succeeds, the webview shows            │
│  dist/index.html (local splash; spinner; backend-status    │
│  listener updates the message text).                      │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│ backend/app.py  (uvicorn, 127.0.0.1:<free port>)           │
│                                                            │
│  lifespan → _startup_event:                                │
│   • purge incognito sessions from prior process            │
│   • spawn upload cleanup task                              │
│   • start_bg_monitor (auto-continues agent when #!bg ends) │
│   • register_builtin_servers(MCP) + connect_all_enabled()  │
│   • pre-warm tool_index (semantic tool selection)          │
│   • keepalive_loop (60s endpoint pings)                    │
│   • _ensure_default_tasks (per-user task reconciliation)   │
│   • skills_manager.backfill_owner (assigns orphan SKILL.md)│
│   • task_scheduler.start()                                 │
│   • null_owner_sweep_loop (hourly; assign legacy owner)    │
│   • skill_audit_nightly_loop (auto-judge / auto-fix)       │
│   • cookbook_serve_lifecycle_loop (kill expired serves)    │
│                                                            │
│  Request lifecycle:                                        │
│   SecurityHeadersMiddleware                                │
│     → _RequestTimeoutMiddleware (45 s default; whitelists  │
│       /api/chat, /api/shell/stream, /api/research,          │
│       /api/model/download, /api/model/probe,               │
│       /api/model-endpoints, /api/cookbook/setup,            │
│       /api/upload, /api/image, /api/memory/audit)          │
│     → GZipMiddleware                                       │
│     → CORSMiddleware                                       │
│     → AuthMiddleware (if AUTH_ENABLED)                     │
│       • CORS preflight bypass                               │
│       • /api/auth/*, /static/*, /api/health, /api/version  │
│         bypass                                              │
│       • /api/tasks/<id>/webhook/<token> regex bypass       │
│       • /api/research/report/<token> regex bypass          │
│       • X-Odysseus-Internal-Token (loopback only) bypass    │
│       • Bearer API token (prefix-cache + bcrypt verify)    │
│       • Session cookie (7-day)                            │
│       • Loopback bypass ONLY with no proxy forward headers│
│                                                            │
│  Routers mounted (in order — see app.py:570–820):         │
│   auth, upload, emoji, sessions, admin_wipe, memory,      │
│   skills, chat, research, history, search, presets,       │
│   diagnostics, cleanup, personal_docs, embeddings, models, │
│   copilot, chatgpt_sub, tts, stt, documents, signatures,  │
│   gallery, editor_drafts, tasks, assistant, calendar,     │
│   shell, cookbook, workspace, code_workspace, hwfit,       │
│   compare, prefs, backup, fonts, mcp, ai_interaction,      │
│   webhooks, api_tokens, notes, email, codex, claude,      │
│   vault, contacts, companion.                              │
│                                                            │
│  Static served from /static (cache-control: no-cache on    │
│  .js/.css/.html to defeat browser caching; JS MIME forced │
│  in app.py register_static_mime_types).                   │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│ Frontend (served from the same loopback origin)            │
│                                                            │
│  index.html <script type="module"> chain:                  │
│   storage → ui → markdown → dragSort → sessions → memory → │
│   skills → tourHints → fileHandler → voiceRecorder →       │
│   models → rag → presets → search → spinner → tts-ai →     │
│   document → gallery → chatRenderer → toolOutputHooks →    │
│   codeRunner → chatStream → codeWorkspace → nativeDialog → │
│   terminalPanel → chat → cookbook → search-chat → compare │
│   → theme → censor → settings → admin → init →            │
│   slashCommands → emailInbox → emailLibrary/{utils,        │
│   signatureFold, state} → notes → tasks → calendar/{utils, │
│   reminders} → group → keyboard-shortcuts → sidebar-layout │
│   → section-management                                    │
│   …plus highlight.min.js (hljs) and xterm.js (lazy)        │
└────────────────────────────────────────────────────────────┘
```

### Build / install (`tauri build`)

```
tauri build
  ↓
src-tauri/target/release/bundle/
  ├── nsis/DevSpace_1.0.0_x64-setup.exe       ← Windows NSIS installer
  └── msi/DevSpace_1.0.0_x64_en-US.msi        ← Windows MSI
```

The installer is **"this-machine"**: it expects the bundled `resources/`
(`python/`, `backend/`, `fastembed_cache/`) to live next to the binary.
Path overrides for testing: `DEVSPACE_PYTHON`, `DEVSPACE_BACKEND_DIR`,
`DEVSPACE_PORT`. See `docs/phase2-decisions.md` — fully-portable Python
runtime bundling is deferred future work.

### Backend resolution order (`backend_paths()` in `lib.rs`)

1. `DEVSPACE_PYTHON` / `DEVSPACE_BACKEND_DIR` env vars (testing)
2. Bundled resources (`<resources>/python/python.exe`, `<resources>/backend/app.py`)
   — probes both `python/` and `resources/python/` layouts since Tauri
   `bundle.resources` mapping can land either way
3. Dev fallback: `../backend/` + `../odysseus-ref/venv/Scripts/python.exe`

### `ODYSSEUS_DATA_DIR`

Backend resolves the data dir in this order:

1. `ODYSSEUS_DATA_DIR` env var (Tauri shell sets it to
   `<app_data_dir>/data` on every launch)
2. `get_default_data_dir()` from `src/runtime_paths.py`

This is the ONLY writable state across reinstalls — everything in
`backend/data/` (DB, settings, sessions, Chroma index, uploads, logs,
generated images, embeddings cache) lives here.

---

## 4. Backend boot, layout, and routing (annotated)

### `app.py` (slim orchestrator)

| Lines | What |
|---|---|
| `:1-50` | Module docstring, MIME registration, HF symlink-disable on Windows, dotenv with `utf-8-sig` (tolerates BOM) |
| `:52-65` | Logging setup — rotating file handler (5 MB × 3 backups) under `DATA_DIR/logs` |
| `:67-76` | `app = FastAPI(...)` |
| `:78-99` | CORS allowlist (loopback by default); CSP set to null (relaxed for the desktop shell) |
| `:101-108` | `GZipMiddleware(minimum_size=1024, compresslevel=6)` — text/event-stream excluded automatically |
| `:111-114` | `SecurityHeadersMiddleware` (CSP nonce, X-Frame-Options, etc.) |
| `:117-150` | `_RequestTimeoutMiddleware` — 45 s hard cap on non-streaming/non-research paths |
| `:152-355` | `AuthMiddleware` (only when `AUTH_ENABLED=true`). Cookie session + API bearer tokens (cached, prefix-keyed) + loopback bypass + X-Odysseus-Internal-Token. Proxy-header rejection (`cf-connecting-ip`, `x-forwarded-for`, etc.) prevents Cloudflare tunnel abuse of `LOCALHOST_BYPASS`. |
| `:357-373` | `_RevalidatingStatic` (sets `Cache-Control: no-cache` on `.js/.css/.html`) |
| `:375-417` | `/api/generated-image/{filename}` — gallery ownership check; immutable cache |
| `:419-428` | `init_youtube()` |
| `:430-439` | RAG init (lazy, Chroma embedded via `PersistentClient` under `backend/data/chroma`) |
| `:441-487` | Component wiring: `session_manager`, `memory_manager`, `memory_vector`, `upload_handler`, `personal_docs_manager`, `api_key_manager`, `preset_manager`, `chat_processor`, `research_handler`, `chat_handler`, `model_discovery`, `skills_manager`, `tts_service` |
| `:489-502` | Exception handlers (`SessionNotFoundError`, `InvalidFileUploadError`, `LLMServiceError`, `WebSearchError`) |
| `:506-507` | WebhookManager init |
| `:509-820` | **~60 `app.include_router(...)` calls in order** — auth, upload, emoji, sessions, admin_wipe, memory, skills, chat, research, history, search, presets, diagnostics, cleanup, personal_docs, embedding, models, copilot, chatgpt_sub, tts, stt, documents, signatures, gallery, editor_drafts, task_scheduler, assistant, calendar, shell, cookbook, workspace, **code_workspace**, hwfit, compare, prefs, backup, fonts, mcp, ai_interaction, webhooks, api_tokens, notes, email, codex, claude, vault, contacts, companion |
| `:822-895` | SPA route handlers (`/`, `/notes`, `/calendar`, `/cookbook`, `/email`, `/memory`, `/gallery`, `/tasks`, `/library`, `/backgrounds`, `/login`) — every route is the same SPA shell with a window.location-based deep-link |
| `:897-916` | `/api/version`, `/api/health`, `/api/ready` (readiness self-check), `/api/runtime` (Docker detection + Ollama URL) |
| `:918-1180` | `_lifespan` → `_startup_event` (purges, MCP, tool-index warmup, keepalive, defaults reconciliation, skill backfill, scheduler, sweep loops, cookbook serve loop) and `_shutdown_event` (cancel tasks, disconnect MCP, close webhook manager) |
| `:1183-1187` | `if __name__ == "__main__"` → `uvicorn.run(app, host=APP_BIND, port=APP_PORT)` |

### Auth posture (desktop)

- `AUTH_ENABLED=true` is the default (Tauri shell sets it).
- The first launch shows the SPA at `/login` → backend `auth.setup` flow →
  creates the single local owner (admin). Subsequent launches reuse the
  7-day session cookie (WebView2 persists it).
- All `/api/workspace/*` endpoints are **owner-authed, NOT admin-gated**
  (per plan decision 4 — single-user trusted posture). They use
  `get_current_user(request)`, not `require_admin`.
- The `/api/shell/stream` admin-gated path 403s in single-user mode
  because the AuthMiddleware isn't installed when `AUTH_ENABLED=false`,
  but in desktop mode it IS installed and the local owner IS admin —
  still, the Code Workspace terminal uses the dedicated owner-authed
  `POST /api/workspace/shell` and `WS /api/workspace/pty` to avoid
  surprising future changes.

### Path confinement

Every workspace-scoped endpoint goes through
`_resolve_tool_path_in_workspace(root, path)` in `src/tool_execution.py`,
which:

1. Resolves symlinks (`os.path.realpath`)
2. Applies the **sensitive-path denylist** (`.ssh`, `.gnupg`, `id_rsa`, …)
3. Verifies containment inside the chosen root via `os.path.commonpath`
   (case-insensitive on Windows via `os.path.normcase`)

The Code Workspace root is vetted by `vet_workspace()` at set-time AND
re-vetted on every read so a deletion or symlink-swap can't smuggle a
path past the gate.

### Active workspace (agent)

For the **agent** (per-turn), `_active_workspace: ContextVar` is bound in
`execute_tool_block` and read by `_resolve_tool_path`, `_resolve_search_root`,
and `agent_cwd()`. It's sourced from the chat form field, not the
`code_workspace_root` setting — that's the UI's source of truth. They are
three different readers of three different inputs, all converging on the
same `_resolve_tool_path_in_workspace()` check.

`code_workspace_root` (the UI's setting) is sourced only by the in-app
file/git/terminal endpoints and by `code_quality_tools` (which call
`agent_cwd()` → falls back to the setting if the per-turn form field is
empty).

---

## 5. Agent architecture

### Tool dispatch

```
agent_loop.py  run_agent_turn(...)
  │
  ▼
stream_llm_with_fallback(messages, candidates, …)
  │   ← src/llm_core.py — multi-provider SSE streaming with
  │      endpoint fallback chain (default_model_fallbacks,
  │      utility_model_fallbacks)
  │
  ▼  for each chunk: parse tool calls (native function calls + legacy
  │  ```tool``` blocks via tool_parsing.py)
  │
  ▼
execute_tool_block(block, ctx) — tool_execution.py
  │   • bound check (TOOL_TAGS in src/agent_tools/__init__.py)
  │   • tool_policy / plan_mode filtering
  │   • owner scoping
  │   • workspace + session_id binding (ContextVar)
  │   • calls TOOL_HANDLERS[name](content, ctx)
  │
  ▼
do_<name>() in tool_implementations.py  OR
BashTool/PythonTool/EditFileTool/... in src/agent_tools/
  │
  ▼
result streamed back as {type: "tool_output", output, exit_code, diff?}
```

### Auto-continue

`agent_max_rounds` is a soft checkpoint by default
(`agent_auto_continue=true`). Crossing it while the agent is still
working emits `agent_continue` markers and re-states the open TODO list
to keep the model on-rails. The hard ceiling is
`agent_max_rounds_ceiling` (default 150). The UI drops a fading
"Still working — continuing automatically…" chip on each marker
(`chat.js`).

### Sub-agents

`spawn_agent` tool — runs a nested `run_agent_turn` with its own context
and `relevant_tools` set (`explore` / `code` / `general`). Hard-capped at
depth 1 — sub-agents cannot spawn sub-agents.

### Edit approval (toggle)

`agent_edit_review` setting (`"strict"` default | `"auto"`):

- **strict** (opencode-style, default for fresh installs) — edit is NOT
  written; both old + new content staged in
  `data/checkpoints/<id>.{json,old,new}`. UI shows Apply / Discard. Tool
  result warns the model: `EDIT STAGED — ... has NOT been applied`. The
  `edit_pending` SSE event (`{count, files}`) drives an input-bar
  "N edits pending" chip (`chat.js` + `index.html` + `style.css`) that
  stays current via `GET /api/workspace/checkpoints` on session
  change + after every Apply / Discard click. Each diff card also
  carries an "Open in editor" button that deep-links straight to the
  changed file in the Code Workspace modal (`toolOutputHooks.js` +
  `codeWorkspace.js` `openCodeWorkspaceAt(path)`).
- **auto** — edit is written; old content captured in
  `data/checkpoints/<id>.{json,old}`. UI shows Keep / Revert buttons.
  Revert stale-guarded by hash of the post-edit file. Still the
  right choice for one-off agents you want to fly through edits
  without per-step prompts.

Routes: `POST /api/workspace/{revert,apply,discard}` and
`POST /api/workspace/revert_all/{session_id}` (rolls the entire session
back to its pre-session state). Enumeration: `GET /api/workspace/
checkpoints?session_id=...&include_resolved=1` (used by the chip +
the "View session changes" drawer on the assistant message).

Existing installs that set `agent_edit_review: "auto"` in
`data/settings.json` keep their saved value; only fresh installs get
the new strict default. Backward-compatible by design.

### Edit-then-verify nudge

Soft loop-side check in `stream_agent_loop`: counts consecutive
`edit_file` / `write_file` calls since the last `run_tests` / `lint` /
`format`. After 2 consecutive edits without verifying, injects a
one-time system reminder ("run the project's tests / linter now, or
say why not"). Capped at one nudge per turn — would false-positive on
trivial one-line edits. The setting override is the same as the
verifier below.

### Output summarization (bash / python)

Long outputs (>10K chars) are run through `src/output_summarizer.py`
before truncation. The summarizer keeps:
- first 30 lines (early context: command echoes, banners, test discovery)
- last 60 lines (the final result / actual error / exit status)
- any "interesting" line in the middle (error / exception / traceback /
  failed / fail / fatal / panic / warning / deprecated / errno /
  segfault / abort / npm err; case-insensitive, also matches camelcase
  like `ValueError` and pytest's E / F / W column markers)
- capped at 80 kept interesting lines so a noisy `pip install`
  doesn't drown out the real failure
- then the existing `_truncate` bounds the result at `MAX_OUTPUT_CHARS`
  (10 KB)

Stats (`output_summarized.{kept_head, kept_tail, kept_interesting_
middle, omitted_middle}`) are surfaced on the tool result so the
agent loop / metrics can chart how often summarization fires per
model. Pure function — never throws, never mutates input.

### Verifier-on-by-default for coding tasks

The completion-verifier subagent (`_run_verifier_subagent` in
`agent_loop.py`) used to be off by default because on weak local
models the action-snapshot it judges from doesn't include the doc
body and it false-rejects on every effectful turn. New heuristic
(`_looks_like_coding_turn`, `_CODING_TURN_HINT_RE`) flips the
default:

- **ON** when a workspace is set AND the user message matches a
  code-ish term (fix / bug / error / traceback / implement /
  refactor / port / migrate / coverage / pytest / docker / etc.)
  OR the message contains a recognized file extension (`.py`,
  `.ts`, `.go`, …)
- **OFF** for the personal-assistant use case (email, calendar,
  notes, chitchat) where it would only add cost
- The setting still wins: explicit `true` forces on for every turn,
  explicit `false` forces off

The regex is intentionally conservative — it excludes ambiguous
words like "add" (a calendar event, a note, a contact all use "add")
in favor of unambiguous anchors. Covered by
`tests/test_coding_turn_heuristic.py` (15 boundary cases).

### Project bootstrap (one-call orientation)

`project_bootstrap` tool (`src/agent_tools/project_tools.py`) gives
the agent a fast "what am I looking at" answer in a single call,
replacing 5+ read_file / ls / glob round trips on a fresh workspace.
Detects project type / language / package manager / preferred test
runner / linter / formatter, surfaces entry points + conventions,
and previews any opencode / Claude-Code instruction files
(AGENTS.md, CLAUDE.md, .opencode/instructions.md, README). Cached
per-workspace (mtime-keyed via `mtime_signature`); invalidate via
`invalidate_bootstrap_cache` when a signature file changes.
Failure mode: refuses with a clean error if no workspace is set,
telling the model to pick one first.

### Git awareness (first-class tools)

The agent used to shell out to `git` via `bash` for everything;
common-case queries like "what's changed?" / "who wrote this?" cost
a full round + a model decision about formatting. New first-class
tools in `src/agent_tools/git_tools.py`:

- `git_status` — branch, ahead/behind, every file with x/y/staged/
  unstaged/untracked flags
- `git_diff` — unified diff; `staged=true` → `--cached`; untracked
  files surface as a virtual "+" diff so the model can "see what
  changed" right after `write_file`
- `git_log` — recent commits; `oneline=false` for full bodies; scope
  to a `path`
- `git_blame` — annotated lines; `start_line` / `end_line` for regions
- `git_commit` — stage the given paths (or all modified files) and
  commit; multi-line / amend / fixup still go through `bash`
- `git_branch` — list / current / create / delete (refuses unmerged;
  force-delete via `bash`)

All workspace-scoped, all `subprocess.run` with a 30s timeout, all
path-confined via `_resolve_tool_path_in_workspace` so the model
can't escape the workspace. Reuses the existing tool-dispatch
plumbing (one `elif` entry routes all 6 through `_direct_fallback`).

### Tool surface (current)

```
TOOL_TAGS = {                                  ← single source of truth,
                                                  enforced by the dispatcher
    bash, python, web_search, web_fetch,
    read_file, write_file, edit_file,
    grep, glob, ls, get_workspace,
    create_document, update_document, edit_document,
    search_chats, chat_with_model, create_session,
    list_sessions, send_to_session, pipeline,
    manage_session, manage_memory, list_models,
    ui_control, generate_image, ask_user, update_plan,
    manage_todos, spawn_agent,
    manage_tasks, api_call, ask_teacher, manage_skills,
    suggest_document, manage_endpoints, manage_mcp,
    manage_webhooks, manage_tokens, manage_documents,
    manage_settings, manage_notes, manage_calendar,
    resolve_contact, manage_contact, list_email_accounts,
    send_email, list_emails, read_email, reply_to_email,
    bulk_email, archive_email, delete_email, mark_email_read,
    download_model, serve_model, list_served_models,
    stop_served_model, list_downloads, cancel_download,
    search_hf_models, list_cached_models, list_serve_presets,
    serve_preset, adopt_served_model, list_cookbook_servers,
    edit_image, trigger_research, manage_research, app_api,
    run_tests, lint, format,                    ← Phase 4 (auto-detect)
    git_status, git_diff, git_log, git_blame,    ← first-class git
    git_commit, git_branch,                      ← (workspace-scoped)
    project_bootstrap                            ← one-call orientation
}
```

Every tool has:

- A schema entry in `FUNCTION_TOOL_SCHEMAS` (`src/tool_schemas.py`) +
  an `elif` in `function_call_to_tool_block` (the native → text converter)
- A handler class in `src/agent_tools/*.py` registered in `TOOL_HANDLERS`
- A documentation block in `TOOL_SECTIONS` (`src/agent_loop.py`)
- A domain in `_DOMAIN_TOOL_MAP` (`src/agent_loop.py`) — semantic
  retrieval via `src/tool_index.py`

A guard test (`tests/test_agent_prompt_budget.py`) asserts each
rule block stays under a reasonable cap so accidental additions
(another 30-line anti-pattern paragraph) are caught before they
ship. The cap sizes are conservative; update the cap AND leave a
comment explaining why if you genuinely need to grow a section.


### Codex bridge

`/api/codex/{files,cookbook,memory,email,todos,calendar,documents}/…`
endpoints gated by API-token scopes (`files:read`, `files:write`,
`cookbook:read`, `cookbook:launch`, `todos:read|write`, `email:read|draft|send`,
`memory:read|write`, `calendar:read|write`, `documents:read|write`).
`/api/claude/plugin.zip` serves the Claude Code skill bundle.

---

## 6. Code Workspace — the headline DevSpace feature

Status: **all 8 phases complete and verified 2026-06-19** (see
`docs/HANDOFF.md`). A 3-pane **Code Workspace** modal sits inside the
app, driven by the chat agent + the user.

### Language Server Protocol (LSP) — Python, TypeScript, Rust

A dedicated WebSocket bridge gives the in-app Code editor real
language-server features (diagnostics, hover, completion,
go-to-definition, formatting). The editor itself is Monaco 0.52.2,
vendored at `backend/static/lib/monaco/` (14 MB AMD bundle, no
build step, no CDN dependency — keeps the Tauri desktop build
offline-friendly).

**Endpoints** (under `/api/lsp/`, defined in
`backend/routes/lsp_routes.py`):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/lsp/availability` | JSON `{lang: bool}` for every language the bridge knows about. Frontend uses this to dim the LSP status pill when a server isn't installed. |
| `WS` | `/api/lsp/{lang}?path=<root>` | JSON-RPC over WebSocket. One session per `(lang, workspace_root)` pair, refcounted and idle-reaped after 10 min. Frames are full JSON-RPC messages (`{jsonrpc:"2.0", id, method, params}`) sent as text frames — no extra envelope. |

**Path confinement:** every `textDocument/didOpen` (and other
URI-bearing request) is mapped back to a filesystem path and checked
against the workspace root + a sensitive-path denylist
(`_resolve_tool_path_in_workspace` in `code_workspace_routes.py`).
URIs outside the root or matching a denylisted substring (`.ssh`,
`.gnupg`, `id_rsa`, etc.) are dropped silently — the language
server is never asked to read them. Mirrors the existing Code
Workspace policy so adding LSP doesn't widen the attack surface.

**Server configuration** (`backend/src/lsp_bridge.py:LSP_SERVERS`):

| Language | Server | Install | Capability check |
|---|---|---|---|
| `python` | `python -m pylsp` (python-lsp-server) | `pip install "python-lsp-server[all]"` | Module import check |
| `typescript` | `typescript-language-server --stdio` (also handles JS/TSX/JSX) | `npm install -g typescript-language-server` | `which_tool` PATH lookup |
| `rust` | `rust-analyzer` | `rustup component add rust-analyzer` | `which_tool` PATH lookup |

Adding a new language is a config-only change: add an entry to
`LSP_SERVERS`, a branch in `_lspLanguageFor` in `codeWorkspace.js`,
and (optionally) register a Monaco basic-languages contribution for
syntax highlighting.

**Lifecycle:**

1. Frontend opens a file → calls `/api/lsp/availability` → opens a
   WebSocket to `/api/lsp/{lang}?path=<root>` if the server is
   installed.
2. The route reuses an existing subprocess (refcounted) or starts a
   new one. The subprocess is launched with `cwd=<root>` so pylsp
   indexes the right project.
3. Editor keystrokes forward `textDocument/didChange`; Save sends
   `textDocument/didSave`; the server's `publishDiagnostics` flows
   back as Monaco markers.
4. After the last WebSocket disconnects the refcount drops to 0; the
   session stays alive for 10 min so a re-open is instant; then the
   sweeper reaps it.

**Status visibility:** the editor toolbar shows a colour-coded
`LSP:` pill (`pending` / `ok` / `off` / `error`) plus an "Enable
LSP" checkbox that persists in `localStorage.code.lsp_enabled`.
When off, the bridge never opens a WebSocket — saves a roundtrip
and a pylsp startup for users who want the editor but not the
language server.

**Tests** (`backend/tests/test_lsp_bridge.py`): 22 cases including
end-to-end pylsp round-trips for `initialize`, `didOpen`, and
`didChange` triggering `publishDiagnostics`.

### Layout

```
┌──────────────── Code Workspace modal (drag/resize) ────────────────┐
│ [Files] [Git]                       [⇄ root]  [⚙ pick workspace]  │
├────────────────┬───────────────────────────────┬───────────────────┤
│  File tree     │   Code editor                 │   Diff viewer     │
│  (lazy-expand) │   (textarea + hljs overlay)   │   (per save)      │
│  fuzzy search  │   language auto-detected      │   Accent active   │
│                │                               │   bar + +/- rails │
└────────────────┴───────────────────────────────┴───────────────────┘
```

### Files (Phase 0–2 + Phase 5 additions)

- **Backend** (`routes/code_workspace_routes.py`, ~600 lines):
  - `GET/POST /api/workspace/current` — persisted root (settings)
  - `GET /api/workspace/tree?path=` — one-level dir listing, skips
    `_CODENAV_SKIP_DIRS` (matches agent's `ls`/`glob`)
  - `GET /api/workspace/file?path=` — read (truncated at MAX_READ_CHARS)
  - `POST /api/workspace/file` — write, returns unified diff
  - `GET /api/workspace/search?q=` — recursive fuzzy filename search
  - `POST /api/workspace/{revert,apply,discard}` — checkpoint resolution
  - `POST /api/workspace/revert_all/{session_id}` — roll the session back
  - `GET /api/workspace/git/{status,diff}` + `POST /git/{stage,unstage,commit}`
  - `POST /api/workspace/shell` — owner-authed streaming shell
    (SSE) — cwd = workspace root
  - `WS /api/workspace/pty` — owner-authed interactive PTY via
    `core/pty_session.py` (cross-platform; uses pywinpty on Windows)
  - **Auth posture:** owner-authed (not admin-gated), every path
    confined via `_resolve_tool_path_in_workspace()`

- **Frontend** (`static/js/codeWorkspace.js`, ~480 lines):
  - `Modals.register('code-workspace-modal', …)` + `makeWindowDraggable`
  - 3-pane flex chain with explicit `height: min(880px, 90vh)`
  - File tree: lazy expansion, fuzzy search box, kind filter
  - Code editor: transparent textarea over hljs `<pre>` overlay,
    auto-detected language (extension map → one-time content guess
    with relevance ≥ 7)
  - Save → POST /file → render the unified diff in the right pane
  - Listens for `workspace:diff-applied` CustomEvent and reloads the
    open file when the agent edits it (auto mode)
  - Git tab: lazy-mounts `gitPanel.js` on first open

- **Terminal** (`static/js/terminalPanel.js`, ~270 lines):
  - Lazy-loads xterm.js + xterm-addon-fit from `/static/lib/`
  - Connects to `WS /api/workspace/pty?cols=&rows=`
  - Binary frames = keystrokes; JSON `{type:'resize',cols,rows}`
  - xterm rendered with theme colors pulled from CSS vars
  - Keystrokes go to PTY stdin; PTY output streamed straight to xterm
  - PTY session owned by `core/pty_session.py` (cross-platform wrapper)

- **Git Panel** (`static/js/gitPanel.js`, ~190 lines):
  - Branch bar, status list with X/Y badges
  - Stage/unstage per file and "stage/unstage all"
  - File diff viewer (reuses the diff colors)
  - Commit box; refresh on every action

- **Diff approval UX** (`static/js/toolOutputHooks.js`,
  `static/js/chat.js` `initListeners()`):
  - Shared hook `highlightToolOutput(node)` runs hljs on
    `.agent-tool-output > pre:not(.diff-pre)` (relevance ≥ 10)
  - Shared hook `_attachDiffApprovalButtons(node, diff)` injects a
    `.diff-actions` bar with Keep/Revert (auto) or Apply/Discard (strict)
  - Delegated click handler resolves the checkpoint via the API and
    dispatches `workspace:diff-applied` so the panel reloads

- **Native code-quality tools** (`src/agent_tools/code_quality_tools.py`):
  - `run_tests` — auto-detects `pytest`, `npm test` / `yarn test` / `pnpm test`,
    `go test`, `cargo test` and runs with `cwd=agent_cwd()`
  - `lint` — auto-detects `ruff check`, `eslint`, `flake8`
  - `format` — auto-detects `black`, `prettier`, `ruff format`
  - Optional `target` argument; shell metacharacters rejected
  - Streams output via `_run_subprocess_streaming` (same helper as bash)

- **@filename mentions** (`static/js/fileMentionAutocomplete.js`):
  - Forks `slashAutocomplete.js` for the `@partial` trigger
  - Hits `GET /api/workspace/search?q=`, inserts `@<path> ` on selection
  - Wired to `#message` in chat.js

- **Codex files API** (`routes/codex_routes.py`):
  - `FILES_READ_SCOPES = {"files:read", "files:write"}`,
    `FILES_WRITE_SCOPES = {"files:write"}`
  - `GET /api/codex/files/{list,read}`, `POST /api/codex/files/write`
  - Confined to the same `_codex_ws_root()` + `_codex_confine()` helpers
  - Scopes surfaced in the API-token mint UI

### Phase 2 visual bugs (resolved 2026-06-19)

The original Code Workspace panel shipped with 4 visual bugs in dark/light
themes, documented and fixed in `docs/HANDOFF.md`:
1. Editor pane collapsed (no definite height → position:absolute children
   sized off siblings).
2. Textarea painted an opaque field over the hljs `<pre>` in light theme
   and on hover (specificity: global `:root.light textarea{…}` outranked
   `.cw-editor-textarea`). Fix: scope panel form-control rules under
   `#code-workspace-modal`.
3. Dead `var(--accent)` references — `--accent-primary` is the actual
   variable name in this codebase. Now `var(--accent, var(--red))`.
4. Full-bleed modal (no padding, no overflow) + accent active-bar.

See `design/cw-review/01_current_BUG_editor_collapses.png` through
`08_FINAL_picker.png` for the before/after sequence.

---

## 7. Phase status — what's done, what's in flight

### Done (Phases 0–4 of the desktop build)

| # | Phase | Status |
|---|---|---|
| 0 | backend boots natively in single-user mode | ✅ |
| 1 | Tauri shell spawns the backend as a sidecar and loads its UI | ✅ |
| 2 | embedded ChromaDB, native notifications, dialog/opener plugins | ✅ |
| 3 | rebrand (name, accent, logo, app icon) + full Code Workspace UI | ✅ |
| 4 | Windows installer packaging (`tauri build` → NSIS/MSI) | ✅ |

### Done — Code Workspace (Phases 0–8 of `docs/code-workspace-plan.md`)

| # | Phase | Status |
|---|---|---|
| 0 | scaffolding (anchors, inert stubs) | ✅ verified |
| 1 | workspace backbone backend (tree/file/search/current) | ✅ 14 endpoint assertions pass |
| 2 | Code Workspace panel UI | ✅ verified dark + light |
| 3 | diff approval (toggle, checkpoints) | ✅ verified |
| 4 | native `run_tests` / `lint` / `format` | ✅ verified |
| 5 | Terminal v1 (xterm.js + owner-authed PTY WebSocket) | ✅ verified |
| 6 | Git panel | ✅ verified |
| 7 | quick wins (tool-output hljs + `@`-mention) | ✅ verified |
| 8 | Codex files API (scoped external bridge) | ✅ verified |

### Recently completed in the agent surface (2026-06-20)

These sit on top of the Code Workspace foundation and address long-coding
agent workflows:

- **`agent_auto_continue`** — `agent_max_rounds` becomes a soft checkpoint
  up to `agent_max_rounds_ceiling` (default 150). Agent emits
  `agent_continue` markers; UI shows a fading "Still working —
  continuing automatically…" chip (`chat.js`).
- **`manage_todos`** tool — agent's own checklist; re-anchored at each
  auto-continue checkpoint so a long task stays on rails.
- **`spawn_agent`** tool — nested sub-agent with `explore|code|general`
  presets; depth-capped at 1.
- **`test_agent_auto_continue.py`**, **`test_loop_breaker_progress.py`**,
  **`test_manage_todos_tool.py`**, **`test_spawn_agent_tool.py`** — new
  pytest files covering the above.

### Recently completed in the agent surface (2026-06-20, coding-agent pass)

A focused pass to make the existing chat agent usable for real coding
work — comparable in feel to opencode, without introducing a new
panel or new endpoint shape. All changes are additive and
backward-compatible (existing installs that set
`agent_edit_review: "auto"` keep that value; the strict default
applies only to fresh installs).

- **Safer edit loop** — `agent_edit_review` default flipped from
  `"auto"` to `"strict"`. New `edit_pending` SSE event + input-bar
  pending chip + "View session changes" drawer on the assistant
  message. New `GET /api/workspace/checkpoints` endpoint backs
  both. `edit-then-verify` soft loop-side nudge: after 2 consecutive
  `edit_file` / `write_file` calls without `run_tests` / `lint` /
  `format`, injects a one-time reminder. (`agent_loop.py:2390` et al.,
  `routes/code_workspace_routes.py`, `static/index.html`,
  `static/js/chat.js`, `static/js/sessions.js`, `static/style.css`.)
- **First-class git tools** — `git_status`, `git_diff`, `git_log`,
  `git_blame`, `git_commit`, `git_branch` as named tools with
  structured output, workspace-scoped, path-confined. Replaces
  shell-out for the common-case queries. (`agent_tools/git_tools.py`,
  `tool_schemas.py`, `tool_execution.py`, `tool_index.py`,
  `agent_loop.py:TOOL_SECTIONS`.)
- **Output summarization** — bash / python outputs over 10K chars
  now keep the head, the tail, and any "interesting" line in the
  middle (error / exception / traceback / fail / panic / warning /
  camelcase like `ValueError` / pytest E F W markers), capped at 80
  interesting lines so a noisy `pip install` doesn't drown out the
  real failure. Stats surfaced on the tool result for metrics.
  (`output_summarizer.py`, `subprocess_tools.py`.)
- **Verifier-on-by-default for coding tasks** — completion verifier
  subagent now fires by default when the workspace is set AND the
  user message matches a code-ish term (fix / bug / error /
  implement / refactor / coverage / pytest / docker / etc.) OR
  contains a recognized file extension. Stays off for the
  personal-assistant use case where it false-rejects.
  (`agent_loop.py:_looks_like_coding_turn`.)
- **Project bootstrap** — one-call workspace orientation. Detects
  project type / package manager / test / lint / format commands,
  surfaces entry points + conventions + AGENTS.md / CLAUDE.md /
  .opencode/instructions.md previews. Cached per-workspace
  (mtime-keyed). (`agent_tools/project_tools.py`.)
- **Open-in-editor deep-link** + **session changes chip + drawer**
  on every edit card in the chat thread. Reuses the existing
  Code Workspace modal — no new panel. (`toolOutputHooks.js`,
  `codeWorkspace.js:openCodeWorkspaceAt()`, `chat.js`.)
- **Prompt-budget guard** — `tests/test_agent_prompt_budget.py`
  asserts each rule block stays under a reasonable cap, so future
  accidental additions to the KV-cached system prefix are caught
  before they ship.

New / updated test files:

- `tests/test_edit_file.py` (updated, 10 tests) — review-mode
  default + auto/strict split
- `tests/test_workspace_checkpoints_enumerate.py` (5 tests) — pure
  function tests for the new checkpoint helpers
- `tests/test_workspace_checkpoints_route.py` (5 tests) — HTTP-level
  tests for the new GET endpoint
- `tests/test_git_tools.py` (27 tests) — real-git end-to-end + 3
  dispatcher-integration tests
- `tests/test_output_summarizer.py` (11 tests) — unit tests for the
  summarizer
- `tests/test_bash_summarizer_integration.py` (6 tests) — bash / python
  tools actually call the summarizer
- `tests/test_coding_turn_heuristic.py` (15 tests) — boundary cases
  for the verifier-on-by-default heuristic
- `tests/test_project_bootstrap.py` (22 tests) — pure + integration
  tests for the bootstrap tool
- `tests/test_agent_prompt_budget.py` (8 tests) — cap guards on the
  KV-cached system prompt prefix

**212 tests pass** across the agent / chat / workspace test surface
post-pass; the existing test suite was preserved throughout.

### In flight / next (deferred)

From `backend/ROADMAP.md` and `docs/phase2-decisions.md`:

- **Fully-portable Python runtime bundling** — the current installer is
  "this-machine"; portable bundling of `torch` / `onnx` / `chromadb` /
  `faster-whisper` (multi-GB) is the next packaging milestone.
- **CSS cleanup** — `static/style.css` is ~1.26 MB of single-file CSS
  with paired desktop/mobile overrides that frequently fight each
  other. Tracked in upstream ROADMAP.md.
- **Modal/window positioning cleanup** — still fragile.
- **Provider probe/setup audit** (Anthropic, Gemini, Groq, xAI, etc.).
- **Skill/tool prompt-injection audit** (notes, memories, fetched pages
  are treated as untrusted data — keep testing).
- **Email performance audit** (cache/prefetch without breaking
  multi-account state).
- **Cookbook ranking** — score newer architectures / hardware-fit more
  confidently.
- **Accessibility pass** (keyboard nav, focus states, reduced motion).
- **Tour core helper** — onboarding tours have too much copy-pasted
  scaffolding.

---

## 8. Coding conventions (when contributing)

From `docs/HANDOFF.md` + observed codebase:

- **Tool handler signature:** `async def execute(self, content: str, ctx: dict) -> dict`
- **Add-a-tool recipe** (append-only):
  1. Schema entry in `FUNCTION_TOOL_SCHEMAS` (`src/tool_schemas.py`)
  2. `elif` clause in `function_call_to_tool_block` (the native→text converter)
  3. Handler class in `src/agent_tools/<group>_tools.py`
  4. Register in `TOOL_HANDLERS` dict (`src/agent_tools/__init__.py`)
  5. Add to `TOOL_TAGS` **set** (append-only)
  6. Docs in `TOOL_SECTIONS` (`src/agent_loop.py`)
  7. Add to the relevant domain in `_DOMAIN_TOOL_MAP`
  8. For workspace-scoped tools, add an `elif` in
     `_execute_tool_block_impl` (`tool_execution.py`) that routes to
     `_direct_fallback` — see `git_status` / `git_diff` / … for the
     canonical one-liner pattern (one entry covers a whole family of
     sibling tools).
  9. Add a short description in `BUILTIN_TOOL_DESCRIPTIONS`
     (`src/tool_index.py`) so the RAG retriever can find it.
  10. Add to the relevant `_SUBAGENT_TOOLSETS` set in
      `tool_execution.py` if a sub-agent should be able to call it
      (otherwise the model can only call it from the main agent).
  11. Add a prompt-budget guard test entry — bump the cap in
      `tests/test_agent_prompt_budget.py` if your new content
      legitimately needs the room.
- **Path confinement:** always use `_resolve_tool_path_in_workspace(root, path)`
  or `vet_workspace()` for anything that touches disk.
- **Owner-authed, not admin-gated** for in-app workspace/file/terminal
  endpoints (single-user trusted posture). Use `get_current_user(request)`,
  not `require_admin`.
- **Modal pattern:** `Modals.register(id, {railBtnId, sidebarBtnId, closeFn, restoreFn})`
  + `makeWindowDraggable(modal, {content, header})`. Self-init at the
  bottom of the module (ES modules are deferred, DOM is ready).
- **Editor pattern:** transparent textarea + `<pre>` hljs overlay
  (absolute positioned, synced scroll).
- **CSS:** avoid `!important` unless overriding a global `!important`
  rule (e.g. the global `pre { background: ... !important }`).
- **Service worker:** bump `CACHE_NAME` (`odysseus-v335`) whenever the
  precache list or logic changes.
- **Settings:** append-only on `DEFAULT_SETTINGS`. Per-user overrides
  via `_PER_USER_KEYS` whitelist (currently vision_model, image_model,
  default_endpoint, etc.).
- **Atomic writes:** use `core.atomic_io.atomic_write_json(...)` for
  any settings/preset/skill/feature JSON.
- **Auth middleware posture:** CORS preflight must bypass auth;
  the loopback check must reject proxy-forwarded requests
  (`cf-connecting-ip`, `x-forwarded-for`, etc.) — otherwise
  `LOCALHOST_BYPASS` can be abused via Cloudflare tunnel.
- **CSP nonce:** HTML files use `{{CSP_NONCE}}` placeholder;
  `_serve_html_with_nonce(request, file_path)` substitutes it.

---

## 9. Quick start (dev)

### Prereqs (Windows)

- Rust (rustup)
- Node
- Tauri CLI v2 (`cargo install tauri-cli --version "^2"`)
- Python 3.12 with backend deps installed in a venv (the dev default
  reuses `odysseus-ref/venv`)

### Run

```powershell
# from repo root
tauri dev
```

The shell auto-spawns `backend/` with `odysseus-ref/venv`'s Python.
No env vars needed unless you want to override:

```
DEVSPACE_PYTHON=D:\path\to\other\python.exe
DEVSPACE_BACKEND_DIR=D:\path\to\backend
DEVSPACE_PORT=8123
```

### Build the installer

```powershell
tauri build
```

Produces `src-tauri/target/release/bundle/{nsis,msi}/DevSpace_*_x64-*.{exe,msi}`.

### Test the backend alone (no Tauri)

```powershell
$env:AUTH_ENABLED = "false"
$env:PYTHONPATH = "D:\projects\DevSpace\backend"
& "D:\projects\DevSpace\odysseus-ref\venv\Scripts\python.exe" -m uvicorn app:app --port 8123 --host 127.0.0.1
# from D:\projects\DevSpace\backend
```

Then `Invoke-RestMethod http://127.0.0.1:8123/api/workspace/current` etc.

### Run pytest

```powershell
cd backend
& "..\odysseus-ref\venv\Scripts\python.exe" -m pytest tests/test_workspace_confine.py -v
# ~600 test files; full suite runs in ~minutes
```

---

## 10. Where to start reading (TL;DR)

If you are an AI agent picking this project up:

1. **THIS file** — you are here.
2. **`README.md`** — top-level orientation, run commands.
3. **`docs/HANDOFF.md`** — Phase-by-phase narrative of the Code Workspace
   build; lists exactly which file owns what per stream.
4. **`docs/code-workspace-plan.md`** — the full plan with file:line refs
   (the architecture decisions are baked into the cross-cutting
   decisions section).
5. **`docs/phase2-decisions.md`** — the "desktop-ify services" decision
   record (Chroma embedded, native notifications, no bundled SearXNG,
   deferred portable runtime bundling).
6. **`src-tauri/src/lib.rs`** — the whole desktop shell in one file
   (~700 lines, well-commented).
7. **`backend/app.py`** — the backend orchestrator; scanning the
   `include_router` block tells you the full surface.
8. **`backend/src/agent_loop.py`** — the agent. `TOOL_SECTIONS` (line
   ~306) and `_DOMAIN_TOOL_MAP` (line ~280) are the two indexes that
   explain what the agent can do.

If you are about to make a change:

- Adding a **tool** → §8 "Coding conventions" recipe.
- Adding a **route** → mirror the pattern in `routes/code_workspace_routes.py`
  (owner-authed + `_resolve_tool_path_in_workspace` if it touches disk).
- Adding a **modal** → mirror `static/js/codeWorkspace.js` (use
  `Modals.register` + `makeWindowDraggable`, self-init at module bottom).
- Touching **CSS** → scope rules under the modal id; never rely on
  `var(--accent)` (use `var(--accent, var(--red))`); never go below
  the global `pre { background: ... !important }` without `!important`.
- Bumping the **service worker** → update `CACHE_NAME` in `sw.js`.