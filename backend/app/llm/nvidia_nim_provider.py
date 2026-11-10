from pathlib import Path
import sys
from typing import Optional, TypeVar

from pydantic import BaseModel

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.llm.base import LLMProvider, LLMStructuredOutputError
from app.config import settings

T=TypeVar("T",bound=BaseModel)

class NvidiaNimProvider(LLMProvider):
    def __init__(self,api_key:Optional[str]=None,base_url:Optional[str]=None,model:Optional[str]=None):
        resolved_key=settings.nvidia_api_key if api_key is None else api_key
        if not resolved_key:
            raise ValueError(
                "No NVIDIA API key configured. Set CODEREVIVE_NVIDIA_API_KEY "
            )

        self.base_url=settings.nvidia_base_url if base_url is None else base_url
        self.model=settings.nvidia_model if model is None else model

        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "The openai package isn't installed"
            ) from exc

        self._client=openai.OpenAI(api_key=resolved_key,base_url=self.base_url)

    def generate_text(self, prompt, system_instruction = None, temperature = 0.2)->str:
        messages=[]
        if system_instruction:
            messages.append({"role":"system","content":system_instruction})
        messages.append({"role":"user","content":prompt})

        response=self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature
        )
        return response.choices[0].message.content or ""

    def generate_structured(self, prompt, response_schema, system_instruction = None, temperature = 0.1)->T:
        schema_instruction=(
            "Respond with ONLY a single JSON object matching this JSON Schema exactly - "
            "no markdown code fences, no commentary before or after it:\n"
            f"{response_schema.model_json_schema()}"
        )

        messages=[]
        if system_instruction:
            messages.append({"role":"system","content":system_instruction})
        messages.append({"role":"user","content":f"{prompt}\n\n{schema_instruction}"})

        response=self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            response_format={"type":"json_object"}
        )

        raw_text=(response.choices[0].message.content or "").strip()
        if raw_text.startswith("```"):
            raw_text=raw_text.strip()
            if raw_text.lower().startswith("json"):
                raw_text=raw_text[4:].strip()

        try:
            return response_schema.model_validate_json(raw_text)
        except Exception as exc:
            raise LLMStructuredOutputError(
                f"NVIDIA NIM response could not be parsed into {response_schema.__name__}: {exc}\n"
                f"Raw response (first 500 chars): {raw_text[:500]}"
            ) from exc
