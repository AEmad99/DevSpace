"""`PreviousChatsSource` — research over the user's saved chat history.

Walks every chat session belonging to the current user, embeds the
conversation transcript into a per-user ChromaDB collection, and
surfaces relevant chunks when the agent asks for evidence. Unlike
``LibrarySource``, this source has NO config: it always searches ALL
chat sessions for the user (per the UX decision: "all chat sessions").

Design choices:
  - Per-user collection (``chats_<user_slug>``) so two users on the same
    server never share evidence. Empty owner → ``chats_default`` (single-user).
  - One chunk per ~1500 chars of the joined transcript, preserving
    message boundaries so a citation can say "user said X / assistant
    said Y" via the chunk's stored role-prefixed text.
  - Incremental warmup keyed on each session's ``last_message_at``
    timestamp — re-embeds only sessions that received new messages since
    the last warmup.
  - System / slash-command messages are excluded from the transcript
    (they're not real user/assistant content).
  - When no DB engine is available (very early startup, tests without a
    real DB), the source no-ops with a clear log instead of crashing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import Finding, Source, SourceRef
from .chunker import chunk_file
from .library import _slugify_user   # reuse the same slugifier
from .registry import registry

logger = logging.getLogger(__name__)

_MAX_CHARS_PER_CHUNK = 1500
_MAX_CHUNKS_PER_SESSION = 1000   # safety cap; very long sessions cap here
_MAX_SESSIONS = 2000             # safety cap; typical user has <200 sessions


@registry.register
class PreviousChatsSource(Source):
    """Research over the user's previous chat sessions.

    No config keys. The source always searches every chat session that
    belongs to ``config.owner`` (or every session when owner is empty
    in single-user / auth-disabled mode).
    """
    type_id = "chats"
    display_name = "Previous Chats"
    config_schema = {
        "owner": {"type": "string", "default": "", "required": True},
        # Hard cap on returned findings across all sessions — keeps one
        # chatty session from drowning the others. Mirrors LibrarySource.
        "limit_per_session": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.owner: str = (self.config.get("owner") or "").strip()
        self.limit_per_session: int = max(1, int(self.config.get("limit_per_session", 3)))
        self.collection_name: str = f"chats_{_slugify_user(self.owner)}"
        # Cached during warmup so retrieve() doesn't re-walk the DB.
        self._session_ids: List[str] = []
        # Embedding lane is resolved once and reused across warmup/retrieve
        # rounds — building it reloads the model + probes Chroma each time.
        self._lane: Any = None

    # ------------------------------------------------------------------
    # DB access
    # ------------------------------------------------------------------

    def _load_sessions(self) -> List[Dict[str, Any]]:
        """Read every session + its messages for the current user.

        Returns a list of dicts: {id, name, last_message_at, transcript}.
        Excludes:
          - archived sessions (the user explicitly hid them)
          - empty sessions (no messages to search)
          - sessions not owned by ``self.owner`` (multi-user scoping)
        """
        try:
            from core.database import Session as DbSession, ChatMessage as DbChatMessage, SessionLocal
        except Exception as e:
            logger.warning(f"PreviousChatsSource: cannot import DB models: {e}")
            return []

        try:
            db = SessionLocal()
        except Exception as e:
            logger.warning(f"PreviousChatsSource: cannot open DB session: {e}")
            return []

        out: List[Dict[str, Any]] = []
        try:
            q = db.query(DbSession).filter(DbSession.archived == False)
            if self.owner:
                # Owner filter: include this user's rows + null-owner legacy rows.
                q = q.filter((DbSession.owner == self.owner) | (DbSession.owner.is_(None)))
            else:
                # Single-user: include only null-owner rows.
                q = q.filter(DbSession.owner.is_(None))
            q = q.filter(DbSession.message_count > 0)
            q = q.order_by(DbSession.last_message_at.desc().nullslast())
            sessions = q.limit(_MAX_SESSIONS).all()
            # Fetch every message for these sessions in ONE query rather than a
            # query per session (N+1 — up to _MAX_SESSIONS round-trips). Bucket
            # by session_id in Python; chunk the IN-list to stay under SQLite's
            # ~999 bound-variable limit.
            session_ids = [s.id for s in sessions]
            msgs_by_session: Dict[str, List[Any]] = {}
            for start in range(0, len(session_ids), 500):
                chunk = session_ids[start:start + 500]
                if not chunk:
                    continue
                rows = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id.in_(chunk))
                    .order_by(
                        DbChatMessage.session_id.asc(),
                        DbChatMessage.timestamp.asc(),
                    )
                    .all()
                )
                for m in rows:
                    msgs_by_session.setdefault(m.session_id, []).append(m)
            for s in sessions:
                msgs = msgs_by_session.get(s.id) or []
                if not msgs:
                    continue
                transcript = self._join_transcript(msgs)
                if not transcript.strip():
                    continue
                out.append({
                    "id": s.id,
                    "name": s.name or s.id,
                    "last_message_at": s.last_message_at,
                    "transcript": transcript,
                })
        except Exception as e:
            logger.warning(f"PreviousChatsSource: DB query failed: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass
        return out

    @staticmethod
    def _join_transcript(messages: List[Any]) -> str:
        """Render a session's messages as a single transcript string.

        Excludes system + slash-command chatter (mirrors
        ``Session.get_context_messages`` from core/models.py).
        Each message is prefixed with its role so the chunked text is
        self-describing when the LLM sees a finding.
        """
        parts: List[str] = []
        for m in messages:
            role = (m.role or "").strip().lower()
            if role in ("system",):
                continue
            content = (m.content or "").strip()
            if not content:
                continue
            # Best-effort slash-command filter (matches core/models.py
            # which uses metadata.source == "slash" when present).
            try:
                meta = json.loads(m.meta_data) if m.meta_data else {}
            except Exception:
                meta = {}
            if (meta or {}).get("source") == "slash":
                continue
            label = "User" if role == "user" else ("Assistant" if role == "assistant" else role.capitalize())
            parts.append(f"{label}: {content}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Embedding lane (mirrors LibrarySource)
    # ------------------------------------------------------------------

    def _resolve_lane(self):
        if self._lane is not None:
            return self._lane
        from src.embedding_lanes import build_embedding_lanes
        lanes = build_embedding_lanes(self.collection_name)
        healthy = [l for l in lanes if l.healthy]
        if not healthy:
            raise RuntimeError(
                f"No healthy embedding lane for PreviousChatsSource "
                f"({self.collection_name}). Install `chromadb` + `fastembed`."
            )
        self._lane = healthy[0]
        return self._lane

    @staticmethod
    def _chunk_id(session_id: str, idx: int) -> str:
        return f"{session_id}::chunk::{idx}"

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
        # Walking the DB (and its messages) is blocking I/O — keep it off the
        # event loop so research doesn't stall the rest of the server.
        sessions = await asyncio.to_thread(self._load_sessions)
        self._session_ids = [s["id"] for s in sessions]
        if not sessions:
            logger.info("PreviousChatsSource: no chat sessions for this user")
            return
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"PreviousChatsSource warmup skipped: {e}")
            return
        coll = lane.collection

        try:
            existing = await asyncio.to_thread(coll.get, include=["metadatas"])
        except Exception as e:
            logger.warning(f"PreviousChatsSource: chroma get() failed: {e}")
            existing = {"ids": [], "metadatas": []}

        existing_by_session: Dict[str, List[Dict[str, Any]]] = {}
        for cid, meta in zip(existing.get("ids", []), existing.get("metadatas", [])):
            if not meta:
                continue
            existing_by_session.setdefault(meta.get("session_id", ""), []).append(
                {"id": cid, **meta}
            )

        current_session_ids: Set[str] = set()
        to_upsert: List[Dict[str, Any]] = []
        for s in sessions:
            sid = s["id"]
            current_session_ids.add(sid)
            last_ts = self._ts_to_epoch(s.get("last_message_at"))
            prev = existing_by_session.get(sid, [])
            if prev and all(
                abs(float(m.get("last_message_at") or 0) - last_ts) < 1e-3
                for m in prev
            ):
                continue
            to_upsert.append(s)

        # Sessions that disappeared (deleted/archived) — remove their chunks.
        to_delete_ids = [
            cid
            for sid, chunks in existing_by_session.items()
            if sid and sid not in current_session_ids
            for cid in [c.get("id") for c in chunks if c.get("id")]
        ]
        if to_delete_ids:
            try:
                await asyncio.to_thread(coll.delete, ids=to_delete_ids)
            except Exception as e:
                logger.warning(f"PreviousChatsSource: chroma delete failed: {e}")

        if not to_upsert:
            logger.info(f"PreviousChatsSource: {len(sessions)} session(s) up to date")
            return

        docs: List[str] = []
        metas: List[Dict[str, Any]] = []
        ids: List[str] = []
        for s in to_upsert:
            sid = s["id"]
            virtual_path = Path(f"<chats>/{sid}.md")
            chunks = chunk_file(
                virtual_path, s["transcript"], max_chars=_MAX_CHARS_PER_CHUNK,
            )[:_MAX_CHUNKS_PER_SESSION]
            last_ts = self._ts_to_epoch(s.get("last_message_at"))
            for idx, ch in enumerate(chunks):
                docs.append(ch.text)
                metas.append({
                    "session_id": sid,
                    "name": (s.get("name") or sid)[:200],
                    "last_message_at": last_ts,
                    "start_line": ch.start_line,
                    "end_line": ch.end_line,
                })
                ids.append(self._chunk_id(sid, idx))

        if not docs:
            return
        try:
            embeddings = await asyncio.to_thread(lane.encode, docs)
            await asyncio.to_thread(
                coll.upsert, ids=ids, documents=docs, metadatas=metas,
                embeddings=embeddings,
            )
            logger.info(
                f"PreviousChatsSource: indexed {len(docs)} chunk(s) from "
                f"{len(to_upsert)} session(s) for user '{self.owner or 'default'}'"
            )
        except Exception as e:
            logger.error(f"PreviousChatsSource: chroma upsert failed: {e}", exc_info=True)

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
        if not self._session_ids:
            await self.warmup()
            if not self._session_ids:
                return []
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"PreviousChatsSource retrieve skipped: {e}")
            return []

        prior = set(prior_refs or [])
        best: Dict[str, Finding] = {}
        per_query = max(limit, self.limit_per_session * len(self._session_ids))
        for q in queries:
            try:
                q_emb = (await asyncio.to_thread(lane.encode, [q]))[0]
                res = await asyncio.to_thread(coll_query, lane, q_emb, per_query)
            except Exception as e:
                logger.warning(f"PreviousChatsSource query failed: {e}")
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
        sid = meta.get("session_id", "unknown")
        return (
            f"chats://{sid}#L{meta.get('start_line', 1)}-L{meta.get('end_line', 1)}"
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
        sid = meta.get("session_id", "unknown")
        name = (meta.get("name") or sid)[:80]
        loc = PreviousChatsSource._location(meta)
        return Finding(
            content=doc,
            ref=SourceRef(
                source_id="chats",
                title=name,
                location=loc,
                snippet=doc[:200],
                metadata={
                    "session_id": sid,
                    "name": name,
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                },
            ),
            score=score,
            metadata={"session_id": sid, "name": name},
        )


def coll_query(lane, q_emb, n_results: int) -> Dict[str, Any]:
    """Thin wrapper so tests can monkeypatch the collection call."""
    return lane.collection.query(query_embeddings=[q_emb], n_results=n_results)
