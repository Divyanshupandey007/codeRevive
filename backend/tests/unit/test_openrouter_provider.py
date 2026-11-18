import json
from typing import Any, Dict, List, Optional

import pytest
import requests

from app.config import Settings, settings
from app.llm.base import LLMProviderError, LLMStructuredOutputError
from app.llm.factory import LLMFactory
from app.llm.openrouter_provider import OpenRouterProvider
from app.models.agent import FilePatchResult


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Any = None, text: str = "", headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body


class _FakeSession:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, headers: Dict[str, str], data: str, timeout: float):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": json.loads(data),
                "timeout": timeout,
            }
        )
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        item = self.responses[idx]
        if isinstance(item, Exception):
            raise item
        return item


def test_openrouter_factory_and_env_configuration(monkeypatch):
    monkeypatch.setenv("CODEREVIVE_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("CODEREVIVE_OPENROUTER_API_KEY", "sk-or-v1-test-key")
    monkeypatch.setenv("CODEREVIVE_OPENROUTER_MODEL", "openai/gpt-oss-120b")
    custom = Settings(_env_file=None)
    assert custom.llm_provider == "openrouter"
    assert custom.openrouter_api_key == "sk-or-v1-test-key"
    assert custom.openrouter_model == "openai/gpt-oss-120b"

    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-v1-test-key")
    provider = LLMFactory.create()
    assert isinstance(provider, OpenRouterProvider)
    assert provider.model == "openai/gpt-oss-120b"


def test_openrouter_accepts_standard_env_var_and_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-env-fallback")
    provider = OpenRouterProvider()
    assert provider._api_key == "sk-or-v1-env-fallback"


def test_openrouter_generate_structured_sends_reasoning_and_parses_fenced_json():
    patch_json = json.dumps(
        {
            "file_path": "src/main/java/A.java",
            "new_content": "import jakarta.servlet.Filter;\nclass A {}\n",
            "explanation": "Migrated javax.servlet to jakarta.servlet",
        }
    )
    reasoning_details = [{"type": "reasoning.text", "text": "Checking Jakarta imports..."}]
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": f"```json\n{patch_json}\n```",
                                "reasoning_details": reasoning_details,
                            }
                        }
                    ]
                },
            )
        ]
    )

    provider = OpenRouterProvider(
        api_key="sk-or-v1-abc",
        model="openai/gpt-oss-120b",
        site_url="https://coderevive.local",
        app_name="CodeRevive",
        session=session,
    )
    result = provider.generate_structured(
        prompt="Migrate A.java",
        response_schema=FilePatchResult,
        system_instruction="Be precise.",
        temperature=0.1,
    )

    assert isinstance(result, FilePatchResult)
    assert result.file_path == "src/main/java/A.java"
    assert "jakarta.servlet.Filter" in result.new_content
    assert provider.last_reasoning_details == reasoning_details

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-or-v1-abc"
    assert call["headers"]["HTTP-Referer"] == "https://coderevive.local"
    assert call["headers"]["X-Title"] == "CodeRevive"
    assert call["payload"]["model"] == "openai/gpt-oss-120b"
    assert call["payload"]["reasoning"] == {"enabled": True}
    assert call["payload"]["response_format"] == {"type": "json_object"}
    assert call["payload"]["messages"][0] == {"role": "system", "content": "Be precise."}


def test_openrouter_preserves_reasoning_details_across_multi_turn_chat():
    details_1 = [{"type": "reasoning.text", "text": "Counted r letters."}]
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "There are 3 r's.",
                                "reasoning_details": details_1,
                            }
                        }
                    ]
                },
            ),
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Yes, confirmed 3.",
                                "reasoning_details": details_1,
                            }
                        }
                    ]
                },
            ),
        ]
    )
    provider = OpenRouterProvider(api_key="sk-or-v1-abc", session=session)
    msg1 = provider.chat_messages([{"role": "user", "content": "How many r's?"}])
    assert msg1["reasoning_details"] == details_1

    msg2 = provider.chat_messages(
        [
            {"role": "user", "content": "How many r's?"},
            {
                "role": "assistant",
                "content": msg1.get("content"),
                "reasoning_details": msg1.get("reasoning_details"),
            },
            {"role": "user", "content": "Are you sure?"},
        ]
    )
    assert msg2["content"] == "Yes, confirmed 3."
    assert session.calls[1]["payload"]["messages"][1]["reasoning_details"] == details_1


def test_openrouter_retries_rate_limit_and_falls_back_on_response_format_400(monkeypatch):
    monkeypatch.setattr("app.llm.openrouter_provider.time.sleep", lambda _s: None)
    session = _FakeSession(
        [
            _FakeResponse(429, text="Rate limited"),
            _FakeResponse(400, text="Provider does not support response_format json_object"),
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": 'Here is the JSON:\n{"file_path":"A.java","new_content":"class A {}","explanation":"ok"}',
                            }
                        }
                    ]
                },
            ),
        ]
    )

    provider = OpenRouterProvider(api_key="sk-or-v1-abc", session=session, max_retries=4)
    result = provider.generate_structured("Fix A.java", FilePatchResult)
    assert result.file_path == "A.java"
    assert len(session.calls) == 3
    assert "response_format" in session.calls[1]["payload"]
    assert "response_format" not in session.calls[2]["payload"]


def test_openrouter_raises_typed_errors_on_bad_json_or_transport_failure(monkeypatch):
    monkeypatch.setattr("app.llm.openrouter_provider.time.sleep", lambda _s: None)

    bad_json_session = _FakeSession(
        [
            _FakeResponse(
                200,
                {"choices": [{"message": {"role": "assistant", "content": "not json at all"}}]},
            )
        ]
    )
    provider = OpenRouterProvider(api_key="sk-or-v1-abc", session=bad_json_session)
    with pytest.raises(LLMStructuredOutputError):
        provider.generate_structured("Fix A.java", FilePatchResult)

    failing_session = _FakeSession([requests.Timeout("timed out"), requests.Timeout("timed out")])
    provider2 = OpenRouterProvider(api_key="sk-or-v1-abc", session=failing_session, max_retries=2)
    with pytest.raises(LLMProviderError, match="after 2 attempt"):
        provider2.generate_text("Hi")
