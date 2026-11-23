from __future__ import annotations

from datetime import datetime, timezone
import io
import threading
from time import sleep
from typing import Callable, Dict, List, Literal, Optional, Tuple
from uuid import uuid4
import zipfile

from pydantic import BaseModel, Field

from app.config import settings
from app.integrations.github_actions_client import (
    GitHubActionsClient,
    GitHubActionsError,
    GitHubActionsNotFound,
    RunArtifact,
    RunStatus,
)
from app.ingestion.pipeline import UnsupportedRepositoryError
from app.llm.base import LLMProviderError
from app.models.orchestrator import MigrationRunSummary, WorkflowState
from app.orchestrator.service import (
    MigrationJobRequest,
    MigrationServiceResult,
    run_migration_job,
)
from app.sandbox.docker_runner import ContainerExecutionError


def _sanitize_error(message: str) -> str:
    """Redact the absolute workspace path from error text surfaced to the UI.

    Underlying OS/git errors (e.g. ``[WinError 5] Access is denied: 'D:\...'``)
    include the full local path. It is the user's own machine path, not a
    secret, but there is no reason to expose the absolute file system layout.
    """
    workspace = str(settings.resolved_workspace_root)
    if workspace and workspace in message:
        message = message.replace(workspace, "[workspace]")
    return message


JobStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]

# Prefix for remote (GitHub Actions) job ids so they never collide with local
# ``mig_`` ids and are easy to identify in logs and the UI.
_GHA_JOB_ID_PREFIX = "gha_"

# Name of the workflow artifact that holds the migration audit summary.
SUMMARY_ARTIFACT_NAME = "migration-run-summary"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationJobEvent(BaseModel):
    timestamp: str = Field(default_factory=_utc_now_iso)
    stage: str
    message: str


class MigrationJobRecord(BaseModel):
    job_id: str
    status: JobStatus = "QUEUED"
    stage: str = "queued"
    request: MigrationJobRequest
    created_at: str = Field(default_factory=_utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    events: List[MigrationJobEvent] = Field(default_factory=list)
    summary: Optional[MigrationRunSummary] = None
    summary_path: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[str] = None


class JobConflictError(RuntimeError):
    """Raised when attempting a destructive operation on an active job."""


def _gha_inputs(request: MigrationJobRequest) -> Dict[str, str]:
    """Map a MigrationJobRequest to workflow_dispatch inputs for the GHA workflow.

    Only inputs declared in .github/workflows/coderevive-migrate.yml are sent;
    ``clean`` is applied unconditionally by the workflow itself.
    """
    return {
        "repo_url": request.repo_url,
        "target_spring_boot": request.target_spring_boot,
        "target_java": request.target_java,
        "branch": request.branch_name,
        "openrewrite_mode": request.openrewrite_mode,
        "retry_budget": str(request.retry_budget),
        "llm_provider": getattr(request, "llm_provider", settings.llm_provider),
    }


def _map_gha_status(run_status: RunStatus) -> Tuple[str, str]:
    """Map a GitHub Actions run status to a CodeRevive (status, stage) pair."""
    if run_status.status == "queued":
        return "QUEUED", "queued"
    if run_status.status == "in_progress":
        return "RUNNING", "running"
    if run_status.status == "completed":
        if run_status.conclusion == "success":
            return "COMPLETED", "completed"
        if run_status.conclusion == "cancelled":
            return "CANCELLED", "cancelled"
        return "FAILED", "failed"
    # requested / waiting / pending -> treat as queued
    return "QUEUED", "queued"


class MigrationJobManager:
    """Thread-safe in-memory tracker for background migration jobs.

    Supports two execution modes:

    * ``local`` (default): preserves the existing behaviour — migrations run in a
      background thread via :func:`run_migration_job`, which uses Docker locally.
    * ``github_actions`` (production on Hostless): migrations are dispatched to
      GitHub Actions ubuntu-24.04 runners via ``workflow_dispatch``. The
      ``workflow_run_id`` becomes the remote job id (``gha_<run_id>``) and a
      background thread polls the run status, mapping GitHub states to the
      existing CodeRevive job statuses.
    """

    def __init__(
        self,
        max_jobs: int = 100,
        execution_mode: Optional[str] = None,
        github_client: Optional[GitHubActionsClient] = None,
        max_concurrent_jobs: Optional[int] = None,
        poll_interval_seconds: Optional[int] = None,
    ) -> None:
        self._max_jobs = max(1, max_jobs)
        self._lock = threading.RLock()
        self._jobs: Dict[str, MigrationJobRecord] = {}
        self._order: List[str] = []
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._execution_mode = execution_mode or settings.execution_mode
        self._github_client = github_client
        self._github_client_lock = threading.RLock()
        self._max_concurrent = (
            max_concurrent_jobs
            if max_concurrent_jobs is not None
            else settings.max_concurrent_github_actions_jobs
        )
        self._poll_interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.github_actions_poll_interval_seconds
        )

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #
    def submit(
        self,
        request: MigrationJobRequest,
        runner: Optional[Callable[..., MigrationServiceResult]] = None,
    ) -> MigrationJobRecord:
        if self._execution_mode == "github_actions":
            return self._submit_github_actions(request)
        return self._submit_local(request, runner)

    def _submit_local(
        self,
        request: MigrationJobRequest,
        runner: Optional[Callable[..., MigrationServiceResult]] = None,
    ) -> MigrationJobRecord:
        effective_runner = runner or run_migration_job
        job_id = f"mig_{uuid4().hex[:12]}"
        record = MigrationJobRecord(
            job_id=job_id,
            status="QUEUED",
            stage="queued",
            request=request,
            events=[
                MigrationJobEvent(
                    stage="queued",
                    message=f"Migration job queued for {request.repo_url}",
                )
            ],
        )

        with self._lock:
            self._jobs[job_id] = record
            self._order.append(job_id)
            self._cancel_flags[job_id] = threading.Event()
            self._evict_oldest_finished_locked()

        worker = threading.Thread(
            target=self._execute_job,
            args=(job_id, request, effective_runner),
            name=f"coderevive-job-{job_id}",
            daemon=True,
        )
        worker.start()

        with self._lock:
            return self._jobs[job_id].model_copy(deep=True)

    def _submit_github_actions(self, request: MigrationJobRequest) -> MigrationJobRecord:
        # Prevent concurrent submissions from exhausting the available runner
        # resources. Only count active GitHub Actions jobs toward the limit.
        with self._lock:
            active_gha = sum(
                1
                for job in self._jobs.values()
                if job.job_id.startswith(_GHA_JOB_ID_PREFIX)
                and job.status in ("QUEUED", "RUNNING")
            )
            if active_gha >= self._max_concurrent:
                raise JobConflictError(
                    f"Maximum {self._max_concurrent} concurrent GitHub Actions "
                    f"migration job(s) already running. Try again after one completes."
                )

        client = self._get_github_client()
        dispatch = client.dispatch_workflow(_gha_inputs(request))
        run_id = dispatch.run_id
        job_id = f"{_GHA_JOB_ID_PREFIX}{run_id}"

        record = MigrationJobRecord(
            job_id=job_id,
            status="QUEUED",
            stage="queued",
            request=request,
            events=[
                MigrationJobEvent(
                    stage="queued",
                    message=(
                        f"Migration dispatched to GitHub Actions run {run_id} "
                        f"for {request.repo_url}"
                    ),
                )
            ],
        )

        with self._lock:
            self._jobs[job_id] = record
            self._order.append(job_id)
            self._cancel_flags[job_id] = threading.Event()
            self._evict_oldest_finished_locked()

        worker = threading.Thread(
            target=self._poll_github_actions_run,
            args=(job_id, run_id),
            name=f"coderevive-gha-{run_id}",
            daemon=True,
        )
        worker.start()

        with self._lock:
            return self._jobs[job_id].model_copy(deep=True)

    # ------------------------------------------------------------------ #
    # GitHub Actions polling
    # ------------------------------------------------------------------ #
    def _get_github_client(self) -> GitHubActionsClient:
        with self._github_client_lock:
            if self._github_client is None:
                self._github_client = GitHubActionsClient(
                    token=settings.github_token,
                    owner=settings.github_owner,
                    repo=settings.github_repo,
                    workflow_id=settings.github_workflow_id,
                    base_url=settings.github_api_base_url,
                    timeout_seconds=float(settings.github_actions_job_timeout_seconds),
                )
            return self._github_client

    def _poll_github_actions_run(self, job_id: str, run_id: int) -> None:
        client = self._get_github_client()
        cancel_flag = self._cancel_flags.get(job_id)
        deadline = (
            None
            if settings.github_actions_job_timeout_seconds <= 0
            else __import__("time").monotonic()
            + settings.github_actions_job_timeout_seconds
        )

        while True:
            # Honour cancellation before each poll.
            if cancel_flag is not None and cancel_flag.is_set():
                try:
                    client.cancel_run(run_id)
                except (GitHubActionsError, Exception):
                    pass
                self._finalize_gha_job(job_id, "CANCELLED", "cancelled", "Migration cancelled by user.")
                return

            if deadline is not None and __import__("time").monotonic() > deadline:
                self._finalize_gha_job(
                    job_id,
                    "FAILED",
                    "failed",
                    f"GitHub Actions run {run_id} did not complete within the job timeout.",
                    error_type="JobTimeout",
                )
                return

            try:
                run_status = client.get_run_status(run_id)
            except GitHubActionsNotFound:
                self._finalize_gha_job(
                    job_id,
                    "FAILED",
                    "failed",
                    f"GitHub Actions run {run_id} was not found.",
                    error_type="RunNotFound",
                )
                return
            except GitHubActionsError as exc:
                # Transient API error: retry after the poll interval rather than
                # failing the whole job on a single blip.
                sleep(self._poll_interval)
                continue

            status, stage = _map_gha_status(run_status)

            if run_status.is_done:
                # Terminal states are finalized entirely by _finalize_* (which
                # writes the status plus any summary/error payload). We must not
                # pre-mark the record terminal here, or _finalize_gha_job would
                # short-circuit as a no-op and drop the summary/error.
                if run_status.conclusion == "success":
                    self._finalize_successful_gha_job(job_id, run_id, client)
                elif run_status.conclusion == "cancelled":
                    self._finalize_gha_job(job_id, "CANCELLED", "cancelled", "Migration cancelled by user.")
                else:
                    errors = self._gather_gha_failure_details(client, run_id, run_status)
                    self._finalize_gha_job(
                        job_id,
                        "FAILED",
                        "failed",
                        errors or f"GitHub Actions run {run_id} concluded with {run_status.conclusion}.",
                        error_type="WorkflowFailed",
                    )
                return

            self._update_gha_job_status(job_id, status, stage)
            sleep(self._poll_interval)

    def _update_gha_job_status(
        self, job_id: str, status: JobStatus, stage: str
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.status == "CANCELLED":
                return
            record.status = status
            record.stage = stage
            if status == "RUNNING" and record.started_at is None:
                record.started_at = _utc_now_iso()
            record.events.append(
                MigrationJobEvent(
                    stage=stage,
                    message=f"GitHub Actions run status: {status}",
                )
            )

    def _finalize_gha_job(
        self,
        job_id: str,
        status: JobStatus,
        stage: str,
        message: str,
        *,
        error_type: Optional[str] = None,
        summary: Optional[MigrationRunSummary] = None,
        summary_path: Optional[str] = None,
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.status in ("COMPLETED", "FAILED", "CANCELLED"):
                return
            record.status = status
            record.stage = stage
            record.completed_at = _utc_now_iso()
            if summary is not None:
                record.summary = summary
            if summary_path is not None:
                record.summary_path = summary_path
            if status == "FAILED":
                record.error = _sanitize_error(message)
                record.error_type = error_type or "WorkflowFailed"
            record.events.append(
                MigrationJobEvent(stage=stage, message=message)
            )

    def _finalize_successful_gha_job(
        self, job_id: str, run_id: int, client: GitHubActionsClient
    ) -> None:
        """Download the summary artifact and mark the job completed."""
        summary = None
        summary_path = None
        try:
            artifact = client.find_artifact_by_name(run_id, SUMMARY_ARTIFACT_NAME)
            if artifact is not None:
                content = client.download_artifact(artifact.artifact_id)
                # GitHub's artifact download endpoint returns a ZIP archive, not
                # the raw JSON file. Extract migration_run_summary.json from it.
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    report_name = next(
                        (
                            name
                            for name in archive.namelist()
                            if name.endswith("migration_run_summary.json")
                        ),
                        None,
                    )
                    if report_name is None:
                        raise ValueError(
                            "Migration summary JSON not found in artifact."
                        )
                    report_json = archive.read(report_name)
                summary = MigrationRunSummary.model_validate_json(report_json)
                summary_path = f"gha_artifact:{run_id}:{artifact.artifact_id}"
        except (GitHubActionsError, ValueError, OSError, zipfile.BadZipFile) as exc:
            # Summary download is best-effort: the job still completed successfully
            # from GitHub's perspective; surface what we have.
            summary = None
            summary_path = f"gha_run:{run_id}"

        self._finalize_gha_job(
            job_id,
            "COMPLETED",
            "completed",
            f"GitHub Actions run {run_id} completed successfully.",
            summary=summary,
            summary_path=summary_path,
        )

    def _gather_gha_failure_details(
        self, client: GitHubActionsClient, run_id: int, run_status: RunStatus
    ) -> str:
        """Best-effort collection of failure details from a completed run."""
        try:
            jobs = client.get_job_steps(run_id)
        except GitHubActionsError:
            return ""
        details: List[str] = []
        for job in jobs:
            if job.conclusion in ("failure", "cancelled", "timed_out"):
                failing_steps = [
                    s.name for s in job.steps if s.conclusion in ("failure", "timed_out")
                ]
                if failing_steps:
                    details.append(f"job '{job.name}': {', '.join(failing_steps)}")
        if details:
            return "; ".join(details)
        return f"GitHub Actions run {run_id} concluded with {run_status.conclusion}."

    # ------------------------------------------------------------------ #
    # Local execution (unchanged behaviour)
    # ------------------------------------------------------------------ #
    def _evict_oldest_finished_locked(self) -> None:
        while len(self._order) > self._max_jobs:
            evicted = False
            for idx, jid in enumerate(self._order):
                job = self._jobs.get(jid)
                if job is None or job.status in ("COMPLETED", "FAILED"):
                    self._order.pop(idx)
                    self._jobs.pop(jid, None)
                    self._cancel_flags.pop(jid, None)
                    evicted = True
                    break
            if not evicted:
                break

    def _append_event(self, job_id: str, stage: str, message: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.stage = stage
            record.events.append(MigrationJobEvent(stage=stage, message=message))

    def _execute_job(
        self,
        job_id: str,
        request: MigrationJobRequest,
        runner: Callable[..., MigrationServiceResult],
    ) -> None:
        cancel_flag = self._cancel_flags.get(job_id)
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            # A cancel request can land before the worker thread runs; honor it
            # instead of clobbering it with RUNNING below.
            if record.status == "CANCELLED":
                record.completed_at = _utc_now_iso()
                record.events.append(
                    MigrationJobEvent(
                        stage="cancelled",
                        message="Migration cancelled by user.",
                    )
                )
                return
            record.status = "RUNNING"
            record.stage = "starting"
            record.started_at = _utc_now_iso()
            record.events.append(
                MigrationJobEvent(
                    stage="starting",
                    message=f"Starting migration for {request.repo_url}",
                )
            )

        def on_progress(event_text: str) -> None:
            stage = event_text.split(":", 1)[0] if ":" in event_text else event_text
            self._append_event(job_id, stage, event_text)

        try:
            result = runner(
                request, progress_callback=on_progress, cancel_event=cancel_flag
            )
            with self._lock:
                record = self._jobs.get(job_id)
                if record is None:
                    return
                # The worker may have been cancelled mid-run; keep the cancelled
                # terminal state instead of overwriting it with FAILED/COMPLETED.
                if record.status == "CANCELLED":
                    record.completed_at = _utc_now_iso()
                    record.events.append(
                        MigrationJobEvent(
                            stage="cancelled",
                            message="Migration cancelled by user.",
                        )
                    )
                    return
                record.summary = result.summary
                record.summary_path = result.summary_path
                record.completed_at = _utc_now_iso()
                if result.summary.final_state == WorkflowState.COMPLETED:
                    record.status = "COMPLETED"
                    record.stage = "completed"
                    record.events.append(
                        MigrationJobEvent(
                            stage="completed",
                            message="Migration completed successfully.",
                        )
                    )
                else:
                    record.status = "FAILED"
                    record.stage = "failed"
                    record.error = _sanitize_error(
                        "; ".join(result.summary.last_build_errors[:3])
                        or "; ".join(result.summary.integrity_violations[:3])
                        or f"Workflow ended in state {result.summary.final_state.value}"
                    )
                    record.error_type = "WorkflowFailed"
                    record.events.append(
                        MigrationJobEvent(
                            stage="failed",
                            message=record.error,
                        )
                    )
        except (
            UnsupportedRepositoryError,
            ValueError,
            LLMProviderError,
            ContainerExecutionError,
            Exception,
        ) as exc:
            with self._lock:
                record = self._jobs.get(job_id)
                if record is None:
                    return
                record.status = "FAILED"
                record.stage = "failed"
                record.completed_at = _utc_now_iso()
                record.error = _sanitize_error(str(exc))
                record.error_type = type(exc).__name__
                record.events.append(
                    MigrationJobEvent(
                        stage="failed",
                        message=_sanitize_error(f"{type(exc).__name__}: {exc}"),
                    )
                )

    def get(self, job_id: str) -> Optional[MigrationJobRecord]:
        with self._lock:
            record = self._jobs.get(job_id)
            return record.model_copy(deep=True) if record is not None else None

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[MigrationJobRecord], int]:
        with self._lock:
            # Newest first
            ordered = [
                self._jobs[jid]
                for jid in reversed(self._order)
                if jid in self._jobs
            ]
            if status:
                normalized = status.strip().upper()
                ordered = [job for job in ordered if job.status == normalized]
            total = len(ordered)
            sliced = ordered[offset : offset + limit]
            return [job.model_copy(deep=True) for job in sliced], total

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a queued or running job.

        Cooperative: the migration worker checks the cancel flag and stops at
        the next verification round. Terminal jobs cannot be cancelled. For
        GitHub Actions jobs the remote workflow run is cancelled too.
        """
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return False
            if record.status in ("COMPLETED", "FAILED", "CANCELLED"):
                return False
            flag = self._cancel_flags.get(job_id)
            if flag is None:
                flag = threading.Event()
                self._cancel_flags[job_id] = flag
            flag.set()
            if record.job_id.startswith(_GHA_JOB_ID_PREFIX):
                # Cancel the remote workflow run asynchronously; the polling
                # thread will observe the cancellation and finalize the record.
                run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX):])
                client = self._get_github_client()
                try:
                    client.cancel_run(run_id)
                except GitHubActionsError:
                    pass
            record.status = "CANCELLED"
            record.stage = "cancelled"
            record.completed_at = _utc_now_iso()
            record.events.append(
                MigrationJobEvent(
                    stage="cancelled",
                    message="Migration cancelled by user.",
                )
            )
            return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return False
            if record.status in ("QUEUED", "RUNNING"):
                # Cancel the remote run first for GHA jobs before deleting.
                if record.job_id.startswith(_GHA_JOB_ID_PREFIX):
                    run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX):])
                    client = self._get_github_client()
                    try:
                        client.cancel_run(run_id)
                    except GitHubActionsError:
                        pass
                raise JobConflictError(
                    f"Cannot delete job '{job_id}' while status is '{record.status}'."
                )
            self._jobs.pop(job_id, None)
            self._order = [jid for jid in self._order if jid != job_id]
            self._cancel_flags.pop(job_id, None)
            return True

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._order.clear()

    # ------------------------------------------------------------------ #
    # Latest completed summary (used by /api/migrations/latest)
    # ------------------------------------------------------------------ #
    def get_latest_completed_summary(self) -> Optional[MigrationRunSummary]:
        """Return the summary of the latest completed job, if any.

        For GitHub Actions jobs this downloads the summary artifact on demand
        when the record does not yet carry it.
        """
        with self._lock:
            ordered = [
                self._jobs[jid]
                for jid in reversed(self._order)
                if jid in self._jobs
            ]
        for record in ordered:
            if record.status == "COMPLETED":
                if record.summary is not None:
                    return record.summary
                if record.job_id.startswith(_GHA_JOB_ID_PREFIX):
                    return self._download_gha_summary_on_demand(record)
                return None
        return None

    def _download_gha_summary_on_demand(
        self, record: MigrationJobRecord
    ) -> Optional[MigrationRunSummary]:
        try:
            run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX):])
        except ValueError:
            return None
        client = self._get_github_client()
        try:
            artifact = client.find_artifact_by_name(run_id, SUMMARY_ARTIFACT_NAME)
            if artifact is None:
                return None
            content = client.download_artifact(artifact.artifact_id)
            return MigrationRunSummary.model_validate_json(content.decode("utf-8"))
        except (GitHubActionsError, ValueError, OSError):
            return None


job_manager = MigrationJobManager()
