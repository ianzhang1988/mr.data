"""Per-document web relevance filtering and content extraction.

Each web document is judged by an individual LLM call: irrelevant docs are
dropped, relevant docs have their ``page_content`` replaced by a simplified
extraction of the parts relevant to the user input. Any failure falls back
to keeping the original document.
"""

from mr_data.config import settings
from mr_data.llm.tokenizer import TokenCounter
from mr_data.logging import get_logger
from mr_data.models import WebDoc, WebDocExtraction

logger = get_logger("mr_data.online")

_token_counter = TokenCounter()

# filter prompt 副本头部截断时附加的省略标记（发给 LLM 的副本专用，源数据不动）。
_FILTER_TRUNCATION_MARKER = "\n\n[资料过长，以下为开头部分，已省略后续内容]"

# 过滤/摘取用的 system 与 user prompt 模板（单一事实源；测试按常量渲染计算
# prompt_overhead，避免源码改措辞后测试静默失配）。
_FILTER_SYSTEM = (
    "判断给定网络资料是否与用户输入相关。"
    "若相关，请从资料中摘取并简化与用户输入相关的内容；若不相关，摘取内容留空。"
)
_FILTER_USER_PROMPT = (
    "用户输入：{user_input}\n\n"
    "资料内容：\n{content}\n\n"
    "请判断该资料是否相关，并摘取相关内容。"
)


def _truncate_content_for_budget(content: str, budget_tokens: int) -> str:
    """纯头部截 content 至 budget_tokens 内：按中文 ~2 字/token 估切口，

    真实 token 计数超发则砍半兜底，换行收尾后附省略标记。
    """
    if budget_tokens <= 0:
        return _FILTER_TRUNCATION_MARKER.strip()
    cut = content[: budget_tokens * 2]
    while cut and _token_counter.count(cut) > budget_tokens:
        cut = cut[: len(cut) // 2]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut + _FILTER_TRUNCATION_MARKER


def filter_web_docs(llm, docs: list[WebDoc], user_input: str) -> list[WebDoc]:
    """Filter and extract web docs one by one with the LLM.

    Returns a new list of docs; never returns an empty list for non-empty
    input (falls back to the original docs to avoid over-filtering).

    整份 filter prompt 受 ``web_filter_max_tokens`` 总预算约束：超额时纯头部
    截「发给 LLM 的副本」（源 doc 不动），防止截外内容在副本之外被误判
    无关而整条丢弃；截断记 ``web.filter_prompt_truncated``。
    """
    if not docs:
        return []

    system = _FILTER_SYSTEM
    prompt_overhead = _token_counter.count(
        system + _FILTER_USER_PROMPT.format(user_input=user_input, content="")
    )
    content_budget = settings.web_filter_max_tokens - prompt_overhead

    filtered: list[WebDoc] = []
    for doc in docs:
        content = doc.page_content
        original_tokens = _token_counter.count(content)
        if original_tokens > content_budget:
            content = _truncate_content_for_budget(content, content_budget)
            truncated_tokens = _token_counter.count(content)
            logger.warning(
                "Web filter prompt over budget; head-truncating LLM copy",
                extra={
                    "event": "web.filter_prompt_truncated",
                    "details": {
                        "url": doc.metadata.url or None,
                        "original_tokens": original_tokens,
                        "truncated_tokens": truncated_tokens,
                    },
                },
            )
        prompt = _FILTER_USER_PROMPT.format(user_input=user_input, content=content)
        try:
            result = llm.chat_structured(
                system, prompt, response_format=WebDocExtraction, temperature=0.0
            )
            extraction = WebDocExtraction.model_validate(result)
        except Exception as exc:
            details = {"error": str(exc)}
            if doc.metadata.url:
                details["url"] = doc.metadata.url
            if doc.metadata.title:
                details["title"] = doc.metadata.title
            logger.warning(
                "Web doc filtering failed; keeping original document",
                extra={"event": "web.filter_doc_failed", "details": details},
            )
            filtered.append(doc)
            continue

        if not extraction.is_relevant:
            continue

        extracted = extraction.extracted_text.strip()
        if extracted:
            filtered.append(doc.model_copy(update={
                "page_content": extracted,
                "metadata": doc.metadata.model_copy(update={"llm_extracted": True}),
            }))
        else:
            filtered.append(doc)

    # Fallback: if the LLM filtered out everything, keep the original docs.
    if not filtered:
        return docs
    return filtered
