# DevSpace

A single-user **desktop AI workspace** — a Tauri (Rust) shell wrapping a local
Python/FastAPI backend, supporting both **local and API LLM providers**. It runs
entirely on your machine, no login. Features: chat + agents, deep research,
a built-in **code workspace** (file tree, editor, diff-approval, terminal, git),
documents, email, calendar/notes/tasks, image gallery/editor, and local model
management.

DevSpace is a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
(AGPL-3.0), restructured into a native desktop app.

## Status

- [x] **Phase 0** — backend boots natively in single-user mode (`AUTH_ENABLED=false`)
- [x] **Phase 1** — Tauri shell spawns the backend as a sidecar and loads its UI
- [x] **Phase 2** — embedded ChromaDB (no external service), native notifications, dialog/opener plugins
- [x] **Phase 3** — rebrand (name, accent, logo, app icon) + a full **Code Workspace** UI
- [x] **Phase 4** — Windows installer packaging (`tauri build` → NSIS/MSI)

### Code Workspace (built-in)

A complete in-app developer panel driven by the agent and by you:

- File tree with lazy expansion + fuzzy filename search
- Code editor with automatic syntax highlighting (extension map → content guess)
- **Diff approval** — agent edits are captured/staged and you Accept/Reject them
- Integrated **terminal** (xterm.js, workspace-scoped, owner-authed)
- **Git panel** — branch bar, status, stage/unstage, per-file diff, commit
- Native `run_tests` / `lint` / `format` agent tools (auto-detects the toolchain)
- `@`-mention files into the chat context

## Layout

| Path | Role |
|---|---|
| `src-tauri/` | Tauri (Rust) desktop shell — spawns the backend, manages its lifecycle, owns the window |
| `backend/` | Python/FastAPI backend + the vanilla-JS frontend it serves (forked Odysseus) |
| `dist/` | Local splash shown while the backend is starting |
| `design/` | Icon/source assets + design previews |
| `docs/` | Architecture notes and the code-workspace implementation handoff |

> The reference upstream clone (`odysseus-ref/`) and all runtime data
> (`backend/data/`, logs, secrets) are intentionally **not** tracked — see `.gitignore`.

## How it works

On launch the shell picks a free loopback port, starts `uvicorn app:app` with auth
disabled, waits for it to accept connections, then navigates the webview to it. The
backend serves the existing (framework-free) frontend, so the UI is reused as-is. On
Windows the backend tree is bound to a kill-on-close Job Object so uvicorn and every
MCP-server child die with the app (no orphaned `python.exe`).

## Run (dev)

Prereqs: Rust, Node, Tauri CLI v2, Python 3.12 with the backend deps installed in a
venv (the dev default reuses `odysseus-ref/venv`).

```
tauri dev
```

Path overrides (point at a different backend/interpreter):
`DEVSPACE_PYTHON`, `DEVSPACE_BACKEND_DIR`, `DEVSPACE_PORT`.

## Build the installer

```
tauri build
```

Produces a Windows installer under `src-tauri/target/release/bundle/` (NSIS `.exe`
and/or MSI). The current build targets this machine's local backend + venv paths;
fully portable bundling of the Python runtime is future work.

## License

AGPL-3.0 (inherited from Odysseus). See [`LICENSE`](LICENSE) and the backend's
`ACKNOWLEDGMENTS.md`.
