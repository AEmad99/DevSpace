"""`KnowledgeBaseSource` — a named, persistent corpus of folders (issue #2 / M4).

A Knowledge Base (KB) is a user-defined collection of folders with a stable
identity (`kb_id`) and persisted manifest. Multiple folders share a single
ChromaDB collection per KB, so retrieval can pull from the union while
the manifest stays easy to edit (add/remove folders via REST).

Key design choices:
  - Manifest is a JSON file under `backend/data/knowledge_bases/<kb_id>.json`.
    Easy to back up, easy to edit by hand, easy to inspect.
  - Each KB has ONE Chroma collection, named `kb_<kb_id>`. All member
    folders share this collection — chunks are differentiated by `path`
    metadata.
  - `KnowledgeBaseSource` delegates to `FolderSource` per member folder
    for actual indexing (`warmup`) and per-folder retrieval. Results
    are merged and re-ranked across folders.
  - Path safety mirrors `FolderSource` — paths are resolved and validated
    at construction; manifest entries that point to non-existent folders
    are skipped at warmup with a warning (don't crash the whole KB).
  - When a KB member is added/removed, the manifest is the source of
    truth. Re-warmup after editing a manifest picks up the change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import Finding, Source, SourceRef
from .folder import FolderSource
from .registry import registry

logger = logging.getLogger(__name__)


# Manifest schema (written to disk):
# {
#   "id": "abc123",
#   "name": "Work Notes",
#   "folders": [
#     {"path": "/abs/path1", "extensions": [".md", ".txt"], "exclude_dirs": [...]},
#     {"path": "/abs/path2"}
#   ],
#   "created_at": "2026-...",
#   "updated_at": "2026-..."
# }


def _manifests_dir() -> Path:
    """Resolve the directory where KB manifests live.

    Mirrors the pattern in routes/knowledge_base_routes.py — both call
    sites must agree on this path.
    """
    from src.constants import DEEP_RESEARCH_DIR
    d = Path(DEEP_RESEARCH_DIR).parent / "knowledge_bases"
    d.mkdir(parents=True, exist_ok=True)
    return d


@registry.register
class KnowledgeBaseSource(Source):
    """Research over a named, persistent Knowledge Base.

    Config keys:
        kb_id   (str, required)   the KB's identifier (see /api/knowledge_bases)
        limit_per_folder (int)    per-folder retrieval cap before merging
                                   (default 5 — KBs typically have many
                                   folders, so we want each to contribute a
                                   few strong hits)
    """
    type_id = "kb"
    display_name = "Knowledge Base"
    config_schema = {
        "kb_id": {"type": "string", "required": True},
        "limit_per_folder": {"type": "integer", "default": 5,
                             "minimum": 1, "maximum": 50},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.kb_id = self.config.get("kb_id")
        if not self.kb_id:
            raise ValueError("KnowledgeBaseSource requires 'kb_id' in config")
        self.limit_per_folder: int = int(self.config.get("limit_per_folder", 5))
        self.manifest: Dict[str, Any] = self._load_manifest()
        self._member_sources: List[FolderSource] = self._build_member_sources()

    # ------------------------------------------------------------------
    # Manifest I/O
    # ------------------------------------------------------------------

    # KB ids are minted via secrets.token_urlsafe(8) → URL-safe base64 alphabet.
    _KB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    @classmethod
    def manifest_path(cls, kb_id: str) -> Path:
        # SECURITY: kb_id arrives from a research `source` spec config and is
        # concatenated into a file path. Reject anything outside the mint
        # alphabet so a crafted kb_id like "../../vault" can't read an
        # arbitrary JSON outside the manifests dir.
        if not isinstance(kb_id, str) or not cls._KB_ID_RE.match(kb_id):
            raise ValueError(f"Invalid knowledge base id: {kb_id!r}")
        return _manifests_dir() / f"{kb_id}.json"

    def _load_manifest(self) -> Dict[str, Any]:
        p = self.manifest_path(self.kb_id)
        if not p.exists():
            raise FileNotFoundError(
                f"Knowledge base '{self.kb_id}' not found at {p}. "
                "Create it via POST /api/knowledge_bases first."
            )
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"KB manifest for '{self.kb_id}' is corrupt: {e}")
        # Lightweight validation; raises if obviously wrong.
        if not isinstance(data.get("folders"), list):
            raise ValueError(f"KB manifest '{self.kb_id}' missing 'folders' list")
        return data

    def _build_member_sources(self) -> List[FolderSource]:
        out: List[FolderSource] = []
        for entry in self.manifest.get("folders", []):
            if not isinstance(entry, dict) or not entry.get("path"):
                logger.warning(f"KB '{self.kb_id}': skipping malformed folder entry: {entry!r}")
                continue
            try:
                # Each member folder gets its OWN collection (so deletions
                # from one folder don't affect the others' chunks). The KB
                # shares its identity only at the manifest + display layer.
                cfg = dict(entry)
                cfg.setdefault("collection_name", self._member_collection_name(entry["path"]))
                src = FolderSource(cfg)
                out.append(src)
            except (FileNotFoundError, NotADirectoryError, ValueError) as e:
                logger.warning(f"KB '{self.kb_id}': skipping folder {entry.get('path')}: {e}")
        return out

    def _member_collection_name(self, path: str) -> str:
        # One Chroma collection per (kb_id, member path). Lets us reindex
        # one member without touching the others.
        h = hashlib.sha1(f"{self.kb_id}::{path}".encode("utf-8")).hexdigest()[:10]
        safe = "".join(c if c.isalnum() else "_" for c in Path(path).name)[:24] or "root"
        return f"kb_{self.kb_id}_{safe}_{h}"

    # ------------------------------------------------------------------
    # Lifecycle — delegate to members
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """Index every member folder. Failures in one folder don't abort others."""
        for src in self._member_sources:
            try:
                await src.warmup()
            except Exception as e:
                logger.warning(f"KB '{self.kb_id}' warmup: folder "
                               f"{src.root} failed: {e}")

    async def shutdown(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Retrieval — merge across member folders
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        queries: List[str],
        *,
        question: str,
        limit: int = 10,
        prior_refs: Optional[List[str]] = None,
    ) -> List[Finding]:
        """Pull `limit_per_folder` findings from each member, dedupe, rank."""
        prior = set(prior_refs or [])
        best: Dict[str, Finding] = {}
        for src in self._member_sources:
            try:
                member_findings = await src.retrieve(
                    queries, question=question,
                    limit=self.limit_per_folder, prior_refs=list(prior),
                )
            except Exception as e:
                logger.warning(f"KB '{self.kb_id}' retrieve: folder {src.root} failed: {e}")
                continue
            for f in member_findings:
                if f.ref.location in prior:
                    continue
                key = f.ref.location   # file://path#Lstart-Lend — unique per chunk
                if key not in best or best[key].score < f.score:
                    best[key] = f
            if len(best) >= limit:
                break

        return sorted(best.values(), key=lambda x: x.score, reverse=True)[:limit]

    # ------------------------------------------------------------------
    # Introspection helpers (used by routes + UI)
    # ------------------------------------------------------------------

    @property
    def display_label(self) -> str:
        return self.manifest.get("name") or self.kb_id

    def describe(self) -> Dict[str, Any]:
        return {
            "type": self.type_id,
            "name": f"KB: {self.display_label}",
            "id": self.kb_id,
            "folders": [str(s.root) for s in self._member_sources],
            "config_schema": self.config_schema,
        }
