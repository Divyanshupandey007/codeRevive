import json
import os
import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel
import requests

from app.config import settings
from app.llm.base import (
    LLMProvider,
    LLMProviderError,
    LLMStructuredOutputError,
    parse_structured_json,
)

T = TypeVar("T", bound=BaseModel)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _extract_content_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


class OpenRouterProvider(LLMProvider):
    """OpenRouter Chat Completions provider (default model: openai/gpt-oss-120b)."""

    supports_concurrency: bool = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_enabled: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
        site_url: Optional[str] = None,
        app_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        session: Optional[Any] = None,
        max_retries: int = 4,
    ):
        resolved_key = (
            api_key
            if api_key is not None
            else (settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", ""))
        ).strip()
        if not resolved_key:
            raise ValueError(
                "No OpenRouter API key configured. Set CODEREVIVE_OPENROUTER_API_KEY "
                "in backend/.env (or export OPENROUTER_API_KEY)."
            )

        self._api_key = resolved_key
        self.base_url = (
            settings.openrouter_base_url if base_url is None else base_url
        ).rstrip("/")
        self.model = (
            settings.resolve_llm_model("openrouter") if model is None else model
        )
        self.reasoning_enabled = (
            settings.openrouter_reasoning_enabled
            if reasoning_enabled is None
            else reasoning_enabled
        )
        self.reasoning_effort = (
            settings.openrouter_reasoning_effort
            if reasoning_effort is None
            else reasoning_effort
        )
        self.site_url = settings.openrouter_site_url if site_url is None else site_url
        self.app_name = settings.openrouter_app_name if app_name is None else app_name
        self.timeout_seconds = (
            settings.openrouter_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self._http = session if session is not None else requests.Session()
        self._max_retries = max(1, max_retries)
        self.last_reasoning_details: Optional[Any] = None

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        return headers

    def _build_reasoning_config(self) -> Dict[str, Any]:
        if not self.reasoning_enabled:
            return {"enabled": False}
        cfg: Dict[str, Any] = {"enabled": True}
        if self.reasoning_effort:
            cfg["effort"] = self.reasoning_effort
        return cfg

    def chat_messages(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.2,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a raw chat-completions request, preserving reasoning_details."""
        url = f"{self.base_url}/chat/completions"
        headers = self._build_headers()
        active_response_format = response_format
        include_reasoning = True
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries):
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }
            if include_reasoning:
                payload["reasoning"] = self._build_reasoning_config()
            if active_response_format is not None:
                payload["response_format"] = active_response_format

            try:
                resp = self._http.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise LLMProviderError(
                    f"OpenRouter request failed after {self._max_retries} attempt(s): {exc}"
                ) from exc

            if resp.status_code in _RETRYABLE_STATUS_CODES:
                body_preview = (getattr(resp, "text", "") or "")[:300]
                last_exc = LLMProviderError(
                    f"OpenRouter HTTP {resp.status_code}: {body_preview}"
                )
                if attempt < self._max_retries - 1:
                    retry_after = getattr(resp, "headers", {}).get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else float(2 ** (attempt + 1))
                    )
                    time.sleep(min(delay, 30.0))
                    continue
                raise last_exc

            if resp.status_code in (400, 422) and attempt < self._max_retries - 1:
                body_text = (getattr(resp, "text", "") or "").lower()
                if active_response_format is not None and any(
                    tok in body_text
                    for tok in ("response_format", "json_object", "json_schema", "structured")
                ):
                    active_response_format = None
                    continue
                if include_reasoning and "reasoning" in body_text:
                    include_reasoning = False
                    continue

            if resp.status_code >= 400:
                body_preview = (getattr(resp, "text", "") or "")[:500]
                raise LLMProviderError(
                    f"OpenRouter API error HTTP {resp.status_code}: {body_preview}"
                )

            try:
                data = resp.json()
            except Exception as exc:
                raise LLMProviderError(
                    f"OpenRouter returned invalid JSON response: {exc}"
                ) from exc

            if "error" in data and not data.get("choices"):
                raise LLMProviderError(f"OpenRouter error response: {data['error']}")

            choices = data.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                raise LLMProviderError(
                    f"OpenRouter response missing 'choices': {str(data)[:500]}"
                )

            message = choices[0].get("message") or {}
            if not isinstance(message, dict):
                raise LLMProviderError(
                    f"OpenRouter response missing 'message' object: {str(choices[0])[:500]}"
                )

            self.last_reasoning_details = message.get("reasoning_details")
            return message

        raise LLMProviderError(
            f"OpenRouter request failed: {last_exc}"
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

        message = self.chat_messages(messages=messages, temperature=temperature)
        return _extract_content_text(message)

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

        message = self.chat_messages(
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )

        raw_text = _extract_content_text(message)
        if not raw_text.strip():
            hint = (
                " (content was empty while reasoning_details were returned)"
                if self.last_reasoning_details
                else ""
            )
            raise LLMStructuredOutputError(
                f"OpenRouter returned empty content for {response_schema.__name__}{hint}."
            )
        return parse_structured_json(
            raw_text, response_schema, provider_name="OpenRouter"
        )
