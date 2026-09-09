import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import chromadb

from mr_data.config import settings
from mr_data.embeddings import BGEMemoryEmbedding, NomicPersonalityEmbedding
from mr_data.logging import get_logger
from mr_data.models import PersonalityEvent

logger = get_logger("mr_data.chroma")


def _stable_doc_id(kind: str, *parts: str) -> str:
    """Derive a deterministic document id from the natural key parts of a write.

    All id derivation rules for Chroma writes live here so that idempotency
    semantics are defined in one place. Ids are prefixed by kind to avoid
    cross-type collisions (e.g. "evidence:<sha256>").
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def evidence_doc_id(dialogue_log_id: Optional[int], dimension_id: int, snippet: str) -> str:
    """Stable id for an evidence snippet persisted to the personality collection."""
    return _stable_doc_id("evidence", str(dialogue_log_id), str(dimension_id), snippet)


def event_doc_id(dialogue_log_id: Optional[int], dimension_id: int, summary: str) -> str:
    """Stable id for an event summary persisted to the personality collection."""
    return _stable_doc_id("event", str(dialogue_log_id), str(dimension_id), summary)


def line_doc_id(speaker: Optional[str], content: str, context: Optional[str] = None) -> str:
    """Stable id for an ingested sample line persisted to the personality collection."""
    return _stable_doc_id("line", speaker or "assistant", content, context or "")


def dialogue_chunk_memory_id(
    session_id: str, first_log_id, last_log_id, content: str
) -> str:
    """Stable id for a dialogue chunk persisted to the memory collection."""
    return _stable_doc_id(
        "dialogue", session_id, str(first_log_id), str(last_log_id), content
    )


class ChromaStore:
    def __init__(
        self,
        persist_dir: Optional[str] = None,
        personality_embedding_fn: Optional = None,
        memory_embedding_fn: Optional = None,
        ephemeral: bool = False,
    ):
        if ephemeral:
            # In-memory mode (used by tests): no persistence directory involved.
            # chromadb EphemeralClient is a process-wide singleton (SharedSystemClient),
            # so drop leftover collections from previous instances to keep isolation.
            self.persist_dir = None
            self._client = chromadb.EphemeralClient()
            for name in ("personality", "memories"):
                try:
                    self._client.delete_collection(name)
                except Exception:
                    pass
        else:
            self.persist_dir = Path(persist_dir or settings.chroma_persist_dir)
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))

        self._personality_embedding_fn = personality_embedding_fn or NomicPersonalityEmbedding(
            model_name=settings.personality_embedding_model,
            dim=settings.personality_embedding_dim,
        )
        self._memory_embedding_fn = memory_embedding_fn or BGEMemoryEmbedding(
            model_name=settings.memory_embedding_model,
        )

        # Trigger collection initialization / migration checks.
        _ = self.personality
        _ = self.memories

    def _export_collection_backup(self, coll) -> Optional[Path]:
        """Export a collection's documents/metadatas to a JSON backup before it is deleted.

        Embeddings are intentionally not exported: a recreate happens because the
        embedding model/dim changed, so old vectors are meaningless and embeddings
        are recomputed on restore. Ephemeral mode (no persist_dir) skips exporting.
        Any export failure raises RuntimeError so the caller will NOT delete the
        collection (fail-safe: losing the backup is worse than keeping old data).
        """
        if self.persist_dir is None:
            return None
        data = coll.get(include=["documents", "metadatas"])
        documents = [
            {"id": doc_id, "document": document, "metadata": metadata}
            for doc_id, document, metadata in zip(
                data.get("ids", []), data.get("documents", []), data.get("metadatas", [])
            )
        ]
        backup_dir = self.persist_dir.parent / "chroma-backups"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup_path = backup_dir / f"{coll.name}-{timestamp}.json"
        payload = {
            "collection": coll.name,
            "collection_metadata": coll.metadata,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(documents),
            "documents": documents,
        }
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to export backup for Chroma collection {coll.name!r} to {backup_path}; "
                "refusing to delete the collection."
            ) from exc
        logger.info(
            "Exported Chroma collection backup before recreate",
            extra={
                "event": "chroma.backup_exported",
                "details": {
                    "collection": coll.name,
                    "path": str(backup_path),
                    "count": len(documents),
                },
            },
        )
        return backup_path

    def _ensure_collection(self, name: str, dim: int, model_name: str):
        backup_path = None
        backup_count = 0
        try:
            coll = self._client.get_collection(name)
        except Exception:
            coll = None

        if coll is not None:
            meta = coll.metadata or {}
            existing_dim = meta.get("embedding_dim")
            existing_model = meta.get("embedding_model")
            if existing_dim is None or existing_dim != dim or existing_model != model_name:
                if settings.chroma_recreate_on_mismatch:
                    # Export a backup FIRST; if it fails, the RuntimeError propagates
                    # and delete_collection below is never reached.
                    backup_path = self._export_collection_backup(coll)
                    if backup_path is not None:
                        backup_count = coll.count()
                    logger.warning(
                        "Recreating Chroma collection due to embedding mismatch",
                        extra={
                            "event": "chroma.recreate",
                            "details": {
                                "collection": name,
                                "existing_dim": existing_dim,
                                "target_dim": dim,
                                "existing_model": existing_model,
                                "target_model": model_name,
                            },
                        },
                    )
                    self._client.delete_collection(name)
                    coll = None
                else:
                    raise RuntimeError(
                        f"Chroma collection {name!r} was created with a different embedding "
                        f"(dim={existing_dim}, model={existing_model}). "
                        "Set MR_DATA_CHROMA_RECREATE_ON_MISMATCH=true to recreate or delete the persist dir."
                    )

        if coll is None:
            coll = self._client.create_collection(
                name=name,
                metadata={
                    "hnsw:space": "cosine",
                    "embedding_dim": dim,
                    "embedding_model": model_name,
                },
            )
        if backup_path is not None:
            print(
                f"[Chroma] Collection '{name}' was recreated due to an embedding change. "
                f"Backed up {backup_count} document(s) to {backup_path}. "
                f"To restore into the new embedding space, run: mr-data chroma-restore {backup_path}"
            )
        return coll

    def restore_collection_backup(self, backup_path) -> dict:
        """Restore a JSON backup (see _export_collection_backup) into this store.

        Embeddings are recomputed with the currently configured embedding function.
        Documents are embedded exactly as stored: personality documents already
        carry the "search_document: " prefix (added by add_personality_event before
        calling the embedding fn, which itself adds no prefix), so no prefix is
        added here. Documents whose id already exists are skipped, so repeated
        restores never duplicate. Metadatas (recall_count/added_at etc.) are
        restored verbatim via collection.add.

        Returns a report dict: {"collection", "restored", "skipped"}.
        """
        backup_path = Path(backup_path)
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
        name = payload["collection"]
        if name == "personality":
            coll = self.personality
            embedding_fn = self._personality_embedding_fn
        elif name == "memories":
            coll = self.memories
            embedding_fn = self._memory_embedding_fn
        else:
            raise ValueError(f"Unknown Chroma collection {name!r} in backup {backup_path}")

        documents = payload.get("documents", [])
        existing_ids = set(coll.get(ids=[d["id"] for d in documents])["ids"]) if documents else set()
        restored = 0
        skipped = 0
        for entry in documents:
            if entry["id"] in existing_ids:
                skipped += 1
                continue
            embedding = embedding_fn([entry["document"]])[0]
            coll.add(
                ids=[entry["id"]],
                documents=[entry["document"]],
                embeddings=[embedding],
                metadatas=[entry["metadata"] or {}],
            )
            restored += 1
        logger.info(
            "Restored Chroma collection backup",
            extra={
                "event": "chroma.backup_restored",
                "details": {
                    "collection": name,
                    "path": str(backup_path),
                    "restored": restored,
                    "skipped": skipped,
                },
            },
        )
        return {"collection": name, "restored": restored, "skipped": skipped}

    @property
    def personality(self):
        return self._ensure_collection(
            "personality",
            settings.personality_embedding_dim,
            settings.personality_embedding_model,
        )

    @property
    def memories(self):
        return self._ensure_collection(
            "memories",
            settings.memory_embedding_dim,
            settings.memory_embedding_model,
        )

    def add_personality_event(self, event: PersonalityEvent) -> str:
        """Add a personality event. When the caller provides a (stable) event id,
        an existing document with that id is left untouched and its id returned,
        so replays never create duplicates (see doc/database-design.md)."""
        doc_id = event.id or str(uuid.uuid4())
        if event.id and self.personality.get(ids=[doc_id])["ids"]:
            return doc_id
        # Embed the full context if available; otherwise fall back to the utterance.
        embedding_text = event.context if event.context else event.content
        # Nomic expects a document prefix for retrieval alignment.
        prefixed_embedding_text = f"search_document: {embedding_text}"
        embedding = self._personality_embedding_fn([prefixed_embedding_text])[0]
        self.personality.add(
            ids=[doc_id],
            documents=[prefixed_embedding_text],
            embeddings=[embedding],
            metadatas=[{
                "utterance": event.content,
                "context": event.context or "",
                "speaker": event.speaker or "assistant",
                "dimension_ids": ",".join(str(d) for d in event.dimension_ids),
                "source_type": event.source_type,
                "source_id": event.source_id or doc_id,
            }],
        )
        return doc_id

    def delete_personality_events(self, doc_ids: list[str]) -> None:
        if not doc_ids:
            return
        self.personality.delete(ids=doc_ids)

    def get_personality_event_ids_by_dimension(self, dimension_id: int) -> list[str]:
        """Return all personality doc IDs whose metadata includes the given dimension."""
        target = str(dimension_id)
        # Chroma does not support substring matching on string metadata, so we
        # fetch ids/metadatas and filter locally. This is acceptable because the
        # personality collection is small and deactivation is infrequent.
        result = self.personality.get(include=["metadatas"])
        doc_ids = []
        for doc_id, metadata in zip(result["ids"], result["metadatas"]):
            dim_ids_str = metadata.get("dimension_ids", "") if metadata else ""
            dim_ids = [x for x in dim_ids_str.split(",") if x]
            if target in dim_ids:
                doc_ids.append(doc_id)
        return doc_ids

    def query_personality(self, query: str, top_k: int = 5) -> list[dict]:
        prefixed_query = f"search_query: {query}"
        query_embedding = self._personality_embedding_fn([prefixed_query])[0]
        result = self.personality.query(query_embeddings=[query_embedding], n_results=top_k)
        docs = []
        for i in range(len(result["ids"][0])):
            metadata = result["metadatas"][0][i]
            dim_ids_str = metadata.get("dimension_ids", "")
            # Return the agent utterance as page_content, while preserving the
            # full context in metadata for callers that need it.
            docs.append({
                "id": result["ids"][0][i],
                "page_content": metadata.get("utterance", result["documents"][0][i]),
                "metadata": {
                    **metadata,
                    "dimension_ids": [int(x) for x in dim_ids_str.split(",") if x],
                },
            })
        return docs

    def add_memory(
        self,
        session_id: str,
        content: str,
        memory_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Add a memory document. When the caller provides a (stable) memory_id,
        an existing document with that id is left untouched and its id returned,
        so replays never reset recall_count/added_at."""
        doc_id = memory_id or str(uuid.uuid4())
        if memory_id and self.memories.get(ids=[doc_id])["ids"]:
            return doc_id
        meta = {"session_id": session_id}
        if metadata:
            meta.update(metadata)
        embedding = self._memory_embedding_fn([content])[0]
        self.memories.add(
            ids=[doc_id],
            documents=[content],
            embeddings=[embedding],
            metadatas=[meta],
        )
        return doc_id

    def upsert_memory(
        self,
        session_id: str,
        content: str,
        memory_id: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """Upsert a memory document. Use this when the caller already has a stable id."""
        meta = {"session_id": session_id}
        if metadata:
            meta.update(metadata)
        embedding = self._memory_embedding_fn([content])[0]
        self.memories.upsert(
            ids=[memory_id],
            documents=[content],
            embeddings=[embedding],
            metadatas=[meta],
        )
        return memory_id

    def query_memories(self, query: str, session_id: Optional[str] = None, top_k: int = 5) -> list[dict]:
        where = {"session_id": session_id} if session_id else None
        prefixed_query = f"Represent this sentence for searching relevant passages: {query}"
        query_embedding = self._memory_embedding_fn([prefixed_query])[0]
        result = self.memories.query(query_embeddings=[query_embedding], n_results=top_k, where=where)
        docs = []
        for i in range(len(result["ids"][0])):
            docs.append({
                "id": result["ids"][0][i],
                "page_content": result["documents"][0][i],
                "metadata": result["metadatas"][0][i],
            })
        return docs

    def increment_memory_recall(self, doc_ids: list[str]) -> None:
        """Increment recall_count and update last_recalled_at for dialogue memories."""
        if not doc_ids:
            return
        # Deduplicate while preserving order; Chroma update expects unique ids.
        unique_ids = list(dict.fromkeys(doc_ids))
        result = self.memories.get(ids=unique_ids, include=["metadatas"])
        now = datetime.now(timezone.utc).isoformat()
        new_metadatas = []
        for metadata in result.get("metadatas", []):
            if metadata is None:
                metadata = {}
            count = metadata.get("recall_count", 0)
            try:
                count = int(count) + 1
            except (TypeError, ValueError):
                count = 1
            metadata["recall_count"] = count
            metadata["last_recalled_at"] = now
            new_metadatas.append(metadata)
        if new_metadatas:
            self.memories.update(ids=unique_ids, metadatas=new_metadatas)

    def prune_stale_dialogue_memories(
        self, cutoff_days: int, min_recall_count: int
    ) -> int:
        """Remove dialogue memories that are older than cutoff_days and recalled fewer than min_recall_count times."""
        try:
            result = self.memories.get(where={"source_type": "dialogue"}, include=["metadatas"])
        except Exception as exc:
            logger.warning(
                "Failed to fetch dialogue memories for pruning",
                extra={
                    "event": "chroma.prune_failed",
                    "details": {
                        "cutoff_days": cutoff_days,
                        "min_recall_count": min_recall_count,
                        "error": str(exc),
                    },
                },
            )
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
        ids_to_delete = []
        for doc_id, metadata in zip(result.get("ids", []), result.get("metadatas", [])):
            if metadata is None:
                continue
            count = metadata.get("recall_count", 0)
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0
            last_recalled = metadata.get("last_recalled_at", "")
            if not last_recalled:
                # Never recalled; use added_at as fallback if available.
                last_recalled = metadata.get("added_at", "")
            try:
                last_dt = datetime.fromisoformat(last_recalled)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if last_dt < cutoff and count < min_recall_count:
                ids_to_delete.append(doc_id)

        if ids_to_delete:
            self.memories.delete(ids=ids_to_delete)
        return len(ids_to_delete)
