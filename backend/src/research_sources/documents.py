"""`DocumentsSource` — research over the user's saved documents.

Walks every active (non-archived) document belonging to the current user
— the same set shown under the Library's **Documents** tab — embeds each
document's content into a per-user ChromaDB collection, and surfaces
relevant chunks when the agent asks for evidence. Like
``PreviousChatsSource`` (and unlike ``LibrarySource``), this source has
NO selection config: it always searches ALL of the user's documents.

Design choices:
  - Per-user collection (``documents_<user_slug>``) so two users on the
    same server never share evidence. Empty owner → ``documents_default``.
  - One chunk per ~1500 chars of the document body (same prose chunker as
    the other sources, so citations share the file://...#Lstart-Lend shape).
  - Incremental warmup keyed on each document's ``updated_at`` timestamp —
    re-embeds only documents edited since the last warmup, and drops chunks
    for documents that were deleted/archived.
  - Mirrors the Library's Documents view: only ``is_active`` and
    non-archived documents owned by the user are indexed.
  - When no DB engine is available (very early startup, tests without a
    real DB), the source no-ops with a clear log instead of crashing.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import Finding, Source, SourceRef
from .chunker import chunk_file
from .library import _slugify_user   # reuse the same slugifier
from .registry import registry

logger = logging.getLogger(__name__)

_MAX_CHARS_PER_CHUNK = 1500
_MAX_CHUNKS_PER_DOC = 2000    # safety cap; a very long document caps here
_MAX_DOCUMENTS = 5000         # safety cap; typical user has far fewer


@registry.register
class DocumentsSource(Source):
    """Research over the user's documents (Library → Documents tab).

    No selection config. The source always searches every active,
    non-archived document that belongs to ``config.owner`` (or every
    such document when owner is empty in single-user / auth-disabled mode).
    """
    type_id = "documents"
    display_name = "Documents"
    config_schema = {
        "owner": {"type": "string", "default": "", "required": True},
        # Hard cap on returned findings across all documents — keeps one
        # long document from drowning the others. Mirrors the other sources.
        "limit_per_doc": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.owner: str = (self.config.get("owner") or "").strip()
        self.limit_per_doc: int = max(1, int(self.config.get("limit_per_doc", 3)))
        self.collection_name: str = f"documents_{_slugify_user(self.owner)}"
        # Cached during warmup so retrieve() doesn't re-walk the DB.
        self._doc_ids: List[str] = []
        # Embedding lane is resolved once and reused across warmup/retrieve
        # rounds — building it reloads the model + probes Chroma each time.
        self._lane: Any = None

    # ------------------------------------------------------------------
    # DB access
    # ------------------------------------------------------------------

    def _load_documents(self) -> List[Dict[str, Any]]:
        """Read every active, non-archived document for the current user.

        Returns a list of dicts: {id, title, updated_at, content}.
        Mirrors the Library's Documents view filter:
          - is_active == True (shown in the Documents library)
          - not archived (the user explicitly hid those)
          - owned by ``self.owner`` (+ null-owner legacy rows)
        """
        try:
            from core.database import Document as DbDocument, SessionLocal
        except Exception as e:
            logger.warning(f"DocumentsSource: cannot import DB models: {e}")
            return []

        try:
            db = SessionLocal()
        except Exception as e:
            logger.warning(f"DocumentsSource: cannot open DB session: {e}")
            return []

        out: List[Dict[str, Any]] = []
        try:
            q = db.query(DbDocument).filter(DbDocument.is_active == True)
            # Exclude archived docs; NULL archived = legacy = not archived.
            q = q.filter((DbDocument.archived == False) | (DbDocument.archived.is_(None)))
            if self.owner:
                # Owner filter: this user's rows + null-owner legacy rows.
                q = q.filter((DbDocument.owner == self.owner) | (DbDocument.owner.is_(None)))
            else:
                # Single-user: include only null-owner rows.
                q = q.filter(DbDocument.owner.is_(None))
            q = q.order_by(DbDocument.updated_at.desc().nullslast())
            rows = q.limit(_MAX_DOCUMENTS).all()
            for d in rows:
                content = (d.current_content or "").strip()
                if not content:
                    continue
                out.append({
                    "id": d.id,
                    "title": d.title or d.id,
                    "updated_at": d.updated_at,
                    "content": content,
                })
        except Exception as e:
            logger.warning(f"DocumentsSource: DB query failed: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------
    # Embedding lane (mirrors PreviousChatsSource)
    # ------------------------------------------------------------------

    def _resolve_lane(self):
        if self._lane is not None:
            return self._lane
        from src.embedding_lanes import build_embedding_lanes
        lanes = build_embedding_lanes(self.collection_name)
        healthy = [l for l in lanes if l.healthy]
        if not healthy:
            raise RuntimeError(
                f"No healthy embedding lane for DocumentsSource "
                f"({self.collection_name}). Install `chromadb` + `fastembed`."
            )
        self._lane = healthy[0]
        return self._lane

    @staticmethod
    def _chunk_id(doc_id: str, idx: int) -> str:
        return f"{doc_id}::chunk::{idx}"

    @staticmethod
    def _ts_to_epoch(ts: Any) -> float:
        try:
            if ts is None:
                return 0.0
            # datetime → epoch
            if hasattr(ts, "timestamp"):
                return float(ts.timestamp())
            return float(ts)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        # Walking the DB is blocking I/O — keep it off the event loop so
        # research doesn't stall the rest of the server.
        documents = await asyncio.to_thread(self._load_documents)
        self._doc_ids = [d["id"] for d in documents]
        if not documents:
            logger.info("DocumentsSource: no documents for this user")
            return
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"DocumentsSource warmup skipped: {e}")
            return
        coll = lane.collection

        try:
            existing = await asyncio.to_thread(coll.get, include=["metadatas"])
        except Exception as e:
            logger.warning(f"DocumentsSource: chroma get() failed: {e}")
            existing = {"ids": [], "metadatas": []}

        existing_by_doc: Dict[str, List[Dict[str, Any]]] = {}
        for cid, meta in zip(existing.get("ids", []), existing.get("metadatas", [])):
            if not meta:
                continue
            existing_by_doc.setdefault(meta.get("doc_id", ""), []).append(
                {"id": cid, **meta}
            )

        current_doc_ids: Set[str] = set()
        to_upsert: List[Dict[str, Any]] = []
        for d in documents:
            did = d["id"]
            current_doc_ids.add(did)
            updated = self._ts_to_epoch(d.get("updated_at"))
            prev = existing_by_doc.get(did, [])
            if prev and all(
                abs(float(m.get("updated_at") or 0) - updated) < 1e-3
                for m in prev
            ):
                continue
            to_upsert.append(d)

        # Documents that disappeared (deleted/archived) — remove their chunks.
        to_delete_ids = [
            cid
            for did, chunks in existing_by_doc.items()
            if did and did not in current_doc_ids
            for cid in [c.get("id") for c in chunks if c.get("id")]
        ]
        if to_delete_ids:
            try:
                await asyncio.to_thread(coll.delete, ids=to_delete_ids)
            except Exception as e:
                logger.warning(f"DocumentsSource: chroma delete failed: {e}")

        if not to_upsert:
            logger.info(f"DocumentsSource: {len(documents)} document(s) up to date")
            return

        docs: List[str] = []
        metas: List[Dict[str, Any]] = []
        ids: List[str] = []
        for d in to_upsert:
            did = d["id"]
            virtual_path = Path(f"<documents>/{did}.md")
            chunks = chunk_file(
                virtual_path, d["content"], max_chars=_MAX_CHARS_PER_CHUNK,
            )[:_MAX_CHUNKS_PER_DOC]
            updated = self._ts_to_epoch(d.get("updated_at"))
            for idx, ch in enumerate(chunks):
                docs.append(ch.text)
                metas.append({
                    "doc_id": did,
                    "title": (d.get("title") or did)[:200],
                    "updated_at": updated,
                    "start_line": ch.start_line,
                    "end_line": ch.end_line,
                })
                ids.append(self._chunk_id(did, idx))

        if not docs:
            return
        try:
            embeddings = await asyncio.to_thread(lane.encode, docs)
            await asyncio.to_thread(
                coll.upsert, ids=ids, documents=docs, metadatas=metas,
                embeddings=embeddings,
            )
            logger.info(
                f"DocumentsSource: indexed {len(docs)} chunk(s) from "
                f"{len(to_upsert)} document(s) for user '{self.owner or 'default'}'"
            )
        except Exception as e:
            logger.error(f"DocumentsSource: chroma upsert failed: {e}", exc_info=True)

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
        if not self._doc_ids:
            await self.warmup()
            if not self._doc_ids:
                return []
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"DocumentsSource retrieve skipped: {e}")
            return []

        prior = set(prior_refs or [])
        best: Dict[str, Finding] = {}
        per_query = max(limit, self.limit_per_doc * len(self._doc_ids))
        for q in queries:
            try:
                q_emb = (await asyncio.to_thread(lane.encode, [q]))[0]
                res = await asyncio.to_thread(coll_query, lane, q_emb, per_query)
            except Exception as e:
                logger.warning(f"DocumentsSource query failed: {e}")
                continue
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                if not meta:
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
        did = meta.get("doc_id", "unknown")
        return (
            f"document://{did}#L{meta.get('start_line', 1)}-L{meta.get('end_line', 1)}"
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
        did = meta.get("doc_id", "unknown")
        title = (meta.get("title") or did)[:80]
        loc = DocumentsSource._location(meta)
        return Finding(
            content=doc,
            ref=SourceRef(
                source_id="documents",
                title=title,
                location=loc,
                snippet=doc[:200],
                metadata={
                    "doc_id": did,
                    "title": title,
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                },
            ),
            score=score,
            metadata={"doc_id": did, "title": title},
        )


def coll_query(lane, q_emb, n_results: int) -> Dict[str, Any]:
    """Thin wrapper so tests can monkeypatch the collection call."""
    return lane.collection.query(query_embeddings=[q_emb], n_results=n_results)
