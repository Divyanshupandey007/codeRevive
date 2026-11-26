import json
import threading
import time
from typing import Callable, List, Optional

import pytest

from app.api.jobs import (
    JobConflictError,
    MigrationJobManager,
    MigrationJobRequest,
    _GHA_JOB_ID_PREFIX,
    job_manager,
)
from app.config import settings
from app.integrations.github_actions_client import (
    GitHubActionsClient,
    GitHubActionsNotFound,
    RunArtifact,
    RunJob,
    RunJobStep,
    RunStatus,
    WorkflowDispatchResult,
)
from app.models.orchestrator import WorkflowState


class _FakeGHAClient:
    """Controllable stand-in for GitHubActionsClient used by job-manager tests."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.dispatch_result = WorkflowDispatchResult(
            run_id=9001, run_number=1, status="queued", html_url=None, raw={}
        )
        self.status_sequence: List = []
        self.repeat_status: Optional[RunStatus] = None
        self.cancelled_run_id: Optional[int] = None
        self.artifacts: List[RunArtifact] = []
        self.download_content: Optional[bytes] = None
        self.job_steps_sequence: List[List[RunJob]] = []
        self.dispatch_hook: Optional[Callable[[], None]] = None

    def dispatch_workflow(self, inputs, ref="main") -> WorkflowDispatchResult:
        self.calls.append(("dispatch", dict(inputs)))
        if self.dispatch_hook is not None:
            self.dispatch_hook()
        return self.dispatch_result

    def get_run_status(self, run_id: int) -> RunStatus:
        self.calls.append(("get_run_status", run_id))
        if self.repeat_status is not None:
            return self.repeat_status
        if self.status_sequence:
            nxt = self.status_sequence.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt
        return RunStatus(run_id=run_id, status="completed", conclusion="success")

    def cancel_run(self, run_id: int) -> bool:
        self.calls.append(("cancel", run_id))
        self.cancelled_run_id = run_id
        return True

    def get_job_steps(self, run_id: int) -> List:
        if self.job_steps_sequence:
            return self.job_steps_sequence.pop(0)
        return []

    def list_artifacts(self, run_id: int) -> List[RunArtifact]:
        return self.artifacts

    def find_artifact_by_name(self, run_id: int, name: str):
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        return None

    def download_artifact(self, artifact_id: int, follow_redirects=True) -> bytes:
        return self.download_content or b"{}"


def _make_request() -> MigrationJobRequest:
    return MigrationJobRequest(
        repo_url="https://github.com/org/demo.git",
        target_spring_boot="3.2.5",
        target_java="17",
        branch_name="migration/spring-boot-3",
        use_openrewrite=True,
        openrewrite_mode="hybrid",
        retry_budget=5,
    )


def _wait_for(
    manager: MigrationJobManager,
    job_id: str,
    condition: Callable,
    timeout: float = 5.0,
):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = manager.get(job_id)
        if last is not None and condition(last):
            return last
        time.sleep(0.02)
    return last


def _summary_json_bytes() -> bytes:
    payload = {
        "repo_url": "https://github.com/org/demo.git",
        "baseline_commit": "abc1234",
        "final_commit": "def5678",
        "target_spring_boot_version": "3.2.5",
        "target_java_version": "17",
        "openrewrite_used": True,
        "openrewrite_files_modified": 2,
        "total_files_migrated": 2,
        "total_retries": 0,
        "file_statuses": [],
        "integrity_passed": True,
        "total_duration_seconds": 10.0,
        "final_state": "COMPLETED",
        "integrity_violations": [],
        "last_build_errors": [],
        "generated_at": "2026-01-01T00:00:00Z",
    }
    return json.dumps(payload).encode("utf-8")


def test_gha_submit_dispatches_and_maps_queued_to_running():
    fake = _FakeGHAClient()
    # Sequence must start with "queued" so the record is still QUEUED right after
    # submit; poll_interval > 0 gives the main thread a window to assert QUEUED
    # before the polling thread advances to RUNNING.
    fake.status_sequence = [
        RunStatus(run_id=1, status="queued"),
        RunStatus(run_id=1, status="in_progress"),
        RunStatus(run_id=1, status="completed", conclusion="success"),
    ]
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0.05,
    )
    record = manager.submit(_make_request())
    assert record.job_id.startswith(_GHA_JOB_ID_PREFIX)
    assert record.job_id == "gha_9001"
    assert record.status == "QUEUED"
    assert fake.calls and fake.calls[0][0] == "dispatch"

    final = _wait_for(manager, record.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    assert final.status == "COMPLETED"
    assert final.stage == "completed"


def _summary_zip_bytes() -> bytes:
    """Wrap the summary JSON in a ZIP archive, as GitHub's artifact API does."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("migration_run_summary.json", _summary_json_bytes())
    return buf.getvalue()


def test_gha_successful_run_downloads_summary_artifact():
    fake = _FakeGHAClient()
    fake.status_sequence = [
        RunStatus(run_id=2, status="completed", conclusion="success")
    ]
    summary_zip = _summary_zip_bytes()
    fake.artifacts = [
        RunArtifact(
            artifact_id=77,
            name="migration-run-summary",
            size_in_bytes=len(summary_zip),
            download_url=None,
            expired=False,
            created_at="2026-01-01T00:00:00Z",
            raw={},
        )
    ]
    fake.download_content = summary_zip
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    record = manager.submit(_make_request())
    final = _wait_for(manager, record.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    assert final.summary is not None
    assert final.summary.final_state == WorkflowState.COMPLETED
    assert final.summary.repo_url == "https://github.com/org/demo.git"
    assert final.summary_path is not None
    assert "gha_artifact:" in final.summary_path


def test_gha_failed_run_records_error():
    fake = _FakeGHAClient()
    fake.status_sequence = [
        RunStatus(run_id=3, status="completed", conclusion="failure")
    ]
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    record = manager.submit(_make_request())
    final = _wait_for(manager, record.job_id, lambda r: r.status == "FAILED")
    assert final is not None
    assert final.status == "FAILED"
    assert final.error is not None


def test_gha_cancelled_run_records_cancelled():
    fake = _FakeGHAClient()
    fake.repeat_status = RunStatus(run_id=4, status="in_progress")
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    record = manager.submit(_make_request())
    _wait_for(manager, record.job_id, lambda r: r.status == "RUNNING")

    expected_run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX):])
    assert manager.cancel(record.job_id) is True
    final = _wait_for(manager, record.job_id, lambda r: r.status == "CANCELLED")
    assert final is not None
    assert final.status == "CANCELLED"
    assert fake.cancelled_run_id == expected_run_id


def test_gha_concurrency_limit_rejects_second_submission():
    fake = _FakeGHAClient()
    fake.repeat_status = RunStatus(run_id=5, status="in_progress")
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    manager.submit(_make_request())
    _wait_for(manager, "gha_9001", lambda r: r.status == "RUNNING")

    with pytest.raises(JobConflictError):
        manager.submit(_make_request())

    # Clean up the lingering in-progress job.
    manager.cancel("gha_9001")


def test_gha_latest_completed_summary_returns_summary():
    fake = _FakeGHAClient()
    fake.status_sequence = [
        RunStatus(run_id=6, status="completed", conclusion="success")
    ]
    fake.artifacts = [
        RunArtifact(
            artifact_id=8,
            name="migration-run-summary",
            size_in_bytes=len(_summary_json_bytes()),
            download_url=None,
            expired=False,
            created_at="2026-01-01T00:00:00Z",
            raw={},
        )
    ]
    fake.download_content = _summary_zip_bytes()
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    manager.submit(_make_request())
    _wait_for(manager, "gha_9001", lambda r: r.status == "COMPLETED")

    summary = manager.get_latest_completed_summary()
    assert summary is not None
    assert summary.final_state == WorkflowState.COMPLETED


def test_local_mode_still_uses_threaded_runner(monkeypatch):
    calls: List[str] = []

    def fake_run(request, progress_callback=None, **_kwargs):
        calls.append(request.repo_url)
        from app.models.orchestrator import MigrationRunSummary
        from app.orchestrator.service import MigrationServiceResult

        report = None
        summary = MigrationRunSummary(
            repo_url=request.repo_url,
            baseline_commit="abc",
            final_commit="def",
            final_state=WorkflowState.COMPLETED,
            integrity_passed=True,
        )
        return MigrationServiceResult(
            ingestion_report=report,
            summary=summary,
            summary_path="/tmp/summary.json",
        )

    monkeypatch.setattr("app.api.jobs.run_migration_job", fake_run)
    manager = MigrationJobManager(execution_mode="local", max_jobs=10)
    record = manager.submit(_make_request())
    assert record.job_id.startswith("mig_")
    _wait_for(manager, record.job_id, lambda r: r.status == "COMPLETED")
    assert calls == ["https://github.com/org/demo.git"]


def test_gha_inputs_include_all_required_fields():
    from app.api.jobs import _gha_inputs

    req = _make_request()
    inputs = _gha_inputs(req)
    assert inputs["repo_url"] == "https://github.com/org/demo.git"
    assert inputs["target_spring_boot"] == "3.2.5"
    assert inputs["target_java"] == "17"
    assert inputs["branch"] == "migration/spring-boot-3"
    assert inputs["openrewrite_mode"] == "hybrid"
    assert inputs["retry_budget"] == "5"
    assert "llm_provider" in inputs
    assert "llm_model" in inputs
    # "clean" is applied unconditionally by the workflow itself, not sent as input.
    assert "clean" not in inputs


def test_gha_logs_do_not_mention_github_actions_or_dispatched():
    fake = _FakeGHAClient()
    fake.status_sequence = [
        RunStatus(run_id=9001, status="queued"),
        RunStatus(run_id=9001, status="in_progress"),
        RunStatus(run_id=9001, status="completed", conclusion="success"),
    ]
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0.02,
    )
    record = manager.submit(_make_request())
    final = _wait_for(manager, record.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    for ev in final.events:
        lower = ev.message.lower()
        assert "github actions" not in lower
        assert "dispatched" not in lower


def test_cancel_and_resubmit_same_repo_cleans_old_cancelled_job():
    fake = _FakeGHAClient()
    fake.repeat_status = RunStatus(run_id=9001, status="in_progress")
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0.02,
    )
    first = manager.submit(_make_request())
    _wait_for(manager, first.job_id, lambda r: r.status == "RUNNING")
    assert manager.cancel(first.job_id) is True

    fake.dispatch_result = WorkflowDispatchResult(
        run_id=9002, run_number=2, status="queued", html_url=None, raw={}
    )
    fake.repeat_status = None
    fake.status_sequence = [
        RunStatus(run_id=9002, status="completed", conclusion="success")
    ]
    second = manager.submit(_make_request())
    assert second.job_id == "gha_9002"
    assert manager.get(first.job_id) is None
    final = _wait_for(manager, second.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    assert final.status == "COMPLETED"


def test_execution_mode_defaults_to_local():
    assert settings.execution_mode == "local"
    # The module-level manager is configured for local execution by default.
    assert job_manager._execution_mode == "local"

def test_gha_transient_404_after_dispatch_retries_and_recovers():
    fake = _FakeGHAClient()
    fake.status_sequence = [
        GitHubActionsNotFound("404 while run materializes"),
        GitHubActionsNotFound("still materializing"),
        RunStatus(run_id=9001, status="queued"),
        RunStatus(run_id=9001, status="in_progress"),
        RunStatus(run_id=9001, status="completed", conclusion="success"),
    ]
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    record = manager.submit(_make_request())
    final = _wait_for(manager, record.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    assert final.status == "COMPLETED"
    assert final.error is None


def test_gha_persistent_404_eventually_fails_with_run_not_found():
    fake = _FakeGHAClient()
    fake.status_sequence = [GitHubActionsNotFound("gone")] * 10
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    record = manager.submit(_make_request())
    final = _wait_for(manager, record.job_id, lambda r: r.status == "FAILED")
    assert final is not None
    assert final.status == "FAILED"
    assert final.error_type == "RunNotFound"


def test_gha_step_polling_keeps_setup_steps_in_starting_stage_and_orders_start_first():
    fake = _FakeGHAClient()
    fake.status_sequence = [
        RunStatus(run_id=9001, status="in_progress"),
        RunStatus(run_id=9001, status="in_progress"),
        RunStatus(run_id=9001, status="in_progress"),
        RunStatus(run_id=9001, status="completed", conclusion="success"),
    ]
    fake.job_steps_sequence = [
        [
            RunJob(
                job_id=1,
                name="Run CodeRevive migration",
                status="in_progress",
                conclusion=None,
                started_at=None,
                completed_at=None,
                steps=[
                    RunJobStep(
                        name="Checkout repository",
                        status="in_progress",
                        conclusion=None,
                        number=1,
                    )
                ],
            )
        ],
        [
            RunJob(
                job_id=1,
                name="Run CodeRevive migration",
                status="in_progress",
                conclusion=None,
                started_at=None,
                completed_at=None,
                steps=[
                    RunJobStep(
                        name="Build sandbox Docker image",
                        status="in_progress",
                        conclusion=None,
                        number=2,
                    )
                ],
            )
        ],
        [
            RunJob(
                job_id=1,
                name="Run CodeRevive migration",
                status="in_progress",
                conclusion=None,
                started_at=None,
                completed_at=None,
                steps=[
                    RunJobStep(
                        name="Run CodeRevive migration",
                        status="in_progress",
                        conclusion=None,
                        number=3,
                    )
                ],
            )
        ],
    ]
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    record = manager.submit(_make_request())
    final = _wait_for(manager, record.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    messages = [ev.message for ev in final.events]
    start_idx = next(
        i for i, m in enumerate(messages) if m.startswith("Starting migration for ")
    )
    prep_idx = messages.index("Preparing migration environment...")
    assert start_idx < prep_idx
    step_events = [
        ev
        for ev in final.events
        if ev.message
        in (
            "Preparing migration environment...",
            "Preparing verification sandbox environment...",
            "Running migration pipeline...",
        )
    ]
    assert len(step_events) == 3
    assert all(ev.stage == "starting" for ev in step_events)


def test_gha_concurrent_submit_does_not_double_dispatch():
    fake = _FakeGHAClient()
    fake.repeat_status = RunStatus(run_id=9001, status="in_progress")
    entered = threading.Event()
    release = threading.Event()

    def slow_dispatch() -> None:
        entered.set()
        release.wait(timeout=2.0)

    fake.dispatch_hook = slow_dispatch
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    first_result = []

    def submit_first() -> None:
        first_result.append(manager.submit(_make_request()))

    t = threading.Thread(target=submit_first)
    t.start()
    assert entered.wait(timeout=2.0)

    with pytest.raises(JobConflictError):
        manager.submit(_make_request())

    release.set()
    t.join(timeout=2.0)
    assert len(first_result) == 1
    dispatch_calls = [c for c in fake.calls if c[0] == "dispatch"]
    assert len(dispatch_calls) == 1
    manager.cancel(first_result[0].job_id)


def test_cancel_and_resubmit_different_repo_recovers_from_transient_404():
    fake = _FakeGHAClient()
    fake.repeat_status = RunStatus(run_id=9001, status="in_progress")
    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=fake,
        max_concurrent_jobs=1,
        poll_interval_seconds=0.02,
    )
    first = manager.submit(_make_request())
    _wait_for(manager, first.job_id, lambda r: r.status == "RUNNING")
    assert manager.cancel(first.job_id) is True

    fake.dispatch_result = WorkflowDispatchResult(
        run_id=9002, run_number=2, status="queued", html_url=None, raw={}
    )
    fake.repeat_status = None
    fake.status_sequence = [
        GitHubActionsNotFound("new run not visible yet"),
        RunStatus(run_id=9002, status="in_progress"),
        RunStatus(run_id=9002, status="completed", conclusion="success"),
    ]
    other_repo = MigrationJobRequest(
        repo_url="https://github.com/org/other-app.git",
        target_spring_boot="3.2.5",
        target_java="17",
        branch_name="migration/spring-boot-3",
        use_openrewrite=True,
        openrewrite_mode="hybrid",
        retry_budget=5,
    )
    second = manager.submit(other_repo)
    assert second.job_id == "gha_9002"
    final = _wait_for(manager, second.job_id, lambda r: r.status == "COMPLETED")
    assert final is not None
    assert final.status == "COMPLETED"
