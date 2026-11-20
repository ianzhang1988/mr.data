import pytest

from mr_data.models import WebDoc, WebDocMetadata
from mr_data.online.search_providers import SearchProvider
from mr_data.online import web_search


class FakeSuccessProvider(SearchProvider):
    name = "fake_success"

    def __init__(self, max_results=None):
        self.max_results = max_results

    def search(self, query: str) -> list[WebDoc]:
        return [
            WebDoc(
                page_content=f"Success\n{query}",
                metadata=WebDocMetadata(
                    url="https://example.com",
                    title="Success",
                ),
            )
        ]


class FakeEmptyProvider(SearchProvider):
    name = "fake_empty"

    def __init__(self, max_results=None):
        self.max_results = max_results

    def search(self, query: str) -> list[WebDoc]:
        return []


class FakeErrorProvider(SearchProvider):
    name = "fake_error"

    def __init__(self, max_results=None):
        self.max_results = max_results

    def search(self, query: str) -> list[WebDoc]:
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


@pytest.mark.parametrize(
    "providers",
    [
        pytest.param(["fake_success", "fake_empty"], id="first_success"),
        pytest.param(["fake_empty", "fake_success"], id="falls_back_empty"),
        pytest.param(["fake_error", "fake_success"], id="falls_back_raises"),
    ],
)
def test_dispatcher_skips_to_next_provider_until_success(providers):
    """首个 provider 成功 / 返回空 / 抛异常，调度都跳到下一个直到拿到结果。"""
    tool = web_search.WebSearchTool(providers=providers)
    results = tool.search("hello")
    assert len(results) == 1
    assert results[0].metadata.title == "Success"


def test_dispatcher_skips_unknown_provider():
    tool = web_search.WebSearchTool(
        providers=["unknown", "fake_empty", "fake_success"]
    )
    results = tool.search("hello")
    assert len(results) == 1
    assert results[0].metadata.title == "Success"


def test_dispatcher_returns_empty_when_all_fail():
    tool = web_search.WebSearchTool(providers=["fake_error", "fake_empty"])
    results = tool.search("hello")
    assert results == []


def test_dispatcher_returns_empty_for_blank_query():
    tool = web_search.WebSearchTool(providers=["fake_success"])
    assert tool.search("") == []
    assert tool.search("   ") == []


def test_duckduckgo_none_results_returns_empty(caplog, monkeypatch):
    """F-19：ddgs.text 某些版本无结果时返回 None（而非 []），provider 应
    返回 [] 且不产生 web.search_failed 事件。"""
    import sys
    import types

    from mr_data.online.search_providers import DuckDuckGoProvider

    from conftest import capture_events, open_log_propagation

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, max_results=None):
            return None

    fake_module = types.ModuleType("duckduckgo_search")
    fake_module.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "duckduckgo_search", fake_module)

    open_log_propagation(caplog, monkeypatch)

    provider = DuckDuckGoProvider()
    assert provider.search("hello") == []
    assert capture_events(caplog, "web.search_failed") == []
