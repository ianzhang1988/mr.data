"""Regression tests for re-identifying web docs with the real URL after
page extraction (search-result dedup for redirect/jump links)."""

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.online import DialogueGraph
from mr_data.online.page_extract import ExtractedPage
from mr_data.online.search_providers import _stable_web_id

pytestmark = pytest.mark.usefixtures("reset_pg_state")

REDIRECT_URL = "https://www.baidu.com/link?url=abc123"
REAL_URL = "http://example.com/planets"


class FakeWebSearch:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def search(self, query: str) -> list[dict]:
        return self._docs


class FakePageExtractor:
    def __init__(self, result):
        self._result = result

    def extract(self, url: str):
        return self._result


def _make_doc(url: str, title: str = "行星") -> dict:
    return {
        "id": _stable_web_id(url, "摘要", title),
        "page_content": f"{title}\n摘要",
        "metadata": {
            "source_type": "web",
            "url": url,
            "title": title,
        },
    }


def _make_graph(pg, chroma_store, fake_llm, extractor, monkeypatch):
    monkeypatch.setattr(settings, "web_extract_max_pages", 2)
    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch([]),
        enable_web_search=True,
    )
    graph.page_extractor = extractor
    return graph


def test_extract_web_pages_reids_doc_with_real_url(
    fake_llm, pg_available, chroma_store, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    graph = _make_graph(
        pg,
        chroma_store,
        fake_llm,
        FakePageExtractor(ExtractedPage(text="全文内容", url=REAL_URL)),
        monkeypatch,
    )

    doc = _make_doc(REDIRECT_URL)
    state = graph._extract_web_pages(
        {"session_id": "s", "user_input": "u", "web_docs": [doc]}
    )

    new_doc = state["web_docs"][0]
    assert new_doc["id"] == _stable_web_id(REAL_URL)
    assert new_doc["metadata"]["url"] == REAL_URL
    assert new_doc["metadata"]["source_url"] == REDIRECT_URL
    assert new_doc["metadata"]["extracted"] is True
    assert new_doc["page_content"] == "行星\n全文内容"


def test_extract_web_pages_keeps_doc_when_extraction_fails(
    fake_llm, pg_available, chroma_store, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    graph = _make_graph(
        pg,
        chroma_store,
        fake_llm,
        FakePageExtractor(None),
        monkeypatch,
    )

    doc = _make_doc(REDIRECT_URL)
    state = graph._extract_web_pages(
        {"session_id": "s", "user_input": "u", "web_docs": [doc]}
    )

    assert state["web_docs"][0] == doc
