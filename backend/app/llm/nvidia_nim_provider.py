import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

from app.config import settings
from app.llm.base import (
    LLMProvider,
    LLMProviderError,
    LLMStructuredOutputError,
    call_with_retry,
    detect_repetition_loop,
    parse_structured_json,
    strip_reasoning_blocks,
)

T = TypeVar("T", bound=BaseModel)

_MAX_TOKEN_STEP_DOWNS: Tuple[Optional[int], ...] = (16384, 8192, None)


def _is_client_400_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (400, 422):
        return True
    msg = str(exc).lower()
    return (
        "400" in msg
        or "422" in msg
        or "bad request" in msg
        or "unprocessable" in msg
        or "invalid_request_error" in msg
    )


def _error_mentions_any(exc: Exception, tokens: Tuple[str, ...]) -> bool:
    msg = str(exc).lower()
    return any(tok in msg for tok in tokens)


def _build_flat_json_example(response_schema: Type[BaseModel]) -> str:
    """Render a flat JSON key example so models emit top-level fields, not a JSON Schema wrapper."""
    fields = getattr(response_schema, "model_fields", None) or {}
    if not fields:
        return json.dumps(response_schema.model_json_schema())
    example = {name: f"<{name} string>" for name in fields}
    return json.dumps(example)


def _stitch_continuation(existing: str, continuation: str) -> str:
    """Append a continuation chunk while trimming any brief echoed overlap at the seam."""
    if not continuation:
        return existing
    if not existing:
        return continuation

    piece = continuation
    # Trim a leading markdown fence if the model wrapped only the continuation chunk
    stripped_l = piece.lstrip()
    if stripped_l.startswith("```") and "\n" in stripped_l and not existing.rstrip().endswith("```"):
        after_fence = stripped_l.split("\n", 1)[1]
        if after_fence:
            piece = after_fence

    max_overlap = min(len(existing), len(piece), 200)
    for overlap in range(max_overlap, 7, -1):
        if existing.endswith(piece[:overlap]):
            return existing + piece[overlap:]
    return existing + piece


def _extract_message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif hasattr(item, "text") and isinstance(item.text, str):
                parts.append(item.text)
        return "".join(parts)
    return ""


class NvidiaNimProvider(LLMProvider):
    """NVIDIA NIM OpenAI-compatible provider with streaming, thinking control, and auto-continuation."""

    supports_concurrency: bool = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 4,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        stream: Optional[bool] = None,
        enable_thinking: Optional[bool] = None,
        max_continuations: int = 2,
    ):
        resolved_key = (
            api_key
            if api_key is not None
            else (
                settings.nvidia_api_key
                or os.environ.get("CODEREVIVE_NVIDIA_API_KEY", "")
                or os.environ.get("NVIDIA_API_KEY", "")
            )
        ).strip()
        if not resolved_key:
            raise ValueError(
                "No NVIDIA NIM API key configured. Set CODEREVIVE_NVIDIA_API_KEY in backend/.env"
            )

        self.base_url = (
            settings.nvidia_base_url if base_url is None else base_url
        ).rstrip("/")
        self.model = (
            settings.resolve_llm_model("nvidia") if model is None else model
        )
        self._max_retries = max(1, max_retries)
        self.max_tokens = (
            settings.nvidia_max_tokens if max_tokens is None else max_tokens
        )
        self.timeout_seconds = (
            settings.nvidia_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self.stream = settings.nvidia_stream if stream is None else stream
        self.enable_thinking = (
            settings.nvidia_enable_thinking
            if enable_thinking is None
            else enable_thinking
        )
        self.max_continuations = max(0, max_continuations)

        # Learned per-model endpoint capabilities (safe under concurrent workers)
        self._cap_lock = threading.Lock()
        self._supports_chat_template_kwargs: bool = True
        self._supports_response_format: bool = True
        self._effective_max_tokens: Optional[int] = (
            self.max_tokens if self.max_tokens and self.max_tokens > 0 else None
        )

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package is not installed. Run: pip install openai"
            ) from exc

        try:
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=resolved_key,
                timeout=self.timeout_seconds,
            )
        except TypeError:
            self._client = OpenAI(base_url=self.base_url, api_key=resolved_key)

    def _build_messages(
        self, prompt: str, system_instruction: Optional[str] = None
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _collect_completion(
        self, completion: Any
    ) -> Tuple[str, Optional[str], Optional[str], int]:
        """Collect (content, finish_reason, repetition_loop_reason, reasoning_chars)."""
        # Non-streaming response object (also used by unit-test mocks)
        if hasattr(completion, "choices"):
            choices = getattr(completion, "choices", None) or []
            if not choices:
                raise LLMProviderError("NVIDIA NIM returned no completion choices.")
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            message = getattr(choice, "message", None)
            content = _extract_message_text(message) if message is not None else ""
            reasoning = getattr(message, "reasoning_content", None) if message is not None else None
            reasoning_chars = len(str(reasoning)) if reasoning else 0
            loop_reason = detect_repetition_loop(content) if content else None
            return content, finish_reason, loop_reason, reasoning_chars

        # Streaming chunk iterator (matches NVIDIA NIM's official OpenAI SDK pattern)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        finish_reason: Optional[str] = None
        reasoning_chars = 0
        next_reasoning_check_at = 512
        total_chars = 0
        next_loop_check_at = 256
        loop_reason: Optional[str] = None

        try:
            for chunk in completion:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_str = str(reasoning)
                    reasoning_parts.append(reasoning_str)
                    reasoning_chars += len(reasoning_str)
                    if reasoning_chars >= next_reasoning_check_at:
                        r_tail = "".join(reasoning_parts)[-1000:]
                        r_loop = detect_repetition_loop(r_tail)
                        if r_loop:
                            loop_reason = f"Reasoning stream loop: {r_loop}"
                            break
                        next_reasoning_check_at = reasoning_chars + 512
                piece = getattr(delta, "content", None)
                if piece is not None:
                    piece_str = str(piece)
                    content_parts.append(piece_str)
                    total_chars += len(piece_str)
                    if total_chars >= next_loop_check_at:
                        tail = "".join(content_parts)[-1000:]
                        loop_reason = detect_repetition_loop(tail)
                        if loop_reason:
                            break
                        next_loop_check_at = total_chars + 160
        finally:
            if loop_reason and hasattr(completion, "close"):
                try:
                    completion.close()
                except Exception:
                    pass

        return "".join(content_parts), finish_reason, loop_reason, reasoning_chars

    def _next_lower_max_tokens(self, current: Optional[int]) -> Optional[int]:
        if current is None:
            return None
        for step in _MAX_TOKEN_STEP_DOWNS:
            if step is None or step < current:
                return step
        return None

    def _invoke_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        enable_thinking: bool,
        response_format: Optional[Dict[str, Any]] = None,
        frequency_penalty: Optional[float] = None,
    ) -> Tuple[str, Optional[str], Optional[str], int]:
        """Execute one NVIDIA NIM chat completion call with adaptive 400 parameter fallbacks."""
        with self._cap_lock:
            use_thinking = self._supports_chat_template_kwargs
            active_max_tokens = self._effective_max_tokens
            active_response_format = (
                response_format if self._supports_response_format else None
            )
        active_freq_penalty = frequency_penalty
        use_top_p = True

        def _call() -> Tuple[str, Optional[str], Optional[str], int]:
            nonlocal use_thinking, active_max_tokens, active_response_format, active_freq_penalty, use_top_p

            for _ in range(7):
                request_kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                }
                if use_top_p:
                    request_kwargs["top_p"] = 0.95
                if self.stream:
                    request_kwargs["stream"] = True
                if active_max_tokens is not None and active_max_tokens > 0:
                    request_kwargs["max_tokens"] = active_max_tokens
                if use_thinking:
                    request_kwargs["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": enable_thinking}
                    }
                if active_response_format is not None:
                    request_kwargs["response_format"] = active_response_format
                if active_freq_penalty is not None:
                    request_kwargs["frequency_penalty"] = active_freq_penalty

                try:
                    completion = self._client.chat.completions.create(**request_kwargs)
                except Exception as exc:
                    if not _is_client_400_error(exc):
                        raise

                    if use_thinking and _error_mentions_any(
                        exc, ("chat_template_kwargs", "enable_thinking", "extra_body")
                    ):
                        use_thinking = False
                        with self._cap_lock:
                            self._supports_chat_template_kwargs = False
                        continue

                    if active_max_tokens is not None and _error_mentions_any(
                        exc, ("max_tokens", "max_completion_tokens", "maximum", "context length")
                    ):
                        next_cap = self._next_lower_max_tokens(active_max_tokens)
                        active_max_tokens = next_cap
                        with self._cap_lock:
                            self._effective_max_tokens = next_cap
                        continue

                    if active_response_format is not None and _error_mentions_any(
                        exc, ("response_format", "json_object", "structured")
                    ):
                        active_response_format = None
                        with self._cap_lock:
                            self._supports_response_format = False
                        continue

                    if active_freq_penalty is not None and _error_mentions_any(
                        exc, ("frequency_penalty",)
                    ):
                        active_freq_penalty = None
                        continue

                    if use_top_p and _error_mentions_any(exc, ("top_p",)):
                        use_top_p = False
                        continue

                    # Generic 400 fallback: drop optional extras one by one
                    if use_thinking:
                        use_thinking = False
                        with self._cap_lock:
                            self._supports_chat_template_kwargs = False
                        continue
                    if active_response_format is not None:
                        active_response_format = None
                        with self._cap_lock:
                            self._supports_response_format = False
                        continue
                    if use_top_p:
                        use_top_p = False
                        continue
                    raise

                return self._collect_completion(completion)

            raise LLMProviderError(
                f"NVIDIA NIM request failed after parameter fallbacks for model {self.model}."
            )

        return call_with_retry(_call, "NVIDIA NIM", max_retries=self._max_retries)

    def _complete_with_continuations(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        enable_thinking: bool,
        response_format: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Type[T]] = None,
        frequency_penalty: Optional[float] = None,
    ) -> Tuple[str, Optional[str], Optional[str], int]:
        """Run a completion and automatically continue if finish_reason == 'length'."""
        accumulated, finish_reason, loop_reason, reasoning_chars = self._invoke_completion(
            messages=messages,
            temperature=temperature,
            enable_thinking=enable_thinking,
            response_format=response_format,
            frequency_penalty=frequency_penalty,
        )

        continuations = 0
        while (
            finish_reason == "length"
            and loop_reason is None
            and accumulated.strip()
            and continuations < self.max_continuations
        ):
            # If the structured JSON already parses cleanly, stop without another call
            if response_schema is not None:
                try:
                    parse_structured_json(
                        accumulated, response_schema, provider_name="NVIDIA NIM"
                    )
                    finish_reason = "stop"
                    break
                except LLMStructuredOutputError:
                    pass

            continuations += 1
            continuation_prompt = (
                "Your previous JSON response was cut off at the max_tokens limit. "
                "Continue IMMEDIATELY from the exact character where you stopped — "
                "do NOT repeat prior text, do NOT restart the JSON object, and do NOT add markdown fences. "
                "Finish the remaining content and close the JSON string and object with `}`."
                if response_schema is not None
                else (
                    "Your previous response was cut off at the max_tokens limit. "
                    "Continue immediately from the exact character where you stopped without repeating prior text."
                )
            )
            cont_messages = list(messages) + [
                {"role": "assistant", "content": accumulated},
                {"role": "user", "content": continuation_prompt},
            ]
            # Do NOT force json_object on a mid-JSON continuation (it would force a fresh '{').
            next_piece, finish_reason, loop_reason, extra_reasoning = self._invoke_completion(
                messages=cont_messages,
                temperature=temperature,
                enable_thinking=False,
                response_format=None,
                frequency_penalty=frequency_penalty,
            )
            reasoning_chars += extra_reasoning
            if not next_piece:
                break
            accumulated = _stitch_continuation(accumulated, next_piece)
            if loop_reason is None:
                loop_reason = detect_repetition_loop(accumulated)

        return accumulated, finish_reason, loop_reason, reasoning_chars

    def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        messages = self._build_messages(prompt, system_instruction)
        content, _, _, _ = self._complete_with_continuations(
            messages=messages,
            temperature=temperature,
            enable_thinking=self.enable_thinking,
            response_format=None,
        )
        return strip_reasoning_blocks(content)

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
    ) -> T:
        json_example = _build_flat_json_example(response_schema)
        schema_guide = (
            "Respond ONLY with a single valid JSON object (no markdown fences, no commentary) "
            f"using these exact top-level keys:\n{json_example}\n"
            "Do NOT wrap the object inside a `properties` or schema envelope. "
            "Keep any non-ASCII characters (e.g. Chinese/Japanese comments) as raw UTF-8 characters "
            "inside JSON strings rather than \\uXXXX escape sequences."
        )
        combined_system = (
            f"{system_instruction}\n\n{schema_guide}"
            if system_instruction
            else schema_guide
        )
        base_messages = self._build_messages(prompt, combined_system)

        # Use fast non-thinking mode by default for bulk passes so 100% of max_tokens goes
        # to code output, but automatically engage model thinking on stubborn repair retries
        # (repeat failures / NO-OP retries / elevated temperature) so thinking models like
        # Nemotron Ultra can reason through tricky compiler type/import conflicts.
        is_stubborn_repair = (
            temperature >= 0.2
            or "REPEAT REPAIR FAILURE" in prompt
            or "CRITICAL NO-OP CORRECTION" in prompt
        )
        attempt_1_thinking = self.enable_thinking or is_stubborn_repair

        # Some NIM vLLM backends enter \uXXXX loops under constrained json_object when
        # the prompt contains non-ASCII text, or clash with thinking mode; leave
        # response_format unconstrained when thinking or non-ASCII text is active.
        has_non_ascii = any(ord(ch) > 127 for ch in prompt)
        first_format: Optional[Dict[str, Any]] = (
            None if (has_non_ascii or attempt_1_thinking) else {"type": "json_object"}
        )
        retry_format: Optional[Dict[str, Any]] = (
            None if has_non_ascii else {"type": "json_object"}
        )

        # Attempt 1
        raw_text, finish_reason, loop_reason, reasoning_chars = (
            self._complete_with_continuations(
                messages=base_messages,
                temperature=temperature,
                enable_thinking=attempt_1_thinking,
                response_format=first_format,
                response_schema=response_schema,
            )
        )

        first_error: Optional[Exception] = None
        if loop_reason:
            first_error = LLMStructuredOutputError(
                f"NVIDIA NIM entered a degenerate repetition loop: {loop_reason}"
            )
        elif not raw_text.strip():
            hint = (
                f" ({reasoning_chars} reasoning chars consumed max_tokens)"
                if reasoning_chars
                else ""
            )
            first_error = LLMStructuredOutputError(
                f"NVIDIA NIM returned empty content for {response_schema.__name__}{hint}."
            )
        else:
            try:
                return parse_structured_json(
                    raw_text, response_schema, provider_name="NVIDIA NIM"
                )
            except LLMStructuredOutputError as exc:
                first_error = exc

        # Attempt 2: 1 clean corrective retry (always with enable_thinking=False so 100%
        # of max_tokens goes to JSON output; add mild frequency_penalty after a loop)
        retry_note = (
            f"Previous response failed ({first_error}). "
            f"Return ONLY one complete, valid JSON object with exact top-level keys {json_example}. "
            "Do not output reasoning, markdown fences, or repeated tokens."
        )
        retry_messages = self._build_messages(
            f"{prompt}\n\nCORRECTION: {retry_note}", combined_system
        )
        raw_text_2, finish_reason_2, loop_reason_2, _ = (
            self._complete_with_continuations(
                messages=retry_messages,
                temperature=max(temperature, 0.2) if loop_reason else temperature,
                enable_thinking=False,
                response_format=retry_format,
                response_schema=response_schema,
                frequency_penalty=0.25 if loop_reason else None,
            )
        )

        if loop_reason_2:
            raise LLMStructuredOutputError(
                f"NVIDIA NIM produced degenerate output on retry for {response_schema.__name__}: {loop_reason_2}"
            )
        if not raw_text_2.strip():
            raise LLMStructuredOutputError(
                f"NVIDIA NIM returned empty content on retry for {response_schema.__name__} "
                f"(finish_reason={finish_reason_2})."
            )
        return parse_structured_json(
            raw_text_2, response_schema, provider_name="NVIDIA NIM"
        )
