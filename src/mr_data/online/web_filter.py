"""Per-document web relevance filtering and content extraction.

Each web document is judged by an individual LLM call: irrelevant docs are
dropped, relevant docs have their ``page_content`` replaced by a simplified
extraction of the parts relevant to the user input. Any failure falls back
to keeping the original document.
"""

from mr_data.logging import get_logger
from mr_data.models import WebDoc, WebDocExtraction

logger = get_logger("mr_data.online")


def filter_web_docs(llm, docs: list[WebDoc], user_input: str) -> list[WebDoc]:
    """Filter and extract web docs one by one with the LLM.

    Returns a new list of docs; never returns an empty list for non-empty
    input (falls back to the original docs to avoid over-filtering).
    """
    if not docs:
        return []

    filtered: list[WebDoc] = []
    for doc in docs:
        system = (
            "判断给定网络资料是否与用户输入相关。"
            "若相关，请从资料中摘取并简化与用户输入相关的内容；若不相关，摘取内容留空。"
        )
        prompt = (
            f"用户输入：{user_input}\n\n"
            f"资料内容：\n{doc.page_content}\n\n"
            "请判断该资料是否相关，并摘取相关内容。"
        )
        try:
            result = llm.chat_structured(
                system, prompt, response_format=WebDocExtraction, temperature=0.0
            )
            extraction = WebDocExtraction.model_validate(result)
        except Exception as exc:
            details = {"error": str(exc), "doc_id": doc.id}
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
