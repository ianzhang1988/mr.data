import uuid
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from mr_data.config import settings
from mr_data.models import PersonalityEvent


class ChromaStore:
    def __init__(self, persist_dir: Optional[str] = None):
        self.persist_dir = Path(persist_dir or settings.chroma_persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._embedding_fn = DefaultEmbeddingFunction()

    def _collection(self, name: str):
        return self._client.get_or_create_collection(
            name=name,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def personality(self):
        return self._collection("personality")

    @property
    def memories(self):
        return self._collection("memories")

    def add_personality_event(self, event: PersonalityEvent) -> str:
        doc_id = event.id or str(uuid.uuid4())
        # Embed the full context if available; otherwise fall back to the utterance.
        embedding_text = event.context if event.context else event.content
        self.personality.add(
            ids=[doc_id],
            documents=[embedding_text],
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
        result = self.personality.query(query_texts=[query], n_results=top_k)
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

    def add_memory(self, session_id: str, content: str, memory_id: Optional[str] = None) -> str:
        doc_id = memory_id or str(uuid.uuid4())
        self.memories.add(
            ids=[doc_id],
            documents=[content],
            metadatas=[{"session_id": session_id}],
        )
        return doc_id

    def query_memories(self, query: str, session_id: Optional[str] = None, top_k: int = 5) -> list[dict]:
        where = {"session_id": session_id} if session_id else None
        result = self.memories.query(query_texts=[query], n_results=top_k, where=where)
        docs = []
        for i in range(len(result["ids"][0])):
            docs.append({
                "id": result["ids"][0][i],
                "page_content": result["documents"][0][i],
                "metadata": result["metadatas"][0][i],
            })
        return docs
