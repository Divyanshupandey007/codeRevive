from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import routes
from app.integrations.github_actions_client import (
    GitHubActionsError,
    RunArtifact,
)
from app.main import app
from app.orchestrator.workflow import MigrationOrchestrator


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_git_diff_from_baseline_captures_cumulative_multi_pass_edits(tmp_path: Path):
    """Multi-pass edits (Pass 1 OpenRewrite + Pass 2 LLM retries) produce a full baseline-to-commit patch."""
    repo_dir = tmp_path / "demo"
    repo_dir.mkdir()
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.name", "Test User")
    _git(repo_dir, "config", "user.email", "test@example.com")

    target = repo_dir / "WebSecurityConfig.java"
    baseline_src = (
        "import javax.servlet.Filter;\n"
        "public class WebSecurityConfig extends WebSecurityConfigurerAdapter {\n"
        "    void configure() {}\n"
        "}\n"
    )
    target.write_text(baseline_src, encoding="utf-8")
    _git(repo_dir, "add", "WebSecurityConfig.java")
    _git(repo_dir, "commit", "-m", "baseline")
    baseline_sha = _git(repo_dir, "rev-parse", "HEAD")

    # Simulate Pass 1 (OpenRewrite javax -> jakarta) + Pass 2 (LLM SecurityFilterChain rewrite).
    pass1_src = baseline_src.replace("javax.servlet", "jakarta.servlet")
    target.write_text(pass1_src, encoding="utf-8")
    pass2_src = (
        "import jakarta.servlet.Filter;\n"
        "public class WebSecurityConfig {\n"
        "    void filterChain() {}\n"
        "}\n"
    )
    target.write_text(pass2_src, encoding="utf-8")
    _git(repo_dir, "add", "WebSecurityConfig.java")
    _git(repo_dir, "commit", "-m", "migrated")
    commit_sha = _git(repo_dir, "rev-parse", "HEAD")

    orch = MigrationOrchestrator.__new__(MigrationOrchestrator)
    orch.repo_path = repo_dir
    diff = orch._git_diff_from_baseline(
        baseline_sha, commit_sha, "WebSecurityConfig.java"
    )
    assert diff is not None
    assert "-import javax.servlet.Filter;" in diff
    assert "+import jakarta.servlet.Filter;" in diff
    assert "+    void filterChain() {}" in diff

    # Verify the generated diff applies cleanly onto the original baseline commit.
    _git(repo_dir, "checkout", "-B", "verify-apply", baseline_sha)
    patch_file = tmp_path / "cumulative.patch"
    patch_file.write_text(diff if diff.endswith("\n") else f"{diff}\n", encoding="utf-8")
    _git(repo_dir, "apply", "--check", str(patch_file))
    _git(repo_dir, "apply", str(patch_file))
    assert target.read_text(encoding="utf-8") == pass2_src


class _FakeArtifactClient:
    def __init__(self, *, fail_list: bool = False):
        self.fail_list = fail_list
        self.calls: list[tuple[str, object]] = []

    def list_artifacts(self, run_id: int) -> list[RunArtifact]:
        self.calls.append(("list_artifacts", run_id))
        if self.fail_list:
            raise GitHubActionsError("upstream unavailable")
        return [
            RunArtifact(
                artifact_id=42,
                name="migrated-source",
                size_in_bytes=1234,
                download_url=None,
                expired=False,
                created_at="2026-01-01T00:00:00Z",
                raw={},
            )
        ]

    def find_artifact_by_name(self, run_id: int, name: str):
        raise AssertionError("Redundant find_artifact_by_name should not be called")

    def download_artifact(self, artifact_id: int) -> bytes:
        self.calls.append(("download_artifact", artifact_id))
        return b"PK\x03\x04fakezip"


def test_download_artifact_appends_zip_extension_and_skips_redundant_lookup(monkeypatch):
    fake = _FakeArtifactClient()
    monkeypatch.setattr(routes, "_get_github_client", lambda: fake)
    client = TestClient(app)

    resp = client.get("/api/migrations/jobs/gha_12345/artifacts/42/download")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="migrated-source.zip"'
    assert resp.content == b"PK\x03\x04fakezip"


def test_list_artifacts_returns_502_on_github_error(monkeypatch):
    fake = _FakeArtifactClient(fail_list=True)
    monkeypatch.setattr(routes, "_get_github_client", lambda: fake)
    client = TestClient(app)

    resp = client.get("/api/migrations/jobs/gha_12345/artifacts")
    assert resp.status_code == 502
