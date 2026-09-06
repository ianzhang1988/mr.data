import json
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel

from mr_data.config import settings
from mr_data.logging import get_logger

logger = get_logger("mr_data.llm")


def _extract_json(text: str) -> dict:
    """Extract the first JSON object/array from raw LLM text output.

    Tries a plain ``json.loads`` first; on failure strips markdown code fences
    and decodes the first complete JSON substring starting at the first ``{``
    or ``[``. Raises ValueError if nothing parses.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if not starts:
        raise ValueError(f"No JSON object found in text: {text[:200]!r}")
    try:
        data, _ = json.JSONDecoder().raw_decode(cleaned[min(starts):])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to extract JSON from text: {text[:200]!r}") from exc
    return data


class LLMClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.base_url = base_url or settings.llm_base_url
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self._client: Optional[OpenAI] = None
        self._parse_supported = True

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    def chat_with_messages(self, messages: list[dict], temperature: float = 0.7) -> str:
        """Chat with an explicit list of messages."""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    def structured_chat(
        self,
        messages: list[dict],
        response_format: type[BaseModel],
        temperature: float = 0.2,
    ) -> dict:
        """Unified structured output entry with automatic fallback.

        Behavior depends on ``settings.llm_structured_mode``:
        - ``"parse"``: only use the structured-parse endpoint; errors propagate.
        - ``"prompt"``: skip parse and use the schema-in-prompt fallback directly.
        - ``"auto"`` (default): try parse first; on any error log a warning and
          fall back to the schema-in-prompt path, caching the failure so later
          calls skip parse entirely.
        """
        mode = settings.llm_structured_mode
        if mode == "prompt":
            return self._structured_chat_via_prompt(messages, response_format, temperature)
        if mode == "parse" or self._parse_supported:
            try:
                return self._structured_chat_via_parse(messages, response_format, temperature)
            except Exception as exc:
                if mode == "parse":
                    raise
                logger.warning(
                    "Structured parse failed, falling back to schema-in-prompt mode",
                    extra={
                        "event": "llm.structured_parse_fallback",
                        "details": {
                            "response_format": response_format.__name__,
                            "error": str(exc),
                        },
                    },
                )
                self._parse_supported = False
        return self._structured_chat_via_prompt(messages, response_format, temperature)

    def chat_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: type[BaseModel],
        temperature: float = 0.2,
    ) -> dict:
        """Syntactic sugar over :meth:`structured_chat` for system+user prompts."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.structured_chat(messages, response_format, temperature)

    def _structured_chat_via_parse(
        self,
        messages: list[dict],
        response_format: type[BaseModel],
        temperature: float,
    ) -> dict:
        resp = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)

    def _structured_chat_via_prompt(
        self,
        messages: list[dict],
        response_format: type[BaseModel],
        temperature: float,
    ) -> dict:
        schema = response_format.model_json_schema()
        instruction = (
            "请严格按照以下 JSON Schema 输出，不要包含任何解释或 markdown 代码块标记，只输出纯 JSON：\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )
        if messages and messages[0].get("role") == "system":
            merged = [
                {"role": "system", "content": f"{messages[0]['content']}\n\n{instruction}"},
                *messages[1:],
            ]
        else:
            merged = [{"role": "system", "content": instruction}, *messages]
        raw = self.chat_with_messages(merged, temperature)
        data = _extract_json(raw)
        return response_format.model_validate(data).model_dump()
