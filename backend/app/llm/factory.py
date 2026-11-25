from typing import Optional

from app.config import settings
from app.llm.base import LLMProvider

_PROVIDERS = ("gemini", "nvidia", "openrouter")


class LLMFactory:
    @staticmethod
    def create(provider: Optional[str] = None) -> LLMProvider:
        resolved = (
            provider if provider is not None else settings.resolve_llm_provider()
        ).strip().lower()

        if resolved == "gemini":
            from app.llm.gemini_provider import GeminiProvider

            return GeminiProvider()

        if resolved == "nvidia":
            from app.llm.nvidia_nim_provider import NvidiaNimProvider

            return NvidiaNimProvider()

        if resolved == "openrouter":
            from app.llm.openrouter_provider import OpenRouterProvider

            return OpenRouterProvider()

        raise ValueError(
            f"Unsupported LLM provider: {resolved!r}. Supported: {', '.join(_PROVIDERS)}"
        )