import pytest

from conftest import capture_events, open_log_propagation

from mr_data.config import settings
from mr_data.models import WebDoc, WebDocMetadata, WebDocExtraction
from mr_data.online import web_filter as wf
from mr_data.online.web_filter import filter_web_docs


class FakeLLM:
    """Lightweight LLM stub returning preset WebDocExtraction payloads."""

    def __init__(self, results=None, error=None):
        self._results = list(results or [])
        self._error = error
        self.calls = []

    def chat_structured(self, system, prompt, response_format, temperature=0.0):
        assert response_format is WebDocExtraction
        self.calls.append((system, prompt))
        if self._error is not None:
            raise self._error
        return self._results.pop(0)


def make_doc(content, url="http://example.com"):
    return WebDoc(page_content=content, metadata=WebDocMetadata(url=url))


def test_relevant_doc_content_is_extracted():
    doc = make_doc("原始长文本内容")
    llm = FakeLLM(results=[{"is_relevant": True, "extracted_text": " 摘取后的内容 "}])

    result = filter_web_docs(llm, [doc], "用户输入")

    assert len(result) == 1
    assert result[0].page_content == "摘取后的内容"
    assert result[0].metadata.llm_extracted is True
    assert result[0].metadata.url == "http://example.com"


def test_irrelevant_doc_is_dropped():
    relevant = make_doc("相关资料", url="http://a.com")
    irrelevant = make_doc("无关资料", url="http://b.com")
    llm = FakeLLM(results=[
        {"is_relevant": True, "extracted_text": "摘取内容"},
        {"is_relevant": False, "extracted_text": ""},
    ])

    result = filter_web_docs(llm, [relevant, irrelevant], "用户输入")

    assert len(result) == 1
    assert result[0].metadata.url == "http://a.com"


@pytest.mark.parametrize(
    "llm_factory,doc_count",
    [
        pytest.param(
            lambda: FakeLLM(results=[{"is_relevant": True, "extracted_text": "  "}]),
            1,
            id="empty_extraction_keeps_original",
        ),
        pytest.param(
            lambda: FakeLLM(error=RuntimeError("llm failure")),
            1,
            id="llm_error_keeps_original",
        ),
        pytest.param(
            lambda: FakeLLM(results=[
                {"is_relevant": False, "extracted_text": ""},
                {"is_relevant": False, "extracted_text": ""},
            ]),
            2,
            id="all_irrelevant_falls_back",
        ),
    ],
)
def test_fallback_keeps_original_docs(llm_factory, doc_count):
    """摘取为空 / LLM 报错 / 全部判不相关时，原 doc 原样保留。"""
    docs = [
        make_doc("原始内容" if doc_count == 1 else f"资料{'一二'[i]}", url=f"http://{'ab'[i]}.com")
        for i in range(doc_count)
    ]

    result = filter_web_docs(llm_factory(), docs, "用户输入")

    assert result == docs


def test_over_budget_head_truncates_llm_copy_only(monkeypatch, caplog):
    """C2：整份 filter prompt 超 web_filter_max_tokens 时纯头部截「发给 LLM 的

    副本」（带省略标记），源 doc 不动；截断记 web.filter_prompt_truncated。"""
    open_log_propagation(caplog, monkeypatch)
    # 用源码模块级模板常量渲染计算 prompt_overhead（单一事实源）。
    overhead = wf._token_counter.count(
        wf._FILTER_SYSTEM
        + wf._FILTER_USER_PROMPT.format(user_input="用户输入", content="")
    )
    monkeypatch.setattr(settings, "web_filter_max_tokens", overhead + 10)  # 正文预算仅 10 token
    content = "相关资料的正文段落，包含足够长的内容。" * 50  # ~750 字，远超预算
    doc = make_doc(content)
    llm = FakeLLM(results=[{"is_relevant": True, "extracted_text": "摘取内容"}])

    result = filter_web_docs(llm, [doc], "用户输入")

    _, prompt = llm.calls[0]
    assert wf._FILTER_TRUNCATION_MARKER.strip() in prompt
    assert content not in prompt  # 副本为头截版，不含全文
    assert content[:4] in prompt  # 头部保留
    assert doc.page_content == content  # 源数据不动
    assert result[0].page_content == "摘取内容"
    events = capture_events(caplog, "web.filter_prompt_truncated")
    assert len(events) == 1
    details = events[0].details
    assert details["url"] == "http://example.com"
    assert details["original_tokens"] > details["truncated_tokens"]


def test_within_budget_no_truncation(monkeypatch, caplog):
    """C2 回归：预算内内容原样进 prompt，不触发截断日志。"""
    open_log_propagation(caplog, monkeypatch)
    monkeypatch.setattr(settings, "web_filter_max_tokens", 32768)
    doc = make_doc("短小正文")
    llm = FakeLLM(results=[{"is_relevant": True, "extracted_text": "摘取内容"}])

    result = filter_web_docs(llm, [doc], "用户输入")

    _, prompt = llm.calls[0]
    assert "短小正文" in prompt
    assert wf._FILTER_TRUNCATION_MARKER.strip() not in prompt
    assert capture_events(caplog, "web.filter_prompt_truncated") == []
    assert result[0].page_content == "摘取内容"
