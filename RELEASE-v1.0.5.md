## DevSpace v1.0.5

**Odysseus agent/cookbook fixes, plus desktop OAuth links that actually open.**

### New / improved
- **Tool-call parsing** — Hermes/Qwen JSON inside `<tool_call>`, Gemma markers, StepFun tokens, inline fence args, and ReDoS-safe scanners. Local models that used to emit tool markup as prose now execute it.
- **`apply_patch`** — Codex-style multi-file patches (`*** Begin Patch` / `*** End Patch`), confined to the workspace, with the same review/checkpoint path as `edit_file`.
- **`manage_bg_jobs`** — list, read, or kill detached `#!bg` bash jobs in the current chat.
- **Thinking models** — DeepSeek V4 is recognized as a thinking model. gpt-oss (`harmony`) tool names that collide with built-ins (`python`/`bash`) are aliased so tool calls don’t get swallowed.
- **Cookbook Stop on Windows** — local serve records a real Win32 PID (`/proc/$$/winpid`) so Stop kills the model instead of leaving it on the GPU.
- **Live thinking** — reasoning streams are throttled so the DOM is not rebuilt on every token.
- **Indexing** — personal-doc and RAG walks skip `.git`, `node_modules`, `.obsidian`, and hidden files.
- **Desktop OAuth** — Grok / Copilot / ChatGPT device-flow “Authorize” links open in the system browser (WebView2 treats `target=_blank` as a no-op).

DevSpace-only surfaces are unchanged: Code Workspace, Grok subscription login, research sources, and the Tauri shell.

### Install
Download **`DevSpace_1.0.5_x64-setup.exe`** below and run it — it installs over your existing version and keeps your data. Self-contained (bundles its own Python runtime + dependencies), no prerequisites. Windows x64.

### Version sources
`package.json`, `tauri.conf.json`, `Cargo.toml`, and `backend/src/constants.py` (`APP_VERSION`) are all **1.0.5**.

SHA256: `E6DCD68119014D6CE4D8A67AA0A3F31A69822479C74317C38A36653764BDC5DA`
