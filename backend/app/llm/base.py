from abc import ABC, abstractmethod
from typing import Callable, Optional, Tuple, Type, TypeVar
import time

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

RETRYABLE_ERROR_TOKENS: Tuple[str, ...] = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "unavailable",
    "resource_exhausted",
    "rate limit",
    "timeout",
    "timed out",
    "connection reset",
)


class LLMProviderError(Exception):
    """Base class for all LLM-provider-related failures (config, transport, parsing)."""


class LLMStructuredOutputError(LLMProviderError):
    """Raised when a provider's response could not be parsed into the requested schema."""


def extract_json_payload(raw_text: str) -> str:
    """Strip markdown fences or surrounding prose to isolate the first JSON object."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if text.startswith("{") and text.endswith("}"):
        return text

    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    return text[start:]


def is_retryable_llm_exception(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in RETRYABLE_ERROR_TOKENS)


def call_with_retry(
    fn: Callable[[], T],
    provider_name: str,
    max_retries: int = 4,
    max_backoff_seconds: float = 30.0,
) -> T:
    """Execute an LLM SDK call with exponential backoff on transient failures."""
    attempts = max(1, max_retries)
    for attempt in range(attempts):
        try:
            return fn()
        except LLMProviderError:
            raise
        except Exception as exc:
            if attempt < attempts - 1 and is_retryable_llm_exception(exc):
                delay = min(float(2 ** (attempt + 1)), max_backoff_seconds)
                time.sleep(delay)
                continue
            raise LLMProviderError(f"{provider_name} API call failed: {exc}") from exc
    raise LLMProviderError(f"{provider_name} API call failed after {attempts} attempts")


class LLMProvider(ABC):
    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        ...

    @abstractmethod
    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
    ) -> T:
        ...