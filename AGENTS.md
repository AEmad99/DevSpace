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

## Version sources (keep in sync)

Bump **all** of these together on a release:

- `package.json` → `version`
- `src-tauri/tauri.conf.json` → `version`
- `src-tauri/Cargo.toml` → `[package].version` (Cargo.lock updates on build)
- `backend/src/constants.py` → `APP_VERSION` (served by `GET /api/version`)

Installer artifact name comes from Tauri: `DevSpace_<version>_x64-setup.exe`.

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

## Safety / do not

- Do not commit `.env`, API keys, PATs, cookies, or `backend/data/`.
- Do not force-push `main` or rewrite published release tags without explicit ask.
- Do not add multi-user / public-bind server assumptions to the desktop path.
- Do not bundle secrets into the installer resources tree.
- Skip local agent scratch (`.codex/`, `.claude/`) and one-off patch scripts
  unless the user asks to keep them.

## Where to look first

| Task | Start here |
|---|---|
| Overall architecture | `docs/APPLICATION_OVERVIEW.md` |
| Code Workspace UI/backend | `docs/HANDOFF.md`, `static/js/codeWorkspace.js`, `routes/code_workspace_routes.py` |
| Agent loop / tools | `backend/src/agent_loop.py`, `backend/src/agent_tools/` |
| Desktop lifecycle | `src-tauri/src/lib.rs` |
| Installer pipeline | `scripts/build_installer.ps1` |
| Release notes pattern | GitHub releases + `RELEASE-v1.1.1.md` (historical packaging notes) |

## Bias to action

Read only what you need, make the smallest correct change, and verify with a
targeted test or a quick manual path (`tauri dev` / pytest slice). Prefer
updating existing modules over inventing parallel systems.
