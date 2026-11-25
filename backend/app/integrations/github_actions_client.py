"""Client for interacting with the GitHub Actions REST API.

This module is used by the CodeRevive backend when running in ``github_actions``
execution mode (production on Hostless). It is responsible for:

* dispatching a ``workflow_dispatch`` migration run
* polling workflow run status
* listing workflow runs
* cancelling a run
* retrieving job steps for a run
* listing and downloading workflow run artifacts

No GitHub tokens or LLM credentials ever leave the backend: the client holds the
GitHub token in memory and the Angular frontend only ever talks to the backend's
own authenticated endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time

import httpx


class GitHubActionsError(RuntimeError):
    """Base error for GitHub Actions client failures."""


class GitHubActionsNotFound(GitHubActionsError):
    """Raised when a requested GitHub Actions resource does not exist."""


@dataclass
class RunStatus:
    """Normalized status of a GitHub Actions workflow run."""

    run_id: int
    status: str  # queued | in_progress | completed | requested | waiting | pending
    conclusion: Optional[str] = None  # success | failure | cancelled | ... | None while running
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    html_url: Optional[str] = None
    head_branch: Optional[str] = None
    head_sha: Optional[str] = None
    event: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.status == "completed"

    @property
    def is_successful(self) -> bool:
        return self.status == "completed" and self.conclusion == "success"

    @property
    def is_failed(self) -> bool:
        return self.status == "completed" and self.conclusion in (
            "failure",
            "cancelled",
            "skipped",
            "stale",
            "timed_out",
        )


@dataclass
class RunJobStep:
    """A single step within a workflow run job."""

    name: str
    status: str
    conclusion: Optional[str]
    number: int
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunJob:
    """A job within a workflow run, with its steps."""

    job_id: int
    name: str
    status: str
    conclusion: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    steps: List[RunJobStep] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunArtifact:
    """A workflow run artifact available for download."""

    artifact_id: int
    name: str
    size_in_bytes: int
    download_url: Optional[str]
    expired: bool
    created_at: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowDispatchResult:
    """Result of dispatching a workflow_dispatch run."""

    run_id: int
    run_number: int
    status: str
    html_url: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)


class GitHubActionsClient:
    """Thin wrapper over the GitHub Actions REST API.

    Parameters
    ----------
    token:
        GitHub personal access token or GitHub App installation token with
        ``actions:write`` (and ``actions:read``) scope for the target repository.
    owner:
        GitHub user or organization that owns the repository.
    repo:
        Repository name that contains the migration workflow.
    workflow_id:
        The workflow filename (e.g. ``coderevive-migrate.yml``) or numeric ID.
    base_url:
        GitHub API base URL. Defaults to ``https://api.github.com``.
    timeout_seconds:
        HTTP timeout for every request.
    """

    API_VERSION = "2026-03-10"

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        workflow_id: str,
        *,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not token:
            raise ValueError("github_token is required")
        if not owner or not repo:
            raise ValueError("github_owner and github_repo are required")
        if not workflow_id:
            raise ValueError("github_workflow_id is required")
        self._token = token
        self._owner = owner
        self._repo = repo
        self._workflow_id = workflow_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": self.API_VERSION,
                "User-Agent": "coderevive-backend",
            },
        )

    # ------------------------------------------------------------------ #
    # Low-level request helper
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"/repos/{self._owner}/{self._repo}{path}"
        try:
            response = self._client.request(method, url, params=params, json=json)
        except httpx.HTTPError as exc:
            raise GitHubActionsError(f"GitHub API request failed: {exc}") from exc

        if response.status_code == 404:
            raise GitHubActionsNotFound(
                f"GitHub Actions resource not found: {method} {path}"
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise GitHubActionsError(
                f"GitHub API returned {response.status_code} for {method} {path}: {detail}"
            )
        if response.status_code in (204, 205):
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------------ #
    # Workflow dispatch
    # ------------------------------------------------------------------ #
    def dispatch_workflow(
        self,
        inputs: Dict[str, Any],
        *,
        ref: str = "main",
    ) -> WorkflowDispatchResult:
        """Trigger a ``workflow_dispatch`` run and return the resulting run id.

        With GitHub API version 2026-03-10 the dispatch response contains
        ``workflow_run_id`` directly, so we no longer need to guess which run
        was created by listing recent runs.
        """
        payload: Dict[str, Any] = {"ref": ref, "inputs": inputs}
        data = self._request(
            "POST",
            f"/actions/workflows/{self._workflow_id}/dispatches",
            json=payload,
        )
        run_id = data.get("workflow_run_id")
        if not run_id:
            raise GitHubActionsError(
                "Workflow was dispatched but GitHub did not return a run ID."
            )
        return WorkflowDispatchResult(
            run_id=int(run_id),
            run_number=int(data.get("run_number", 0)),
            status=str(data.get("status", "queued")),
            html_url=data.get("html_url"),
            raw=data,
        )

    # ------------------------------------------------------------------ #
    # Run status & listing
    # ------------------------------------------------------------------ #
    def get_run_status(self, run_id: int) -> RunStatus:
        """Return the normalized status of a workflow run."""
        data = self._request("GET", f"/actions/runs/{run_id}")
        return self._normalize_run(data)

    def list_runs(
        self,
        *,
        per_page: int = 10,
        branch: Optional[str] = None,
        status: Optional[str] = None,
        event: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List workflow runs, newest first."""
        params: Dict[str, Any] = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        if event:
            params["event"] = event
        data = self._request("GET", "/actions/runs", params=params)
        return data.get("workflow_runs", [])

    def _normalize_run(self, data: Dict[str, Any]) -> RunStatus:
        return RunStatus(
            run_id=int(data.get("id", 0)),
            status=str(data.get("status", "unknown")),
            conclusion=data.get("conclusion"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            html_url=data.get("html_url"),
            head_branch=data.get("head_branch"),
            head_sha=data.get("head_sha"),
            event=data.get("event"),
            raw=data,
        )

    # ------------------------------------------------------------------ #
    # Cancellation
    # ------------------------------------------------------------------ #
    def cancel_run(self, run_id: int) -> bool:
        """Request cancellation of a queued or in-progress workflow run."""
        try:
            self._request("POST", f"/actions/runs/{run_id}/cancel")
            return True
        except GitHubActionsNotFound:
            return False

    # ------------------------------------------------------------------ #
    # Job steps
    # ------------------------------------------------------------------ #
    def get_job_steps(self, run_id: int) -> List[RunJob]:
        """Return the jobs (with steps) for a workflow run."""
        data = self._request("GET", f"/actions/runs/{run_id}/jobs")
        jobs: List[RunJob] = []
        for item in data.get("jobs", []):
            steps = [
                RunJobStep(
                    name=str(s.get("name", "")),
                    status=str(s.get("status", "")),
                    conclusion=s.get("conclusion"),
                    number=int(s.get("number", 0)),
                    started_at=s.get("started_at"),
                    completed_at=s.get("completed_at"),
                    raw=s,
                )
                for s in item.get("steps", [])
            ]
            jobs.append(
                RunJob(
                    job_id=int(item.get("id", 0)),
                    name=str(item.get("name", "")),
                    status=str(item.get("status", "")),
                    conclusion=item.get("conclusion"),
                    started_at=item.get("started_at"),
                    completed_at=item.get("completed_at"),
                    steps=steps,
                    raw=item,
                )
            )
        return jobs

    # ------------------------------------------------------------------ #
    # Artifacts
    # ------------------------------------------------------------------ #
    def list_artifacts(self, run_id: int) -> List[RunArtifact]:
        """List artifacts for a workflow run."""
        data = self._request("GET", f"/actions/runs/{run_id}/artifacts")
        artifacts: List[RunArtifact] = []
        for item in data.get("artifacts", []):
            artifacts.append(
                RunArtifact(
                    artifact_id=int(item.get("id", 0)),
                    name=str(item.get("name", "")),
                    size_in_bytes=int(item.get("size_in_bytes", 0)),
                    download_url=item.get("archive_download_url"),
                    expired=bool(item.get("expired", False)),
                    created_at=item.get("created_at"),
                    raw=item,
                )
            )
        return artifacts

    def download_artifact(self, artifact_id: int, *, follow_redirects: bool = True) -> bytes:
        """Download a workflow run artifact as raw bytes (zip archive)."""
        # The artifacts/{id}/zip endpoint issues a redirect to the binary zip
        # archive, so use _download_raw directly rather than JSON parsing.
        return self._download_raw(artifact_id, follow_redirects=follow_redirects)

    def _download_raw(self, artifact_id: int, *, follow_redirects: bool) -> bytes:
        url = f"/repos/{self._owner}/{self._repo}/actions/artifacts/{artifact_id}/zip"
        try:
            response = self._client.request(
                "GET",
                url,
                follow_redirects=follow_redirects,
            )
        except httpx.HTTPError as exc:
            raise GitHubActionsError(f"Artifact download failed: {exc}") from exc
        if response.status_code == 404:
            raise GitHubActionsNotFound(f"Artifact {artifact_id} not found")
        if response.status_code >= 400:
            raise GitHubActionsError(
                f"Artifact download returned {response.status_code} for artifact {artifact_id}"
            )
        return response.content

    def find_artifact_by_name(
        self, run_id: int, name: str
    ) -> Optional[RunArtifact]:
        """Return the first artifact matching ``name`` (exact, case-sensitive)."""
        for artifact in self.list_artifacts(run_id):
            if artifact.name == name:
                return artifact
        return None

    def get_repo_variable(self, name: str) -> Optional[str]:
        """Best-effort fetch of a GitHub Actions repository variable value."""
        try:
            data = self._request("GET", f"/actions/variables/{name}")
        except GitHubActionsError:
            return None
        value = data.get("value")
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubActionsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
