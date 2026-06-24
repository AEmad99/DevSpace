# GitHub Issue Triage & Feature Plan

**Repo:** `AEmad99/DevSpace`
**Triage date:** 2026-06-24
**Open issues found:** 1 (no closed issues)

---

## Issue Summary

### #2 — Support Custom Knowledge Sources Beyond Internet Research
- **Author:** @Khalid-Tarek
- **Created:** 2026-06-23
- **State:** Open · 0 comments · 0 reactions · no labels · no assignee
- **URL:** https://github.com/AEmad99/DevSpace/issues/2

**Verbatim body:**

> Right now, the application only researches topics on the internet before generating a report. It would be useful if the research source could be configurable instead.
>
> For example, I'd like to be able to point the agent at a codebase, a folder of documents, or an internal knowledge repository and have it perform the same research and analysis workflow on that data instead of searching the web.
>
> The research process itself wouldn't need to change, only where the information comes from. This would make the application useful for understanding existing projects, generating documentation, and analyzing internal knowledge, not just public internet content.

---

## Features Requested

The issue collapses to **one umbrella feature** with three concrete source types:

| # | Feature | Description |
|---|---|---|
| F1 | **Configurable research source** | Add a UI/API control to select *where* research pulls information from, instead of always defaulting to internet search. |
| F2 | **Local codebase source** | Point the research workflow at a local git repo / code workspace and have the agent analyze it (architecture, dependencies, call graphs, docs). |
| F3 | **Document-folder source** | Point at a folder of files (PDF, MD, TXT, DOCX, code, etc.) and have the agent perform RAG-style research over them. |
| F4 | **Internal knowledge repository source** | Support a persistent, named corpus the user pre-populates (a "knowledge base") that survives across sessions and reports. |

Implicit supporting features that fall out of F1–F4:

| # | Supporting Feature | Why it's required |
|---|---|---|
| S1 | **Source picker in Deep Research UI** | The frontend (`backend/static/js/research/...`) currently has no "source" selector. |
| S2 | **Source-type plugin interface** | New sources must be addable without forking the research pipeline. |
| S3 | **Persistent per-source indexing** | Re-indexing on every research run would be slow; results should be cached. |
| S4 | **Hybrid mode (internet + local)** | The current pipeline can only fan-out to web search; users will want "search both, merge findings". |

---

## Feasibility Assessment

**Overall: HIGH — already ~70% of the infrastructure exists.**

### What's already in the codebase (the hard parts)
- ✅ **Embedded ChromaDB** — `backend/src/chroma_client.py` is wired up; local RAG primitives already work (Phase 2 of README).
- ✅ **Code Workspace** — file tree, editor, git panel, `@`-mention of files into chat (Phase 3 of README).
- ✅ **DeepResearcher class** — `backend/src/deep_research.py` (929 lines) with `findings`, `sources`, `get_stats()`, JSON parsing, fallback report generation.
- ✅ **ResearchHandler** — `backend/services/research/research_handler.py` (485 lines) manages sessions, sources extraction, result persistence (`backend/data/deep_research/`).
- ✅ **Report-token bridge** — `backend/app.py` already special-cases `/api/research/report/...` (lines 231–232).
- ✅ **Document pipeline** — `backend/services/` already has file-processing utilities for the docs feature.

### What needs to be built (the new parts)
- ❌ A `Source` abstraction layer between the request and the existing search/retrieval step.
- ❌ Per-source adapters: `CodebaseSource`, `FolderSource`, `KnowledgeBaseSource`, `InternetSource` (default).
- ❌ ChromaDB collection-per-source + incremental indexing.
- ❌ UI source picker + per-source configuration modal.
- ❌ Mixed-source orchestrator that merges findings from multiple sources.

### Risks / unknowns
- ⚠️ **Large codebases** — naive recursive indexing can blow up. Needs gitignore-aware + size-cap + file-type filtering.
- ⚠️ **Embedding cost** — local sentence-transformers vs. delegating to whichever LLM provider the user has configured (so it costs them tokens, not us).
- ⚠️ **Citation format** — current `_extract_sources()` produces URLs; local sources need `file://` or path:line references.
- ⚠️ **Watcher overhead** — file-watcher for "live" corpora must be debounced or it will pin a CPU core on big repos.

### Verdict
**Feasible and high-value.** Should be a multi-milestone effort, not a one-shot PR. Recommend splitting into 4 milestones so each is reviewable and shippable independently.

---

## Implementation Plan

### Architecture (target)

```
┌──────────────────────────┐
│  Deep Research UI        │  ← add "Source" dropdown + config modal
│  (static/js/research)    │
└────────────┬─────────────┘
             │ POST /api/research  { query, source: {type, ...config} }
             ▼
┌──────────────────────────┐
│  ResearchHandler         │  ← session manager (unchanged)
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│  DeepResearcher          │  ← minor refactor: take Source[], not hardcoded web search
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│  SourceRegistry          │  ← NEW
│  ├─ InternetSource       │    (existing logic, refactored into adapter)
│  ├─ CodebaseSource       │    (NEW — uses Code Workspace path + gitignore)
│  ├─ FolderSource         │    (NEW — generic folder, file-type filter)
│  └─ KnowledgeBaseSource  │    (NEW — named, persistent, multi-folder)
└────────────┬─────────────┘
             │ unified Finding[] + SourceRef[]
             ▼
┌──────────────────────────┐
│  ChromaDB collections    │  ← one collection per source; cached embeddings
│  + existing report gen   │
└──────────────────────────┘
```

### Milestones

#### M1 — Source abstraction (foundation)
**Goal:** Introduce the `Source` interface and refactor the existing internet path through it. Zero behavior change for users.

| Task | File(s) | Effort |
|---|---|---|
| Define `Source` ABC + `SourceRef` dataclass | `backend/src/research_sources/base.py` (new) | S |
| Extract existing web-search logic into `InternetSource` | `backend/src/research_sources/internet.py` (new) + edit `backend/src/deep_research.py` | M |
| Add `SourceRegistry` with `register()` / `get()` | `backend/src/research_sources/registry.py` (new) | S |
| Wire registry into `DeepResearcher.__init__` | `backend/src/deep_research.py` | S |
| Unit tests for registry + refactor parity | `backend/tests/test_research_sources.py` (new) | S |

**Acceptance:** Existing `/api/research` with no source specified behaves identically to today; no frontend changes yet.

---

#### M2 — FolderSource (the smallest useful new feature)
**Goal:** User can point at a folder of files and research over them.

| Task | File(s) | Effort |
|---|---|---|
| `FolderSource` adapter | `backend/src/research_sources/folder.py` (new) | M |
| Recursive walker with .gitignore + size cap + extension filter | same | M |
| ChromaDB collection-per-folder, content-hash dedupe | `backend/src/chroma_client.py` (extend) | M |
| Path-based chunker (overlap, max-chars, language-aware boundaries) | `backend/src/research_sources/chunker.py` (new) | M |
| Wire into `/api/research` body schema | `backend/routes/research_routes.py` | S |
| Tests + golden-query smoke test on `docs/` | `backend/tests/test_folder_source.py` | S |

**Acceptance:** `POST /api/research {query, source:{type:"folder", path:"docs/", extensions:[".md"]}}` returns a report citing `file://docs/foo.md#L42`-style refs.

---

#### M3 — CodebaseSource (leverage the Code Workspace)
**Goal:** Research over a git repo with code-aware chunking.

| Task | File(s) | Effort |
|---|---|---|
| `CodebaseSource` adapter (extends FolderSource) | `backend/src/research_sources/codebase.py` (new) | M |
| Language-aware chunking (function/class boundaries via `tree-sitter` or simpler regex fallback) | `backend/src/research_sources/code_chunker.py` (new) | L |
| Honor `.gitignore`, exclude `node_modules`, `.git`, build dirs | codebase.py | S |
| Reuse Code Workspace's file tree for path validation | `backend/routes/code_workspace_routes.py` (read-only) | S |
| Allow `@`-mention in chat to seed a research session against the same corpus | `backend/src/chat_processor.py` | M |
| Tests | `backend/tests/test_codebase_source.py` | S |

**Acceptance:** From inside the Code Workspace, "Research this repo" produces an architecture overview with citations to real file:line locations.

---

#### M4 — KnowledgeBaseSource + Hybrid mode + UI
**Goal:** Persistent named corpora, multi-source research, and the user-facing picker.

| Task | File(s) | Effort |
|---|---|---|
| `KnowledgeBaseSource` (named, multi-folder, versioned) | `backend/src/research_sources/knowledge_base.py` (new) | L |
| CRUD API for KBs: `/api/knowledge_bases` | `backend/routes/knowledge_base_routes.py` (new) | M |
| Hybrid orchestrator: collect findings from N sources, de-dupe, re-rank | `backend/src/deep_research.py` (`run` method) | L |
| File-watcher with debounce for KB auto-reindex | `backend/services/research/watcher.py` (new) | M |
| Source picker UI + config modal | `backend/static/js/research/source_picker.js` (new) + template | M |
| Persist last-used source per session | `backend/services/research/research_handler.py` | S |
| E2E tests + screenshots in PR | `backend/tests/e2e_research_sources.py` | M |

**Acceptance:** User can create a KB called "Work Notes", add 3 folders, run research in **hybrid** mode combining the KB + internet, and see merged citations.

---

### Effort summary

| Milestone | Effort | Risk |
|---|---|---|
| M1 — Abstraction | ~3 days | Low (refactor only) |
| M2 — FolderSource | ~5 days | Low |
| M3 — CodebaseSource | ~8 days | Medium (chunker complexity) |
| M4 — KB + Hybrid + UI | ~10 days | Medium (UI + concurrency) |
| **Total** | **~26 dev days** | |

---

### Suggested PR sequencing
1. **PR #1 (M1):** Refactor only. Ship behind existing UI. No user-visible change.
2. **PR #2 (M2):** FolderSource — first new source, validates the pattern.
3. **PR #3 (M3):** CodebaseSource — biggest single win, integrates with existing Code Workspace.
4. **PR #4 (M4):** KB + UI — closes the loop with the user-facing feature from the issue.

Each PR is independently mergeable, behind a feature flag (`RESEARCH_SOURCES_ENABLED=true`) until M4 ships.

---

### Open questions to ask the issue author (Khalid-Tarek)
1. Does the "internal knowledge repository" mean a remote git repo (GitHub/GitLab) or strictly a local one?
2. Should sources be re-indexed automatically on file change, or only on explicit "rebuild" action?
3. For hybrid mode, is "internet + local" the default, or should it be opt-in?
4. Should citation format for local sources match the existing URL style, or use `path:line`?

These should be a comment on issue #2 before M2 starts.

---

### Quick links
- Issue: https://github.com/AEmad99/DevSpace/issues/2
- DeepResearcher: `backend/src/deep_research.py`
- ResearchHandler: `backend/services/research/research_handler.py`
- ChromaDB client: `backend/src/chroma_client.py`
- Research routes: `backend/routes/research_routes.py`
- Code Workspace routes: `backend/routes/code_workspace_routes.py`
- Research UI: `backend/static/js/research/`
