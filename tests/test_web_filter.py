from mr_data.models import WebDocExtraction
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
    return {"page_content": content, "metadata": {"url": url}}


def test_relevant_doc_content_is_extracted():
    doc = make_doc("原始长文本内容")
    llm = FakeLLM(results=[{"is_relevant": True, "extracted_text": " 摘取后的内容 "}])

    result = filter_web_docs(llm, [doc], "用户输入")

    assert len(result) == 1
    assert result[0]["page_content"] == "摘取后的内容"
    assert result[0]["metadata"]["llm_extracted"] is True
    assert result[0]["metadata"]["url"] == "http://example.com"


def test_irrelevant_doc_is_dropped():
    relevant = make_doc("相关资料", url="http://a.com")
    irrelevant = make_doc("无关资料", url="http://b.com")
    llm = FakeLLM(results=[
        {"is_relevant": True, "extracted_text": "摘取内容"},
        {"is_relevant": False, "extracted_text": ""},
    ])

    result = filter_web_docs(llm, [relevant, irrelevant], "用户输入")

    assert len(result) == 1
    assert result[0]["metadata"]["url"] == "http://a.com"


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
