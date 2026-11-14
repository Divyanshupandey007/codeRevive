from pathlib import Path
import sys
from typing import Optional, TypeVar

from pydantic import BaseModel

from app.config import settings

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.llm.base import LLMProvider, LLMStructuredOutputError
T=TypeVar("T",bound=BaseModel)

class GeminiProvider(LLMProvider):
    def __init__(self,api_key:Optional[str]=None,model:Optional[str]=None):
        resolved_key=settings.gemini_api_key if api_key is None else api_key
        if not resolved_key:
            raise ValueError(
                "No Gemini API key configured. Set CODEREVIVE_GEMINI_API_KEY in backend/.env"
            )
        self.model=settings.gemini_model if model is None else model

        try:
            from google import genai
        except ImportError as exc:
            raise ImportError(
                "The google-genai package isn't installed" 
            ) from exc

        self._client=genai.Client(api_key=resolved_key)

    def generate_text(self, prompt, system_instruction = None, temperature = 0.2):
        from google.genai import types
        import time

        max_retries = 4
        for attempt in range(max_retries):
            try:
                response=self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=temperature
                    )
                )
                return response.text or ""
            except Exception as exc:
                if attempt < max_retries - 1 and any(code in str(exc) for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise

    def generate_structured(self, prompt, response_schema, system_instruction = None, temperature = 0.1)->T:
        from google.genai import types
        import time

        max_retries = 4
        response = None
        for attempt in range(max_retries):
            try:
                response=self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_schema=response_schema
                    )
                )
                break
            except Exception as exc:
                if attempt < max_retries - 1 and any(code in str(exc) for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise

        if isinstance(response.parsed,response_schema):
            return response.parsed

        raw_text=(response.text or "").strip()
        if raw_text.startswith("```"):
            raw_text=raw_text.strip("`")
            if raw_text.lower().startswith("json"):
                raw_text=raw_text[4:].strip()

        try:
            return response_schema.model_validate_json(raw_text)
        except Exception as exc:
            raise LLMStructuredOutputError(
                f"Gemini response could not be parsed into {response_schema.__name__}: {exc}\n"
                f"Raw response (first 500 chars): {raw_text[:500]}" 
            ) from exc