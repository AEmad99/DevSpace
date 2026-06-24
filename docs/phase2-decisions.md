# Phase 2 (desktop-ify) — decisions

This records the deliberate scope choices for the "desktop-ify services" phase so
later work doesn't re-litigate them.

## ChromaDB — EMBEDDED ✅

**Decision:** run ChromaDB embedded (in-process `PersistentClient`) instead of
requiring an external HTTP service on `:8100`.

Previously `src/chroma_client.py` only spoke to `chromadb.HttpClient(localhost:8100)`,
so on a fresh desktop install vector RAG and vector memory came up **degraded**
(`VectorRAG init failed`, `MemoryVectorStore DEGRADED`). The client now defaults to a
persistent on-disk store under `backend/data/chroma`, with the old HTTP behavior kept
behind an explicit `CHROMADB_HOST`/`CHROMADB_MODE=http` opt-in. No docker, no service
management — vector features work out of the box.

## Native notifications — ENABLED ✅

**Decision:** use the official `tauri-plugin-notification`.

Added to the shell (`Cargo.toml`, `lib.rs`, `capabilities/default.json`) and driven
from the frontend (`static/js/desktopNotifications.js`) so a long-running task (deep
research / agent run) raises an OS notification when it finishes while the window
isn't focused. Reachable from the loopback-origin webview via the capability's
`remote.urls` entry + `withGlobalTauri`.

## SearXNG — NOT bundled (optional external) ⏭️

**Decision:** do not bundle a SearXNG instance.

The backend already supports multiple web-search providers (`services/search/providers.py`:
searxng / brave / tavily / …) and degrades gracefully without any one of them. Bundling
and supervising a separate SearXNG web service inside a single-user desktop app is heavy
ops for marginal benefit. Users who want it can point at an external instance via the
`searxng_instance` setting (`src/config.py`) or switch `search_provider` to an
API-based provider. No code change required.

## Bundle the Python runtime — DEFERRED ⏭️

**Decision:** ship a **this-machine installer** for now; full portable bundling is
future work.

The current `tauri build` installer targets this machine's local backend + venv
(the dev paths). Bundling a full Python runtime + the backend's heavy ML deps
(torch / onnx / chromadb / faster-whisper) into the installer would make it portable
to any PC but balloons the artifact to ~GB and is snag-prone; it's tracked as the next
packaging milestone rather than blocking a runnable build today. The shell already
supports redirection via `DEVSPACE_PYTHON` / `DEVSPACE_BACKEND_DIR` for when bundled
resources become the target.
