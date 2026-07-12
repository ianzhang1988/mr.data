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
        self.personality.add(
            ids=[doc_id],
            documents=[event.content],
            metadatas=[{
                "dimension_tags": ",".join(event.dimension_tags),
                "source_type": event.source_type,
                "source_id": event.source_id or doc_id,
            }],
        )
        return doc_id

    def query_personality(self, query: str, top_k: int = 5) -> list[dict]:
        result = self.personality.query(query_texts=[query], n_results=top_k)
        docs = []
        for i in range(len(result["ids"][0])):
            docs.append({
                "id": result["ids"][0][i],
                "page_content": result["documents"][0][i],
                "metadata": result["metadatas"][0][i],
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
