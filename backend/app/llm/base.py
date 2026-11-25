from abc import ABC, abstractmethod
import json
import re
from typing import Any, Callable, List, Optional, Tuple, Type, TypeVar
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

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")
_REPEATED_UNICODE_ESCAPE_RE = re.compile(r"(\\u[0-9a-fA-F]{4})\1{7,}")
_REPEATED_WORD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{1,24})\b(?:[ \t]+\1){7,}")
_INVALID_JSON_ESCAPE_RE = re.compile(
    r'(?<!\\)((?:\\\\)*)\\(?:u(?![0-9a-fA-F]{4})|([^"\\/bfnrtu]))'
)


class LLMProviderError(Exception):
    """Base class for all LLM-provider-related failures (config, transport, parsing)."""


class LLMStructuredOutputError(LLMProviderError):
    """Raised when a provider's response could not be parsed into the requested schema."""


def strip_reasoning_blocks(raw_text: str) -> str:
    """Remove <think>...</think> reasoning blocks emitted by thinking models."""
    if not raw_text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", raw_text)
    if "<think" in cleaned.lower():
        # If the closing </think> tag exists without an opening tag, keep only after it.
        lower = cleaned.lower()
        close_idx = lower.rfind("</think>")
        if close_idx != -1:
            cleaned = cleaned[close_idx + len("</think>") :]
        else:
            cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    elif "</think>" in cleaned.lower():
        close_idx = cleaned.lower().rfind("</think>")
        cleaned = cleaned[close_idx + len("</think>") :]
    return cleaned.strip()


def _find_balanced_json_objects(text: str) -> List[str]:
    """Return all top-level balanced {...} substrings from text."""
    results: List[str] = []
    n = len(text)
    idx = 0
    while idx < n:
        start = text.find("{", idx)
        if start == -1:
            break
        depth = 0
        in_string = False
        escape = False
        matched_end = -1
        for pos in range(start, n):
            ch = text[pos]
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
                    matched_end = pos
                    break
        if matched_end != -1:
            results.append(text[start : matched_end + 1])
            idx = matched_end + 1
        else:
            results.append(text[start:])
            break
    return results


def iter_json_candidates(raw_text: str) -> List[str]:
    """Return candidate JSON object strings ordered from most to least likely."""
    cleaned = strip_reasoning_blocks(raw_text)
    candidates: List[str] = []
    seen = set()

    def _add(item: str) -> None:
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            candidates.append(s)

    # 1. Exact JSON object after stripping think tags
    if cleaned.startswith("{") and cleaned.endswith("}"):
        _add(cleaned)

    # 2. Markdown fenced blocks (prefer last fenced block first, since models
    # sometimes show an example or draft before the final answer)
    fenced_blocks = _FENCED_BLOCK_RE.findall(cleaned)
    for block in reversed(fenced_blocks):
        block_s = block.strip()
        if block_s.startswith("{") and block_s.endswith("}"):
            _add(block_s)
        for obj in reversed(_find_balanced_json_objects(block_s)):
            _add(obj)

    # 3. Balanced top-level {...} objects in cleaned text (last-to-first)
    for obj in reversed(_find_balanced_json_objects(cleaned)):
        _add(obj)

    # 3b. Outer { ... } span fallback (protects against unescaped inner quotes confusing brace scanner)
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        _add(cleaned[first_brace : last_brace + 1])

    # 4. Fallback: balanced objects from raw_text if stripping removed everything
    if not candidates and raw_text:
        for obj in reversed(_find_balanced_json_objects(raw_text)):
            _add(obj)

    if not candidates and cleaned:
        _add(cleaned)

    return candidates


def extract_json_payload(raw_text: str) -> str:
    """Strip reasoning tags, markdown fences, or prose to isolate the JSON object."""
    candidates = iter_json_candidates(raw_text)
    if not candidates:
        return raw_text.strip()

    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
            if isinstance(parsed, dict):
                return candidate
        except Exception:
            continue

    return candidates[0]


def _unwrap_schema_dict(data: Any, schema_name: str) -> List[Any]:
    """Return possible dict payloads, handling common LLM schema wrappers (including multi-key)."""
    variants: List[Any] = [data]
    if not isinstance(data, dict):
        return variants

    envelope_keys = {
        schema_name.lower(),
        "properties",
        "result",
        "output",
        "response",
        "data",
    }
    for key, val in data.items():
        if isinstance(val, dict) and key.lower() in envelope_keys:
            # Handle both {"properties": {...}} and {"properties": {...}, "explanation": "..."}
            merged = {k: v for k, v in data.items() if k != key and not isinstance(v, dict)}
            merged.update(val)
            variants.append(merged)
            if val != merged:
                variants.append(val)
    return variants


def detect_repetition_loop(text: str) -> Optional[str]:
    """Detect degenerate LLM repetition loops (\\uXXXX loops, repeated words/tokens) early."""
    if not text or len(text) < 64:
        return None

    tail = text[-800:]
    m = _REPEATED_UNICODE_ESCAPE_RE.search(tail)
    if m:
        return f"Repeated Unicode escape loop detected ({m.group(1)})"

    # Only flag a \uXXXX burst when diversity is very low (degenerate cycling like \u9009\u9898...),
    # so legitimate Chinese comments escaped as 30+ distinct \uXXXX codepoints are never rejected.
    recent_240 = tail[-240:]
    escapes = _UNICODE_ESCAPE_RE.findall(recent_240)
    if len(escapes) >= 24 and len(set(escapes)) <= max(4, len(escapes) // 6):
        return "Excessive repetitive \\uXXXX escape storm detected"

    # Word-level repetition loop (e.g. "type type type type type type type type")
    wm = _REPEATED_WORD_RE.search(tail)
    if wm:
        return f"Repeated word loop detected ({wm.group(1)!r})"

    # General short-token repetition loop at the tail (ignoring pure whitespace/separator runs)
    trimmed_tail = tail[-512:].rstrip('"} \t\r\n')
    for candidate_tail in (tail[-512:], trimmed_tail):
        if len(candidate_tail) < 36:
            continue
        for unit_len in range(2, 25):
            repeats = 12 if unit_len >= 3 else 16
            needed = unit_len * repeats
            if len(candidate_tail) < needed:
                continue
            unit = candidate_tail[-unit_len:]
            if not unit.strip():
                continue
            if len(set(unit)) == 1 and unit[0] in (" ", "\t", "\n", "\r", "=", "-", "*", "/", "#"):
                continue
            if candidate_tail.endswith(unit * repeats):
                return f"Repeated token loop detected ({unit!r})"

    return None


def _repair_invalid_json_escapes(candidate: str) -> str:
    """Double invalid JSON backslash escapes (e.g. \\s, \\d, \\., \\users) inside JSON text."""
    def _repl(match: re.Match[str]) -> str:
        even_slashes = match.group(1)
        bad_char = match.group(2)
        if bad_char is None:
            return f"{even_slashes}\\\\u"
        return f"{even_slashes}\\\\{bad_char}"

    return _INVALID_JSON_ESCAPE_RE.sub(_repl, candidate)


_JSON_OBJECT_KEY_AFTER_COMMA_RE = re.compile(r'\s*"(?:\\.|[^"\\\r\n])*"\s*:')
_JSON_ARRAY_NEXT_VALUE_RE = re.compile(r'\s*(?:"|\{|\[|-?\d|true\b|false\b|null\b)')


def _repair_unescaped_json_quotes(candidate: str) -> str:
    """Escape stray unescaped double-quotes inside JSON string values (e.g. Java code literals).

    Only invoked as a fallback after strict json.loads() fails (e.g. `Expecting ',' delimiter`).
    Tracks JSON container stack ('{' vs '[') and key/value position so legitimate closing quotes
    are preserved while unescaped quotes inside Java literals like `root.get("surveyId")` or
    `new String[]{"a"};` are escaped as `\\"`.
    """
    n = len(candidate)
    out: List[str] = []
    stack: List[str] = []
    expecting_key = False
    in_string = False
    in_value_string = False
    escape = False
    i = 0

    while i < n:
        ch = candidate[i]
        if not in_string:
            out.append(ch)
            if ch == "{":
                stack.append("{")
                expecting_key = True
            elif ch == "[":
                stack.append("[")
                expecting_key = False
            elif ch in ("}", "]"):
                if stack:
                    stack.pop()
                expecting_key = False
            elif ch == ":":
                expecting_key = False
            elif ch == ",":
                expecting_key = bool(stack and stack[-1] == "{")
            elif ch == '"':
                in_string = True
                in_value_string = not expecting_key
                escape = False
            i += 1
            continue

        if escape:
            out.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            i += 1
            continue
        if ch == '"':
            if not in_value_string:
                in_string = False
                out.append(ch)
                i += 1
                continue

            j = i + 1
            while j < n and candidate[j] in " \t\r\n":
                j += 1

            is_closing = False
            if j >= n:
                is_closing = True
            else:
                nxt = candidate[j]
                container = stack[-1] if stack else ""
                if container == "{":
                    if nxt == "}":
                        k = j + 1
                        while k < n and candidate[k] in " \t\r\n":
                            k += 1
                        if k >= n or candidate[k] in ",}]":
                            is_closing = True
                    elif nxt == ",":
                        if _JSON_OBJECT_KEY_AFTER_COMMA_RE.match(candidate, j + 1):
                            is_closing = True
                elif container == "[":
                    if nxt == "]":
                        k = j + 1
                        while k < n and candidate[k] in " \t\r\n":
                            k += 1
                        if k >= n or candidate[k] in ",}]":
                            is_closing = True
                    elif nxt == ",":
                        if _JSON_ARRAY_NEXT_VALUE_RE.match(candidate, j + 1):
                            is_closing = True
                else:
                    if nxt in ",}]":
                        is_closing = True

            if is_closing:
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _repair_invalid_unicode_escapes(candidate: str) -> str:
    """Backwards-compatible alias for _repair_invalid_json_escapes."""
    return _repair_invalid_json_escapes(candidate)


def _apply_safe_defaults(variant: Any, response_schema: Type[T]) -> Any:
    """Fill optional-like metadata fields (e.g. missing explanation) on otherwise valid patches."""
    if not isinstance(variant, dict):
        return variant
    model_fields = getattr(response_schema, "model_fields", None) or {}
    if (
        "file_path" in model_fields
        and "new_content" in model_fields
        and "explanation" in model_fields
        and isinstance(variant.get("file_path"), str)
        and variant["file_path"].strip()
        and isinstance(variant.get("new_content"), str)
        and variant["new_content"].strip()
        and not variant.get("explanation")
    ):
        patched_variant = dict(variant)
        patched_variant["explanation"] = "Applied migration patch."
        return patched_variant
    return variant


def parse_structured_json(
    raw_text: str,
    response_schema: Type[T],
    provider_name: str = "LLM",
) -> T:
    """Parse raw LLM output into response_schema, handling thinking tags and loose JSON."""
    candidates = iter_json_candidates(raw_text)
    if not candidates:
        raise LLMStructuredOutputError(
            f"{provider_name} returned empty content for {response_schema.__name__}."
        )

    last_exc: Optional[Exception] = None
    for candidate in candidates:
        parsed: Any = None
        try:
            parsed = json.loads(candidate, strict=False)
        except Exception as exc:
            last_exc = exc
            # Fallback deterministic JSON repairs: invalid backslash escapes and/or unescaped
            # inner double quotes inside Java string values (e.g. `root.get("surveyId")`).
            repair_attempts: List[str] = []
            esc_repaired = _repair_invalid_json_escapes(candidate)
            if esc_repaired != candidate:
                repair_attempts.append(esc_repaired)
            quote_repaired = _repair_unescaped_json_quotes(candidate)
            if quote_repaired != candidate and quote_repaired not in repair_attempts:
                repair_attempts.append(quote_repaired)
            if esc_repaired != candidate:
                both_repaired = _repair_unescaped_json_quotes(esc_repaired)
                if both_repaired not in repair_attempts and both_repaired != candidate:
                    repair_attempts.append(both_repaired)

            for repaired in repair_attempts:
                try:
                    parsed = json.loads(repaired, strict=False)
                    break
                except Exception as exc2:
                    last_exc = exc2
            if parsed is None:
                continue

        if parsed is None:
            continue

        for raw_variant in _unwrap_schema_dict(parsed, response_schema.__name__):
            if isinstance(raw_variant, dict) and isinstance(
                raw_variant.get("new_content"), str
            ):
                loop_reason = detect_repetition_loop(raw_variant["new_content"])
                if loop_reason:
                    raise LLMStructuredOutputError(
                        f"{provider_name} produced degenerate code output for "
                        f"{response_schema.__name__}: {loop_reason}"
                    )
            variant = _apply_safe_defaults(raw_variant, response_schema)
            try:
                return response_schema.model_validate(variant)
            except Exception as exc:
                last_exc = exc

    raise LLMStructuredOutputError(
        f"{provider_name} response could not be parsed into {response_schema.__name__}: {last_exc}\n"
        f"Raw response (first 500 chars): {raw_text[:500]}"
    ) from last_exc


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
    supports_concurrency: bool = False

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