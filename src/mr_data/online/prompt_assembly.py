from dataclasses import dataclass

from mr_data.llm.tokenizer import TokenCounter


@dataclass
class PromptSection:
    """Prompt 组装中的一个分块。"""

    name: str
    text: str
    priority: int
    role: str  # "system" | "assistant" | "user"


def wrap_section(tag: str, text: str) -> str:
    """Wrap a section of context materials in XML-style tags for clarity."""
    body = text if text else "（无）"
    return f"<{tag}>\n{body}\n</{tag}>"


def build_messages(fitted: list[tuple[PromptSection, str]]) -> list[dict]:
    """Group fitted sections into OpenAI-style messages by role."""
    role_texts: dict[str, list[str]] = {}
    for section, text in fitted:
        if not text:
            continue
        role_texts.setdefault(section.role, []).append(text)
    messages = []
    for role in ("system", "assistant", "user"):
        if role in role_texts:
            messages.append(
                {"role": role, "content": "\n\n".join(role_texts[role])})
    return messages


class PromptAssembler:
    def __init__(self, llm):
        self.llm = llm

    def fit_sections(
        self, sections: list[PromptSection], limit: int
    ) -> list[tuple[PromptSection, str]]:
        """Assemble prompt sections, compressing lower-priority ones if over token budget."""
        counter = TokenCounter()
        section_tokens = [(s, counter.count(s.text)) for s in sections]
        total = sum(t for _, t in section_tokens)
        if total <= limit:
            return [(s, s.text) for s, _ in section_tokens]

        must_keep = [(s, t) for s, t in section_tokens if s.priority >= 80]
        adjustable = [(s, t) for s, t in section_tokens if s.priority < 80]
        must_total = sum(t for _, t in must_keep)

        fitted_map: dict[str, str] = {}
        if must_total >= limit:
            # Even high-priority sections exceed the budget; compress them proportionally.
            fitted_map = self._compress_to_budget(must_keep, limit)
            for s, _ in adjustable:
                fitted_map[s.name] = f"（{s.name} 因上下文限制已省略）"
        else:
            remaining = limit - must_total
            fitted_map = self._compress_to_budget(adjustable, remaining)
            for s, _ in must_keep:
                fitted_map[s.name] = s.text

        return [(s, fitted_map.get(s.name, s.text)) for s, _ in section_tokens]

    def _compress_to_budget(
        self, section_tokens: list[tuple[PromptSection, int]], budget: int
    ) -> dict[str, str]:
        """Compress a list of sections so their total token count fits within budget."""
        total = sum(t for _, t in section_tokens)
        fitted: dict[str, str] = {}
        if total <= budget:
            for s, _ in section_tokens:
                fitted[s.name] = s.text
            return fitted
        for s, t in section_tokens:
            target = max(int(budget * (t / total)), 1)
            if t <= target:
                fitted[s.name] = s.text
            else:
                fitted[s.name] = self._compress_text(s.text, target)
        return fitted

    def _compress_text(self, text: str, target_tokens: int) -> str:
        """Use LLM to compress text to roughly target_tokens; fallback to hard truncation."""
        counter = TokenCounter()
        if counter.count(text) <= target_tokens:
            return text
        system = f"请将以下内容压缩到大约 {target_tokens} token 以内，保留关键事实和语义，不要输出解释："
        try:
            # Pre-truncate input so the compression prompt itself stays reasonable.
            max_input_chars = max(target_tokens * 8, 500)
            input_text = text[:max_input_chars]
            compressed = self.llm.chat(system, input_text, temperature=0.3)
        except Exception:
            compressed = text
        # Ensure the result does not exceed the target by too much.
        max_chars = max(target_tokens * 4, 100)
        if len(compressed) > max_chars:
            compressed = compressed[:max_chars]
        return compressed
