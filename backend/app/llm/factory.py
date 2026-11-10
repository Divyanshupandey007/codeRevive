from pathlib import Path
import sys
from typing import Optional

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.config import settings
from app.llm.base import LLMProvider

_PROVIDERS=("gemini","nvidia")

class LLMFactory:
    @staticmethod
    def create(provider:Optional[str]=None)->LLMProvider:
        resolved=(provider if provider is not None else settings.llm_provider).lower()

        if resolved=="gemini":
            from app.llm.gemini_provider import GeminiProvider
            return GeminiProvider()
        
        if resolved=="nvidia":
            from app.llm.nvidia_nim_provider import NvidiaNimProvider
            return NvidiaNimProvider()

        raise ValueError(
            f"Unsupported LLM provider: {resolved!r}. Supported: {', '.join(_PROVIDERS)}"
        ) 