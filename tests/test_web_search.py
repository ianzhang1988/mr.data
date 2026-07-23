import pytest

from mr_data.online.search_providers import SearchProvider
from mr_data.online import web_search


class FakeSuccessProvider(SearchProvider):
    name = "fake_success"

    def __init__(self, max_results=None):
        self.max_results = max_results

    def search(self, query: str) -> list[dict]:
        return [
            {
                "id": "web:0",
                "page_content": f"Success\n{query}",
                "metadata": {
                    "source_type": "web",
                    "url": "https://example.com",
                    "title": "Success",
                },
            }
        ]


class FakeEmptyProvider(SearchProvider):
    name = "fake_empty"

    def __init__(self, max_results=None):
        self.max_results = max_results

    def search(self, query: str) -> list[dict]:
        return []


class FakeErrorProvider(SearchProvider):
    name = "fake_error"

    def __init__(self, max_results=None):
        self.max_results = max_results

    def search(self, query: str) -> list[dict]:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _reset_provider_map(monkeypatch):
    """Provide a fresh, isolated provider map for every test."""
    original_map = web_search._PROVIDER_MAP.copy()
    monkeypatch.setattr(
        web_search,
        "_PROVIDER_MAP",
        {
            "fake_success": FakeSuccessProvider,
            "fake_empty": FakeEmptyProvider,
            "fake_error": FakeErrorProvider,
        },
    )
    yield
    web_search._PROVIDER_MAP = original_map


def test_dispatcher_returns_first_successful_provider():
    tool = web_search.WebSearchTool(providers=["fake_success", "fake_empty"])
    results = tool.search("hello")
    assert len(results) == 1
    assert results[0]["metadata"]["title"] == "Success"


def test_dispatcher_falls_back_when_first_is_empty():
    tool = web_search.WebSearchTool(providers=["fake_empty", "fake_success"])
    results = tool.search("hello")
    assert len(results) == 1
    assert results[0]["metadata"]["title"] == "Success"


def test_dispatcher_falls_back_when_first_raises():
    tool = web_search.WebSearchTool(providers=["fake_error", "fake_success"])
    results = tool.search("hello")
    assert len(results) == 1
    assert results[0]["metadata"]["title"] == "Success"


def test_dispatcher_skips_unknown_provider():
    tool = web_search.WebSearchTool(
        providers=["unknown", "fake_empty", "fake_success"]
    )
    results = tool.search("hello")
    assert len(results) == 1
    assert results[0]["metadata"]["title"] == "Success"


def test_dispatcher_returns_empty_when_all_fail():
    tool = web_search.WebSearchTool(providers=["fake_error", "fake_empty"])
    results = tool.search("hello")
    assert results == []


def test_dispatcher_returns_empty_for_blank_query():
    tool = web_search.WebSearchTool(providers=["fake_success"])
    assert tool.search("") == []
    assert tool.search("   ") == []
