from abc import ABC, abstractmethod
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

T=TypeVar("T",bound=BaseModel)

class LLMProviderError(Exception):
    """Base class for all LLM-provider-related failures (config, transport,
    parsing). Catch this instead of a bare Exception if the orchestrator
    needs to distinguish "the LLM layer broke" from everything else."""

class LLMStructuredOutputError(LLMProviderError):
    """Raised when a provider's response could not be parsed into the
    requested response_schema - malformed/truncated JSON, or the shape
    just doesn't match. The agent/orchestrator decides what to do (retry
    with a stricter prompt, fall back, surface to a human) - this class
    only signals that it happened."""

class LLMProvider(ABC):
    @abstractmethod
    def generate_text(
        self,
        prompt:str,
        system_instruction:Optional[str]=None,
        temperature:float=0.2
    )->str:
        ...

    @abstractmethod
    def generate_structured(
        self,
        prompt:str,
        response_schema:Type[T],
        system_instruction:Optional[str]=None,
        temperature:float=0.1
    )->T:
        ...