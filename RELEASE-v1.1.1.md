# DevSpace v1.1.1 Release Notes

Released: 2026-06-24
Tag: `v1.1.1`
Installer: `DevSpace_1.1.0_x64-setup.exe` (96.6 MB, SHA256 below)

## Installer

- File: `DevSpace_1.1.0_x64-setup.exe`
- Size: 96.6 MB
- SHA256: `5a5e8004f56fb36bab5932885218d3f47cc59fc9a4e36a81aa272fc68344c0b5`
- Self-contained: bundles CPython 3.11 + every backend dep + the full
  backend source. No Python install required on the target machine,
  no PYTHONHOME setup, no symlinks back to a build-time path.
  Stdlib is fully relocated (Lib/, DLLs/, libs/, include/, tcl/) and
  pinned via a `python._pth` so the venv is portable as-is.

## What's new

### Custom knowledge sources for Deep Research (issue #2) ⭐

Resolves [issue #2](https://github.com/AEmad99/DevSpace/issues/2).
The Deep Research panel can now pull evidence from **any combination of
local files, local code, or named persistent knowledge bases** —
alongside or instead of internet search. Citations use real
`file://abs/path#Lstart-Lend` links that open in the Code Workspace editor.

Four capabilities requested in the issue:

| | Feature | What it does |
|---|---|---|
| **F1** | Configurable research source | UI picker + plug-in registry + `RESEARCH_SOURCES_ENABLED` flag; the picker auto-hides local sources when the flag is off. |
| **F2** | Local codebase source | `CodebaseSource` with **language-aware chunking** — whole functions / classes come back as one citation, not mid-statement cuts. 16 languages supported (Python, JS/TS, Go, Rust, Java, C/C++, Ruby, PHP, C#, …). Tree-sitter chunker available as opt-in for AST-perfect boundaries. |
| **F3** | Document-folder source | `FolderSource` with `.gitignore` support, incremental ChromaDB indexing (mtime + size tracking), and stable per-chunk IDs so re-indexing replaces in place. Citations are `file://abs/path#Lstart-Lend`. |
| **F4** | Persistent knowledge repository | `KnowledgeBaseSource` — named, multi-folder corpus that survives across sessions. Full CRUD REST API at `/api/knowledge_bases` (list/create/get/update/delete). A **debounced file watcher** (5s default, watchdog optional) auto-reindexes when KB member files change. |

**Hybrid orchestrator** — when you pick multiple sources, findings are
merged across them with de-duplication by `(source_id, location)` and a
1.2× boost on local hits when the query signals local intent
("this codebase", "our docs", "internal", …).

**Source picker UI** — a new "Sources" section in the Deep Research
panel renders a multi-source dropdown for every registered adapter
plus a `Knowledge Bases` opt-group listing your saved KBs. Add up to 4
rows; the picker reads each source's `config_schema` and renders the
right form fields per type.

**How to enable** — set `RESEARCH_SOURCES_ENABLED=true` in your
environment (or in the bundled install via the Settings → Advanced
panel when that's wired up). The flag defaults to off so the install
behaves exactly like v1.1.0 for anyone who doesn't opt in.

### Silent auto-approve for code edits (default)
- `agent_edit_review` defaults to `auto`; the agent's `write_file` /
  `edit_file` calls land on disk immediately with no Apply/Discard
  prompt. A small `Applied ✓` breadcrumb appears in the chat with
  an `Open in editor` deep-link.
- Strict mode is still available: turn on the **Code Edits** toggle in
  Settings (AI Defaults tab). Off (auto) is the new default; flip it
  on if you want every edit staged as a diff you accept or discard.

### Question card overlap fixed
- The multiple-choice question card no longer staggers past the
  assistant's message above it. Width now matches `.msg-ai` exactly,
  the no-op `align-self` and `margin: auto` are gone, and the question
  text is no longer rendered twice in a row (the full question only
  appears inside the card if the model didn't narrate it in the
  assistant's reply).

### Agent task list — left-of-chat panel
- The agent's working checklist is now a real panel pinned between the
  sidebar and the chat area. Lazy-unhidden on the first `todo_update`
  SSE event, live-updated as the agent works, can be collapsed to a
  0-width rail, and survives a page reload (the latest payload is
  stashed in `sessionStorage` and the collapse state in
  `localStorage`).

### Build pipeline
- `scripts/build_installer.ps1` — full pipeline: uv venv + uv pip
  install + strip pycache/pip/wheel + relocate stdlib + write
  `python._pth` + sync backend source + `tauri build`. The resulting
  NSIS installer is fully self-contained.
- `package.json` scripts: `npm run installer`, `installer:clean`,
  `installer:skip-deps`, `installer:resources-only`, `bundle:sync`.

## Build instructions

```
# Cold build (downloads + compiles every dep, ~5-15 min):
npm run installer:clean

# Warm build (reuses the cached venv, ~3-5 min):
npm run installer:skip-deps

# Resources only (no Rust compile, ~30s):
npm run installer:resources-only
```

## Verification

- **128 new research-sources tests** pass:
  - 23 (M1: Source abstraction, registry, route flag gating)
  - 34 (M2: FolderSource chunker, file enumeration, incremental indexing, retrieval, routes)
  - 34 (M3: CodebaseSource — regex + tree-sitter chunkers, language metadata, range citations)
  - 37 (M4: KnowledgeBaseSource + CRUD + hybrid merge + watcher + single-source fast path)
- **51 pre-existing research tests** still pass — zero regression on the
  legacy internet-only path (`DeepResearcher._search_and_extract` is
  preserved verbatim and `InternetSource` delegates back to it).
- **Real end-to-end smoke tests** (no mocks):
  - `FolderSource` against `backend/docs/` retrieved 10 chunks with
    correct `file://...md#L1-L42` citations.
  - `CodebaseSource` against `backend/src/` retrieved Python functions
    as whole-function chunks with `language=python` metadata.
  - Hybrid merge with local-intent query boosted KB hits (0.8→0.96)
    while leaving internet hits unchanged.

## Files changed

### Custom knowledge sources (issue #2)
**New:**
- `backend/src/research_sources/__init__.py`
- `backend/src/research_sources/base.py`
- `backend/src/research_sources/registry.py`
- `backend/src/research_sources/internet.py`
- `backend/src/research_sources/chunker.py`
- `backend/src/research_sources/code_chunker.py`
- `backend/src/research_sources/folder.py`
- `backend/src/research_sources/codebase.py`
- `backend/src/research_sources/hybrid.py`
- `backend/src/research_sources/knowledge_base.py`
- `backend/services/research/watcher.py`
- `backend/routes/research_sources_routes.py`
- `backend/routes/knowledge_base_routes.py`
- `backend/static/js/research/source_picker.js`
- `backend/tests/test_research_sources.py`
- `backend/tests/test_folder_source.py`
- `backend/tests/test_codebase_source.py`
- `backend/tests/test_kb_source.py`
- `docs/issue-2-knowledge-sources-plan.md`
- `docs/issue-2-knowledge-sources-detailed-plan.md`

**Modified:**
- `backend/app.py`
- `backend/routes/research_routes.py`
- `backend/services/research/research_handler.py`
- `backend/src/constants.py`
- `backend/src/deep_research.py`
- `backend/static/js/research/panel.js`

### Code-edit auto-approve + layout fixes
- `backend/src/agent_loop.py`
- `backend/src/settings.py`
- `backend/src/workspace_checkpoints.py`
- `backend/static/index.html`
- `backend/static/js/chat.js`
- `backend/static/js/settings.js`
- `backend/static/js/toolOutputHooks.js`
- `backend/static/style.css`
- `backend/tests/test_edit_file.py`
- `backend/tests/test_workspace_checkpoints_enumerate.py`
- `backend/tests/test_layout_regressions.py` (new)
- `package.json`
- `scripts/build_installer.ps1` (new)
