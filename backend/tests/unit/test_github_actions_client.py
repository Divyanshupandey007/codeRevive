import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from app.integrations.github_actions_client import (
    GitHubActionsClient,
    GitHubActionsError,
    GitHubActionsNotFound,
    RunArtifact,
    RunJob,
    RunJobStep,
    RunStatus,
    WorkflowDispatchResult,
)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_body: Any = None,
        text: str = "",
        content: Optional[bytes] = None,
    ):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (
            json.dumps(json_body) if json_body is not None else ""
        )
        self.content = content if content is not None else self.text.encode("utf-8")
        self.headers: Dict[str, str] = {}

    def json(self) -> Any:
        return self._json_body


def _make_client(responses: List[Any]):
    """Build a GitHubActionsClient whose underlying httpx request is faked.

    ``responses`` are consumed in order across every ``request`` call.
    """
    client = GitHubActionsClient(
        token="ghp_test_token",
        owner="coderevive-org",
        repo="coderevive-backend",
        workflow_id="coderevive-migrate.yml",
    )
    calls: List[tuple] = []

    def fake_request(method, url, params=None, json=None, **kwargs):
        calls.append((method, url, params, json))
        idx = min(len(calls) - 1, len(responses) - 1)
        item = responses[idx]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch_target = client._client
    monkeypatch_target.request = fake_request  # type: ignore[method-assign]
    return client, calls


def _run_response(status="queued", conclusion=None, run_id=1001, number=7):
    return _FakeResponse(
        200,
        {
            "id": run_id,
            "run_number": number,
            "status": status,
            "conclusion": conclusion,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:05:00Z",
            "html_url": f"https://github.com/coderevive-org/coderevive-backend/actions/runs/{run_id}",
            "head_branch": "main",
            "head_sha": "abc1234",
            "event": "workflow_dispatch",
        },
    )


def test_dispatch_workflow_returns_run_id(monkeypatch):
    client, calls = _make_client(
        [
            _FakeResponse(
                200,
                {
                    "workflow_run_id": 4242,
                    "run_number": 3,
                    "status": "queued",
                    "html_url": "https://github.com/org/repo/actions/runs/4242",
                },
            ),
        ]
    )
    result = client.dispatch_workflow({"repo_url": "https://github.com/org/repo.git"})
    assert isinstance(result, WorkflowDispatchResult)
    assert result.run_id == 4242
    assert result.run_number == 3
    assert result.status == "queued"
    # Single POST to the dispatches endpoint; no run discovery round-trip.
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert "dispatches" in calls[0][1]
    assert calls[0][3]["inputs"]["repo_url"] == "https://github.com/org/repo.git"


def test_get_run_status_normalizes_fields():
    client, calls = _make_client([_run_response(status="in_progress", conclusion=None)])
    status = client.get_run_status(1001)
    assert isinstance(status, RunStatus)
    assert status.run_id == 1001
    assert status.status == "in_progress"
    assert status.conclusion is None
    assert status.is_done is False
    assert status.is_successful is False
    assert calls[0][0] == "GET"
    assert "actions/runs/1001" in calls[0][1]


def test_get_run_status_completed_success():
    client, _ = _make_client(
        [_run_response(status="completed", conclusion="success", run_id=55)]
    )
    status = client.get_run_status(55)
    assert status.is_done is True
    assert status.is_successful is True
    assert status.is_failed is False


def test_get_run_status_completed_failure():
    client, _ = _make_client(
        [_run_response(status="completed", conclusion="failure", run_id=56)]
    )
    status = client.get_run_status(56)
    assert status.is_done is True
    assert status.is_failed is True
    assert status.is_successful is False


def test_cancel_run():
    client, calls = _make_client([_FakeResponse(202)])
    assert client.cancel_run(1001) is True
    assert calls[0][0] == "POST"
    assert "cancel" in calls[0][1]


def test_cancel_run_not_found_returns_false():
    client, _ = _make_client([GitHubActionsNotFound("not found")])
    assert client.cancel_run(1001) is False


def test_list_artifacts():
    client, _ = _make_client(
        [
            _FakeResponse(
                200,
                {
                    "artifacts": [
                        {
                            "id": 77,
                            "name": "migration-run-summary",
                            "size_in_bytes": 1234,
                            "archive_download_url": "https://api.github.com/.../zip",
                            "expired": False,
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                        {
                            "id": 78,
                            "name": "migrated-source",
                            "size_in_bytes": 98765,
                            "archive_download_url": "https://api.github.com/.../zip",
                            "expired": False,
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                    ]
                },
            )
        ]
    )
    artifacts = client.list_artifacts(1001)
    assert len(artifacts) == 2
    assert artifacts[0].artifact_id == 77
    assert artifacts[0].name == "migration-run-summary"
    assert artifacts[0].expired is False
    found = client.find_artifact_by_name(1001, "migrated-source")
    assert found is not None
    assert found.artifact_id == 78
    assert client.find_artifact_by_name(1001, "does-not-exist") is None


def test_download_artifact_returns_bytes():
    payload = b'{"repo_url":"https://github.com/org/repo.git","final_state":"COMPLETED"}'
    client, calls = _make_client([_FakeResponse(200, content=payload)])
    content = client.download_artifact(77)
    assert content == payload
    assert calls[0][0] == "GET"
    assert "artifacts/77/zip" in calls[0][1]


def test_download_artifact_not_found():
    client, _ = _make_client([GitHubActionsNotFound("artifact missing")])
    with pytest.raises(GitHubActionsNotFound):
        client.download_artifact(999)


def test_get_job_steps():
    client, _ = _make_client(
        [
            _FakeResponse(
                200,
                {
                    "jobs": [
                        {
                            "id": 1,
                            "name": "Run CodeRevive migration",
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": "2026-01-01T00:01:00Z",
                            "completed_at": "2026-01-01T00:05:00Z",
                            "steps": [
                                {
                                    "number": 1,
                                    "name": "Checkout repository",
                                    "status": "completed",
                                    "conclusion": "success",
                                    "started_at": "2026-01-01T00:01:00Z",
                                    "completed_at": "2026-01-01T00:01:30Z",
                                },
                                {
                                    "number": 2,
                                    "name": "Run CodeRevive migration",
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "started_at": "2026-01-01T00:02:00Z",
                                    "completed_at": "2026-01-01T00:05:00Z",
                                },
                            ],
                        }
                    ]
                },
            )
        ]
    )
    jobs = client.get_job_steps(1001)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_id == 1
    assert job.name == "Run CodeRevive migration"
    assert job.conclusion == "success"
    assert len(job.steps) == 2
    assert job.steps[0].conclusion == "success"
    assert job.steps[1].conclusion == "failure"


def test_404_raises_not_found():
    client, _ = _make_client([GitHubActionsNotFound("run missing")])
    with pytest.raises(GitHubActionsNotFound):
        client.get_run_status(999)


def test_non_json_error_raises_git_hub_actions_error():
    client, _ = _make_client([_FakeResponse(500, text="Internal Server Error")])
    with pytest.raises(GitHubActionsError):
        client.get_run_status(999)


def test_run_status_properties_cover_github_states():
    # queued / in_progress / pending -> not done
    for status in ("queued", "in_progress", "pending", "requested"):
        s = RunStatus(run_id=1, status=status)
        assert s.is_done is False
        assert s.is_successful is False
    # completed + cancelled -> is_failed True per the property; _map_gha_status
    # maps it to CANCELLED separately.
    s = RunStatus(run_id=1, status="completed", conclusion="cancelled")
    assert s.is_done is True
    assert s.is_failed is True
    # completed + neutral -> not successful, not failed
    s = RunStatus(run_id=1, status="completed", conclusion="neutral")
    assert s.is_done is True
    assert s.is_successful is False
    assert s.is_failed is False
