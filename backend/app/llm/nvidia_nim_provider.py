import json
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from app.config import settings
from app.llm.base import (
    LLMProvider,
    LLMProviderError,
    LLMStructuredOutputError,
    call_with_retry,
    extract_json_payload,
)

T = TypeVar("T", bound=BaseModel)


class NvidiaNimProvider(LLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: int = 4,
    ):
        resolved_key = settings.nvidia_api_key if api_key is None else api_key
        if not resolved_key:
            raise ValueError(
                "No NVIDIA API key configured. Set CODEREVIVE_NVIDIA_API_KEY"
            )
        self.base_url = settings.nvidia_base_url if base_url is None else base_url
        self.model = settings.nvidia_model if model is None else model
        self.timeout_seconds = (
            settings.openrouter_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self._max_retries = max(1, max_retries)

        try:
            import openai
        except ImportError as exc:
            raise ImportError("The openai package isn't installed") from exc

        self._client = openai.OpenAI(
            api_key=resolved_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        messages: List[Dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        response = call_with_retry(
            lambda: self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            ),
            provider_name="NVIDIA NIM",
            max_retries=self._max_retries,
        )
        if not getattr(response, "choices", None):
            raise LLMProviderError("NVIDIA NIM API returned no completion choices")
        return response.choices[0].message.content or ""

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
    ) -> T:
        schema_instruction = (
            "Respond with ONLY a single valid JSON object matching this JSON Schema exactly - "
            "no markdown code fences, no commentary before or after it:\n"
            f"{json.dumps(response_schema.model_json_schema())}"
        )
        messages: List[Dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": f"{prompt}\n\n{schema_instruction}"})

        response = call_with_retry(
            lambda: self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            ),
            provider_name="NVIDIA NIM",
            max_retries=self._max_retries,
        )
        if not getattr(response, "choices", None):
            raise LLMProviderError("NVIDIA NIM API returned no completion choices")
        raw_text = (response.choices[0].message.content or "").strip()
        cleaned = extract_json_payload(raw_text)
        try:
            return response_schema.model_validate_json(cleaned)
        except Exception as exc:
            raise LLMStructuredOutputError(
                f"NVIDIA NIM response could not be parsed into {response_schema.__name__}: {exc}\n"
                f"Raw response (first 500 chars): {raw_text[:500]}"
            ) from exc
