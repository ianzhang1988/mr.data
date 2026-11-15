from conftest import capture_events, open_log_propagation

from mr_data.config import settings
from mr_data.models import WebDoc, WebDocMetadata, WebDocExtraction
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


def test_relevant_doc_with_empty_extraction_keeps_original():
    doc = make_doc("原始内容")
    llm = FakeLLM(results=[{"is_relevant": True, "extracted_text": "  "}])

    result = filter_web_docs(llm, [doc], "用户输入")

    assert result == [doc]


def test_llm_error_keeps_original_doc():
    doc = make_doc("原始内容")
    llm = FakeLLM(error=RuntimeError("llm failure"))

    result = filter_web_docs(llm, [doc], "用户输入")

    assert result == [doc]


def test_all_irrelevant_falls_back_to_original_docs():
    docs = [make_doc("资料一", url="http://a.com"), make_doc("资料二", url="http://b.com")]
    llm = FakeLLM(results=[
        {"is_relevant": False, "extracted_text": ""},
        {"is_relevant": False, "extracted_text": ""},
    ])

    result = filter_web_docs(llm, docs, "用户输入")

    assert result == docs


def test_empty_input_returns_empty_list():
    llm = FakeLLM()

    assert filter_web_docs(llm, [], "用户输入") == []
    assert llm.calls == []


def test_over_budget_head_truncates_llm_copy_only(monkeypatch, caplog):
    """C2：整份 filter prompt 超 web_filter_max_tokens 时纯头部截「发给 LLM 的

    副本」（带省略标记），源 doc 不动；截断记 web.filter_prompt_truncated。"""
    open_log_propagation(caplog, monkeypatch)
    from mr_data.online import web_filter as wf

    system = (
        "判断给定网络资料是否与用户输入相关。"
        "若相关，请从资料中摘取并简化与用户输入相关的内容；若不相关，摘取内容留空。"
    )
    overhead = wf._token_counter.count(
        system + "用户输入：用户输入\n\n资料内容：\n\n\n请判断该资料是否相关，并摘取相关内容。"
    )
    monkeypatch.setattr(settings, "web_filter_max_tokens", overhead + 10)  # 正文预算仅 10 token
    content = "相关资料的正文段落，包含足够长的内容。" * 50  # ~750 字，远超预算
    doc = make_doc(content)
    llm = FakeLLM(results=[{"is_relevant": True, "extracted_text": "摘取内容"}])

    result = filter_web_docs(llm, [doc], "用户输入")

    _, prompt = llm.calls[0]
    assert "[资料过长，以下为开头部分，已省略后续内容]" in prompt
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
    assert "[资料过长" not in prompt
    assert capture_events(caplog, "web.filter_prompt_truncated") == []
    assert result[0].page_content == "摘取内容"
