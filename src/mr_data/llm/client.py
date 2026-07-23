import json
from typing import Optional

from openai import OpenAI

from mr_data.config import settings


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

    def chat_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: type,
        temperature: float = 0.2,
    ) -> dict:
        resp = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=response_format,
            temperature=temperature,
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)

    def structured_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: type,
        temperature: float = 0.2,
    ) -> dict:
        """Unified structured output with automatic fallback for non-parse endpoints."""
        try:
            return self.chat_structured(system_prompt, user_prompt, response_format, temperature)
        except Exception:
            schema = response_format.model_json_schema()
            fallback_system = (
                f"{system_prompt}\n\n"
                "请严格按照以下 JSON Schema 输出，不要包含任何解释或 markdown 代码块标记，只输出纯 JSON：\n"
                f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
            )
            raw = self.chat(fallback_system, user_prompt, temperature).strip()
            cleaned = raw.removeprefix("```json").removesuffix("```").strip()
            data = json.loads(cleaned)
            return response_format.model_validate(data).model_dump()

