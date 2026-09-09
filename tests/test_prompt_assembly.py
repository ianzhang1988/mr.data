"""PromptAssembler 预算分配的 priority 梯度测试（改进 48）。"""

from mr_data.online.prompt_assembly import PromptAssembler, PromptSection


class FakeLLM:
    """chat 返回确定性短文本，避免真实 LLM 调用。"""

    def chat(self, system, user, temperature=0.0):
        return "压缩后"


def _assembler():
    return PromptAssembler(FakeLLM())


def _text(name: str, fitted: list[tuple[PromptSection, str]]) -> str:
    return dict((s.name, t) for s, t in fitted)[name]


def test_fit_under_budget_returns_verbatim():
    sections = [
        PromptSection("system", "系统提示", 100, "system"),
        PromptSection("web", "网页内容" * 50, 40, "assistant"),
    ]
    fitted = _assembler().fit_sections(sections, limit=100000)
    assert _text("system", fitted) == "系统提示"
    assert _text("web", fitted) == "网页内容" * 50


def test_priority_gradient_keeps_high_priority_verbatim():
    """预算只够装一个时，priority 70 的 personality 原文保留，web 被压缩。"""
    personality_text = "人格素材" * 20
    web_text = "网页资料" * 500
    sections = [
        PromptSection("personality", personality_text, 70, "assistant"),
        PromptSection("web", web_text, 40, "assistant"),
    ]
    asm = _assembler()
    from mr_data.llm.tokenizer import TokenCounter

    counter = TokenCounter()
    personality_tokens = counter.count(personality_text)
    web_tokens = counter.count(web_text)
    assert web_tokens > personality_tokens * 5
    # 预算：装得下 personality 全文，但装不下 web。
    limit = personality_tokens + personality_tokens // 2

    fitted = asm.fit_sections(sections, limit)
    assert _text("personality", fitted) == personality_text
    web_fitted = _text("web", fitted)
    assert web_fitted != web_text
    assert len(web_fitted) < len(web_text) // 10


def test_same_priority_shares_budget_proportionally(monkeypatch):
    """同优先级组装不下时，按体积分摊剩余预算。"""
    big = "甲" * 1200
    small = "乙" * 300
    sections = [
        PromptSection("big", big, 50, "assistant"),
        PromptSection("small", small, 50, "assistant"),
    ]
    asm = _assembler()
    from mr_data.llm.tokenizer import TokenCounter

    counter = TokenCounter()
    total = counter.count(big) + counter.count(small)
    limit = total // 2

    targets: dict[str, int] = {}

    def fake_compress(text, target):
        targets["big" if text == big else "small"] = target
        return "压缩后"

    monkeypatch.setattr(asm, "_compress_text", fake_compress)
    fitted = asm.fit_sections(sections, limit)

    assert set(targets) == {"big", "small"}
    # 体积大的分到的预算更多，且两者之和不超过预算。
    assert targets["big"] > targets["small"]
    assert targets["big"] + targets["small"] <= limit
    assert _text("big", fitted) == "压缩后"
    assert _text("small", fitted) == "压缩后"


def test_must_keep_extreme_overage_uses_priority_gradient(monkeypatch):
    """极端超预算时 must_keep 内部也按 priority 降序注水：100 原文保留。"""
    system_text = "系统" * 10
    identity_text = "身份" * 400
    user_input_text = "输入" * 400
    sections = [
        PromptSection("system", system_text, 100, "system"),
        PromptSection("identity", identity_text, 90, "system"),
        PromptSection("user_input", user_input_text, 80, "user"),
    ]
    asm = _assembler()
    from mr_data.llm.tokenizer import TokenCounter

    counter = TokenCounter()
    system_tokens = counter.count(system_text)
    total = sum(counter.count(s.text) for s in sections)
    # must_keep 总体超限，但 system 单独装得下。
    limit = system_tokens + system_tokens // 2
    assert limit < total

    monkeypatch.setattr(asm, "_compress_text", lambda text, target: "压缩后")
    fitted = asm.fit_sections(sections, limit)

    assert _text("system", fitted) == system_text
    # 预算被 system 用完后，identity / user_input 只能压缩或省略。
    assert _text("identity", fitted) != identity_text
    assert _text("user_input", fitted) != user_input_text


def test_exhausted_budget_yields_omission_placeholder():
    """预算耗尽后，低优先级段输出省略占位符。"""
    personality_text = "人格" * 100
    memory_text = "记忆" * 100
    web_text = "网页" * 100
    sections = [
        PromptSection("personality", personality_text, 70, "assistant"),
        PromptSection("memory", memory_text, 60, "assistant"),
        PromptSection("web", web_text, 40, "assistant"),
    ]
    asm = _assembler()
    from mr_data.llm.tokenizer import TokenCounter

    counter = TokenCounter()
    personality_tokens = counter.count(personality_text)
    # 预算仅够 personality 全文 + 一点点零头。
    limit = personality_tokens + 2

    fitted = asm.fit_sections(sections, limit)
    assert _text("personality", fitted) == personality_text
    assert "因上下文限制已省略" in _text("web", fitted)
