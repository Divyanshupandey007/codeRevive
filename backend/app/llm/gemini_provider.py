from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from app.config import settings
from app.llm.base import (
    LLMProvider,
    LLMStructuredOutputError,
    call_with_retry,
    extract_json_payload,
)

T = TypeVar("T", bound=BaseModel)


class GeminiProvider(LLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 4,
    ):
        resolved_key = settings.gemini_api_key if api_key is None else api_key
        if not resolved_key:
            raise ValueError(
                "No Gemini API key configured. Set CODEREVIVE_GEMINI_API_KEY in backend/.env"
            )
        self.model = settings.gemini_model if model is None else model
        self._max_retries = max(1, max_retries)

        try:
            from google import genai
        except ImportError as exc:
            raise ImportError("The google-genai package isn't installed") from exc

        self._client = genai.Client(api_key=resolved_key)

    def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        from google.genai import types

        response = call_with_retry(
            lambda: self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                ),
            ),
            provider_name="Gemini",
            max_retries=self._max_retries,
        )
        return response.text or ""

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
    ) -> T:
        from google.genai import types

        response = call_with_retry(
            lambda: self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                ),
            ),
            provider_name="Gemini",
            max_retries=self._max_retries,
        )

        if isinstance(response.parsed, response_schema):
            return response.parsed

        raw_text = response.text or ""
        cleaned = extract_json_payload(raw_text)
        try:
            return response_schema.model_validate_json(cleaned)
        except Exception as exc:
            raise LLMStructuredOutputError(
                f"Gemini response could not be parsed into {response_schema.__name__}: {exc}\n"
                f"Raw response (first 500 chars): {raw_text[:500]}"
            ) from exc