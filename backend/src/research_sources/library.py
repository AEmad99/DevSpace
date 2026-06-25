"""`LibrarySource` — research over the user's completed deep-research reports.

Reads every saved research JSON under ``backend/data/deep_research/`` that
belongs to the current user, embeds the report body into a per-user
ChromaDB collection, and surfaces relevant chunks when the agent asks
for evidence. The user can optionally restrict retrieval to a specific
list of report IDs via ``config.report_ids`` (the multi-select in the UI).

Design choices:
  - Per-user collection name (``library_<user_slug>``) so two users on the
    same server never see each other's evidence. When the user is empty
    (single-user / auth-disabled) we fall back to ``library_default``.
  - One chunk per ~1500 chars of the report body — same chunker as
    FolderSource so the citation shape (file://...#Lstart-Lend) is
    consistent. We synthesize a synthetic ``path`` per report (the
    session id) so existing tooling can render a clickable citation.
  - Incremental warmup: re-indexes a report only when its on-disk
    ``completed_at`` mtime changes.
  - When ``report_ids`` is non-empty in the config, retrieval filters
    results to only those reports (the user explicitly chose which
    reports to look at). When the multi-select is empty, NO findings
    are returned — empty selection = no findings (per UX decision).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import Finding, Source, SourceRef
from .chunker import chunk_file
from .registry import registry

logger = logging.getLogger(__name__)

# Reuse FolderSource's defaults where they make sense (the chunker is
# the same prose-style splitter, which works well for narrative reports).
_MAX_CHARS_PER_CHUNK = 1500
_MAX_CHUNKS_PER_REPORT = 5000   # safety cap; a 7.5 MB report would hit this
_MAX_REPORTS = 5000             # safety cap; typical user has <500 reports


def _slugify_user(user: str) -> str:
    """Return a Chroma-safe collection segment from a username."""
    safe = "".join(c if c.isalnum() else "_" for c in (user or ""))[:32]
    return safe or "default"


def _collection_name(user: str) -> str:
    return f"library_{_slugify_user(user)}"


@registry.register
class LibrarySource(Source):
    """Research over the user's completed research reports.

    Config keys:
        report_ids  (list[str], default [])  subset of report session ids
                                                 to search. Empty = no
                                                 findings (UI contract).
        owner       (str,        required)   the username that scopes the
                                                 source; injected by the
                                                 route so the source sees
                                                 only its owner's reports.
                                                 Empty = single-user.
        limit_per_report (int, default 3)    per-report cap before merging
                                                 (keeps one long report
                                                 from drowning everything
                                                 else).
    """
    type_id = "library"
    display_name = "Library (Research Reports)"
    config_schema = {
        "report_ids": {"type": "array", "items": {"type": "string"}, "default": []},
        "owner": {"type": "string", "default": "", "required": True},
        "limit_per_report": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.owner: str = (self.config.get("owner") or "").strip()
        self.report_ids: List[str] = list(self.config.get("report_ids") or [])
        self.limit_per_report: int = max(1, int(self.config.get("limit_per_report", 3)))
        self.collection_name: str = _collection_name(self.owner)
        # Cached during warmup so retrieve() doesn't re-walk the data dir.
        self._reports: Dict[str, Dict[str, Any]] = {}
        # Embedding lane is resolved once and reused across warmup/retrieve
        # rounds — building it reloads the model + probes Chroma each time.
        self._lane: Any = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _data_dir(self) -> Path:
        from src.constants import DEEP_RESEARCH_DIR
        return Path(DEEP_RESEARCH_DIR)

    def _iter_reports(self) -> List[Dict[str, Any]]:
        """Read every report JSON on disk, filter to this user.

        A report is included when:
          - the JSON parses
          - the status is "done" (in-progress / failed reports aren't useful)
          - the on-disk ``owner`` matches (or owner is empty — legacy
            reports from before the auth gate were always single-user)
        Returns a list of dicts: {id, query, body, completed_at}.
        """
        out: List[Dict[str, Any]] = []
        data_dir = self._data_dir()
        if not data_dir.is_dir():
            return out
        try:
            files = sorted(
                data_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError as e:
            logger.warning(f"LibrarySource: cannot list {data_dir}: {e}")
            return out
        for p in files[:_MAX_REPORTS]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            # Owner filter. Empty owner on the report = legacy = include
            # only when our owner is also empty (single-user legacy).
            report_owner = (d.get("owner") or "").strip()
            if self.owner:
                if report_owner and report_owner != self.owner:
                    continue
            else:
                if report_owner:
                    continue
            if d.get("status") not in (None, "done", "completed"):
                continue
            body = (d.get("result") or "").strip()
            if not body:
                continue
            out.append({
                "id": p.stem,
                "query": d.get("query") or "",
                "body": body,
                "completed_at": d.get("completed_at") or p.stat().st_mtime,
                "path": p,  # used to detect changes
            })
        return out

    @staticmethod
    def _chunk_id(report_id: str, idx: int) -> str:
        return f"{report_id}::chunk::{idx}"

    # ------------------------------------------------------------------
    # Embedding lane (mirrors FolderSource)
    # ------------------------------------------------------------------

    def _resolve_lane(self):
        if self._lane is not None:
            return self._lane
        from src.embedding_lanes import build_embedding_lanes
        lanes = build_embedding_lanes(self.collection_name)
        healthy = [l for l in lanes if l.healthy]
        if not healthy:
            raise RuntimeError(
                f"No healthy embedding lane for LibrarySource "
                f"({self.collection_name}). Install `chromadb` + `fastembed`."
            )
        self._lane = healthy[0]
        return self._lane

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"LibrarySource warmup skipped: {e}")
            return
        coll = lane.collection

        # Reading + JSON-parsing every report off disk is blocking I/O — run
        # it in a thread so we don't stall the event loop during research.
        reports = await asyncio.to_thread(self._iter_reports)
        self._reports = {r["id"]: r for r in reports}
        if not reports:
            logger.info("LibrarySource: no reports on disk for this user")
            return

        try:
            existing = await asyncio.to_thread(coll.get, include=["metadatas"])
        except Exception as e:
            logger.warning(f"LibrarySource: chroma get() failed: {e}")
            existing = {"ids": [], "metadatas": []}

        # Index existing chunks by (report_id) so we can detect deletes + changes.
        existing_by_report: Dict[str, List[Dict[str, Any]]] = {}
        for cid, meta in zip(existing.get("ids", []), existing.get("metadatas", [])):
            if not meta:
                continue
            existing_by_report.setdefault(meta.get("report_id", ""), []).append(
                {"id": cid, **meta}
            )

        current_report_ids: Set[str] = set()
        to_upsert: List[Dict[str, Any]] = []
        for r in reports:
            rid = r["id"]
            current_report_ids.add(rid)
            # Skip if every existing chunk for this report was indexed at
            # the same completed_at we have on disk.
            prev = existing_by_report.get(rid, [])
            if prev and all(
                abs(float(m.get("completed_at") or 0) - float(r["completed_at"])) < 1e-3
                for m in prev
            ):
                continue
            to_upsert.append(r)

        # Deletes: reports on disk before but no longer present.
        to_delete_ids = [
            cid
            for rid, chunks in existing_by_report.items()
            if rid and rid not in current_report_ids
            for cid in [c.get("id") for c in chunks if c.get("id")]
        ]

        if to_delete_ids:
            try:
                await asyncio.to_thread(coll.delete, ids=to_delete_ids)
            except Exception as e:
                logger.warning(f"LibrarySource: chroma delete failed: {e}")

        if not to_upsert:
            logger.info(f"LibrarySource: {len(reports)} report(s) already up to date")
            return

        docs: List[str] = []
        metas: List[Dict[str, Any]] = []
        ids: List[str] = []
        for r in to_upsert:
            rid = r["id"]
            # Chunk the body as if it were a single long "file".
            virtual_path = Path(f"<library>/{rid}.md")
            chunks = chunk_file(
                virtual_path, r["body"], max_chars=_MAX_CHARS_PER_CHUNK,
            )[:_MAX_CHUNKS_PER_REPORT]
            for idx, ch in enumerate(chunks):
                docs.append(ch.text)
                metas.append({
                    "report_id": rid,
                    "query": r["query"][:200],
                    "completed_at": r["completed_at"],
                    "start_line": ch.start_line,
                    "end_line": ch.end_line,
                })
                ids.append(self._chunk_id(rid, idx))

        if not docs:
            return
        try:
            embeddings = await asyncio.to_thread(lane.encode, docs)
            await asyncio.to_thread(
                coll.upsert, ids=ids, documents=docs, metadatas=metas,
                embeddings=embeddings,
            )
            logger.info(
                f"LibrarySource: indexed {len(docs)} chunk(s) from "
                f"{len(to_upsert)} report(s) for user '{self.owner or 'default'}'"
            )
        except Exception as e:
            logger.error(f"LibrarySource: chroma upsert failed: {e}", exc_info=True)

    async def shutdown(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        queries: List[str],
        *,
        question: str,
        limit: int = 10,
        prior_refs: Optional[List[str]] = None,
    ) -> List[Finding]:
        # Empty selection = no findings (UX contract from the picker).
        if not self.report_ids:
            return []
        if not self._reports:
            # warmup() didn't see anything (likely no data dir) — try once now
            # so a research started without prior warmup still works.
            await self.warmup()
            if not self._reports:
                return []

        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"LibrarySource retrieve skipped: {e}")
            return []
        coll = lane.collection

        prior = set(prior_refs or [])
        allowed: Set[str] = set(self.report_ids)
        # If the user picked specific reports that no longer exist on disk,
        # gracefully return nothing rather than 500.
        allowed &= set(self._reports.keys())
        if not allowed:
            return []

        best: Dict[str, Finding] = {}
        per_query = max(limit, self.limit_per_report * len(allowed))
        for q in queries:
            try:
                q_emb = (await asyncio.to_thread(lane.encode, [q]))[0]
                res = await asyncio.to_thread(
                    coll.query,
                    query_embeddings=[q_emb],
                    n_results=per_query,
                    where={"report_id": {"$in": list(allowed)}},
                )
            except Exception as e:
                logger.warning(f"LibrarySource query failed: {e}")
                continue
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                if not meta:
                    continue
                rid = meta.get("report_id", "")
                if rid not in allowed:
                    continue
                loc = self._location(meta)
                if loc in prior:
                    continue
                score = self._score_from_distance(dist)
                f = self._wrap(doc, meta, score)
                if f.ref.location not in best or best[f.ref.location].score < f.score:
                    best[f.ref.location] = f
            if len(best) >= limit:
                break

        return sorted(best.values(), key=lambda x: x.score, reverse=True)[:limit]

    # ------------------------------------------------------------------
    # Wrapping
    # ------------------------------------------------------------------

    @staticmethod
    def _location(meta: Dict[str, Any]) -> str:
        # Synthetic but informative: library://<report_id>#L<start>-L<end>
        # The UI can resolve this to the actual report when clicked.
        rid = meta.get("report_id", "unknown")
        return (
            f"library://{rid}#L{meta.get('start_line', 1)}-L{meta.get('end_line', 1)}"
        )

    @staticmethod
    def _score_from_distance(dist: Any) -> float:
        try:
            d = float(dist)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, 1.0 - d))

    @staticmethod
    def _wrap(doc: str, meta: Dict[str, Any], score: float) -> Finding:
        rid = meta.get("report_id", "unknown")
        query = (meta.get("query") or "").strip() or rid
        # Use the first ~80 chars of the query as the title so the
        # citations list shows something readable.
        title = query[:80] + ("…" if len(query) > 80 else "")
        loc = LibrarySource._location(meta)
        return Finding(
            content=doc,
            ref=SourceRef(
                source_id="library",
                title=title,
                location=loc,
                snippet=doc[:200],
                metadata={
                    "report_id": rid,
                    "query": query,
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                },
            ),
            score=score,
            metadata={"report_id": rid, "query": query},
        )
