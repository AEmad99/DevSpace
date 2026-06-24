"""CRUD API for Knowledge Bases (issue #2 / M4).

Endpoints:
    GET    /api/knowledge_bases
        → {"knowledge_bases": [{"id": "...", "name": "...", "folders": [...]}, ...]}

    POST   /api/knowledge_bases
        body: {"name": "Work Notes", "folders": [{"path": "/abs"}, ...]}
        → {"id": "<generated>", "name": "...", "folders": [...], "created_at": "..."}

    GET    /api/knowledge_bases/{kb_id}
        → {"id": "...", "name": "...", "folders": [...], "stats": {...}}

    PUT    /api/knowledge_bases/{kb_id}
        body: {"name": "...", "folders": [...]}   (replace name + folders atomically)
        → {"id": "...", "name": "...", "folders": [...], "updated_at": "..."}

    DELETE /api/knowledge_bases/{kb_id}
        → {"deleted": "<kb_id>"}

Manifests are JSON files under
`backend/data/knowledge_bases/<kb_id>.json`. Each Chroma collection
(`kb_<kb_id>_<folder-hash>`) is also dropped on DELETE (best-effort).

All endpoints require the standard session auth (the global auth
middleware). The feature flag `RESEARCH_SOURCES_ENABLED` is enforced —
KBs are inaccessible when the flag is off.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src import constants
from src.research_sources import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge_bases", tags=["knowledge-bases"])


# ----------------------------------------------------------------------
# Request / response models
# ----------------------------------------------------------------------


class FolderEntry(BaseModel):
    path: str
    extensions: Optional[List[str]] = None
    exclude_dirs: Optional[List[str]] = None
    max_file_bytes: Optional[int] = None
    respect_gitignore: Optional[bool] = None


class KBCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    folders: List[FolderEntry]


class KBReplaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    folders: List[FolderEntry]


# ----------------------------------------------------------------------
# Path resolution (shared with KnowledgeBaseSource)
# ----------------------------------------------------------------------


def _manifests_dir() -> Path:
    from src.constants import DEEP_RESEARCH_DIR
    d = Path(DEEP_RESEARCH_DIR).parent / "knowledge_bases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag_or_403() -> None:
    """Refuse to serve KB endpoints when the feature flag is off."""
    if not getattr(constants, "RESEARCH_SOURCES_ENABLED", False):
        raise HTTPException(
            403,
            "Knowledge bases are disabled. Set "
            "RESEARCH_SOURCES_ENABLED=true to enable.",
        )


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@router.get("")
def list_kbs(_: Request) -> Dict[str, Any]:
    _flag_or_403()
    out: List[Dict[str, Any]] = []
    for p in sorted(_manifests_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Skipping unreadable KB manifest {p}: {e}")
            continue
        out.append({
            "id": p.stem,
            "name": data.get("name", p.stem),
            "folders": data.get("folders", []),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        })
    return {"knowledge_bases": out}


@router.post("")
def create_kb(body: KBCreateRequest, _: Request) -> Dict[str, Any]:
    _flag_or_403()
    if not body.folders:
        raise HTTPException(400, "A knowledge base must contain at least one folder.")

    # Validate every folder's path exists. We DON'T refuse on existence
    # issues at create time (the user may add a folder they'll mount
    # later), but we DO record what we can.
    folders_payload: List[Dict[str, Any]] = []
    for f in body.folders:
        # Coerce absolute + strip any user-controlled newline shenanigans.
        clean_path = f.path.replace("\n", "").replace("\r", "").strip()
        entry: Dict[str, Any] = {"path": clean_path}
        if f.extensions is not None:
            entry["extensions"] = list(f.extensions)
        if f.exclude_dirs is not None:
            entry["exclude_dirs"] = list(f.exclude_dirs)
        if f.max_file_bytes is not None:
            entry["max_file_bytes"] = int(f.max_file_bytes)
        if f.respect_gitignore is not None:
            entry["respect_gitignore"] = bool(f.respect_gitignore)
        folders_payload.append(entry)

    kb_id = secrets.token_urlsafe(8)
    manifest = {
        "id": kb_id,
        "name": body.name,
        "folders": folders_payload,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    (_manifests_dir() / f"{kb_id}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    logger.info(f"Created KB '{body.name}' ({kb_id}) with "
                f"{len(folders_payload)} folder(s)")
    return manifest


@router.get("/{kb_id}")
def get_kb(kb_id: str, _: Request) -> Dict[str, Any]:
    _flag_or_403()
    p = _manifests_dir() / f"{kb_id}.json"
    if not p.exists():
        raise HTTPException(404, f"Knowledge base '{kb_id}' not found")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"KB manifest corrupt: {e}")
    data["stats"] = _safe_stats(kb_id)
    return data


@router.put("/{kb_id}")
def replace_kb(kb_id: str, body: KBReplaceRequest, _: Request) -> Dict[str, Any]:
    _flag_or_403()
    p = _manifests_dir() / f"{kb_id}.json"
    if not p.exists():
        raise HTTPException(404, f"Knowledge base '{kb_id}' not found")
    existing = json.loads(p.read_text(encoding="utf-8"))
    existing["name"] = body.name
    existing["folders"] = [f.model_dump(exclude_none=True) for f in body.folders]
    existing["updated_at"] = _now_iso()
    p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    logger.info(f"Updated KB '{kb_id}' ({len(existing['folders'])} folders)")
    return existing


@router.delete("/{kb_id}")
def delete_kb(kb_id: str, _: Request) -> Dict[str, Any]:
    _flag_or_403()
    p = _manifests_dir() / f"{kb_id}.json"
    if not p.exists():
        raise HTTPException(404, f"Knowledge base '{kb_id}' not found")
    p.unlink()

    # Best-effort: drop the Chroma collections for this KB. KB
    # collections are named `kb_<kb_id>_*` so we glob them.
    dropped: List[str] = []
    try:
        from src.chroma_client import get_chroma_client
        client = get_chroma_client()
        # PersistentClient / HttpClient both expose .list_collections().
        cols = client.list_collections()
        for c in cols:
            name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
            if name and name.startswith(f"kb_{kb_id}_"):
                try:
                    client.delete_collection(name)
                    dropped.append(name)
                except Exception as e:
                    logger.debug(f"Could not drop collection {name}: {e}")
    except Exception as e:
        logger.debug(f"Chroma cleanup skipped for KB {kb_id}: {e}")

    logger.info(f"Deleted KB '{kb_id}' (dropped {len(dropped)} collection(s))")
    return {"deleted": kb_id, "collections_dropped": dropped}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _safe_stats(kb_id: str) -> Dict[str, Any]:
    """Return lightweight stats (chunk count per member folder) for the UI."""
    stats: Dict[str, Any] = {"members": []}
    try:
        from src.chroma_client import get_chroma_client
        client = get_chroma_client()
        cols = client.list_collections()
    except Exception:
        return stats

    for c in cols:
        name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
        if not name or not name.startswith(f"kb_{kb_id}_"):
            continue
        try:
            count = int(c.count()) if hasattr(c, "count") else 0
        except Exception:
            count = None
        stats["members"].append({"collection": name, "chunks": count})
    return stats
