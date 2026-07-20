import uuid
from pathlib import Path
from typing import Optional

import chromadb

from mr_data.config import settings
from mr_data.embeddings import BGEMemoryEmbedding, NomicPersonalityEmbedding
from mr_data.logging import get_logger
from mr_data.models import PersonalityEvent

logger = get_logger("mr_data.chroma")


class ChromaStore:
    def __init__(
        self,
        persist_dir: Optional[str] = None,
        personality_embedding_fn: Optional = None,
        memory_embedding_fn: Optional = None,
    ):
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

    def _ensure_collection(self, name: str, dim: int, model_name: str):
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
        return coll

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
        doc_id = event.id or str(uuid.uuid4())
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
        doc_id = memory_id or str(uuid.uuid4())
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
