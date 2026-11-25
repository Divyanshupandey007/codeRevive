from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
from time import sleep
from typing import Callable, Dict, List, Literal, Optional, Set, Tuple
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
    """Redact the absolute workspace path from error text surfaced to the UI."""
    workspace = str(settings.resolved_workspace_root)
    if workspace and workspace in message:
        message = message.replace(workspace, "[workspace]")
    return message


def _normalize_repo_key(repo_url: str) -> str:
    normalized = (repo_url or "").strip().rstrip("/")
    if normalized.lower().endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


JobStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]

_GHA_JOB_ID_PREFIX = "gha_"
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
    gha_run_url: Optional[str] = None


class JobConflictError(RuntimeError):
    """Raised when attempting a destructive operation on an active job."""


def _gha_inputs(request: MigrationJobRequest) -> Dict[str, str]:
    """Map a MigrationJobRequest to workflow_dispatch inputs for the runner workflow."""
    provider = getattr(request, "llm_provider", None) or settings.resolve_llm_provider()
    return {
        "repo_url": request.repo_url,
        "target_spring_boot": request.target_spring_boot,
        "target_java": request.target_java,
        "branch": request.branch_name,
        "openrewrite_mode": request.openrewrite_mode,
        "retry_budget": str(request.retry_budget),
        "llm_provider": provider,
        "llm_model": settings.resolve_llm_model(provider),
    }


def _map_gha_status(run_status: RunStatus) -> Tuple[JobStatus, str]:
    """Map a remote runner status to a CodeRevive (status, stage) pair."""
    if run_status.status == "queued":
        return "QUEUED", "queued"
    if run_status.status == "in_progress":
        return "RUNNING", "starting"
    if run_status.status == "completed":
        if run_status.conclusion == "success":
            return "COMPLETED", "completed"
        if run_status.conclusion == "cancelled":
            return "CANCELLED", "cancelled"
        return "FAILED", "failed"
    return "QUEUED", "queued"


_GHA_MIGRATION_STEP_NAME = "Run CodeRevive migration"
_GHA_MIGRATION_STEP_STAGE_FLOOR = "openrewrite"


def _map_remote_step_to_event(step_name: str) -> Optional[Tuple[str, str]]:
    """Translate internal runner step names into clean pipeline messages."""
    lower = (step_name or "").strip().lower()
    if not lower:
        return None
    if lower in (
        "checkout repository",
        "set up python",
        "install backend python dependencies",
    ):
        return ("starting", "Preparing migration environment...")
    if "build sandbox" in lower or "docker" in lower:
        return ("VERIFYING_SANDBOX", "Preparing verification sandbox environment...")
    if lower == _GHA_MIGRATION_STEP_NAME.lower():
        return ("openrewrite", "Running migration pipeline...")
    # Suppress internal CI housekeeping steps (Set up job, Complete job, Upload artifact, Post ...)
    return None


class MigrationJobManager:
    """Thread-safe tracker for background migration jobs.

    Supports ``local`` execution (background thread + local Docker) and
    ``github_actions`` execution (remote runner + live progress relay).
    """

    def __init__(
        self,
        max_jobs: int = 100,
        execution_mode: Optional[str] = None,
        github_client: Optional[GitHubActionsClient] = None,
        max_concurrent_jobs: Optional[int] = None,
        poll_interval_seconds: Optional[int] = None,
        state_file: Optional[Path] = None,
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
        self._gha_live_progress: Set[str] = set()
        self._active_pollers: Set[str] = set()
        self._removed_job_ids: Set[str] = set()
        self._state_file = state_file
        if self._state_file is not None:
            self._load_state_locked()

    # ------------------------------------------------------------------ #
    # Lightweight atomic state persistence (outside backend/ so uvicorn
    # --reload never triggers on state writes, and jobs survive restarts)
    # ------------------------------------------------------------------ #
    def _save_state_locked(self) -> None:
        if self._state_file is None:
            return
        try:
            payload = {
                "order": list(self._order),
                "jobs": {
                    jid: record.model_dump(mode="json")
                    for jid, record in self._jobs.items()
                },
            }
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_file.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            tmp_path.replace(self._state_file)
        except Exception:
            pass

    def _load_state_locked(self) -> None:
        if self._state_file is None or not self._state_file.is_file():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        jobs_raw = raw.get("jobs") or {}
        order_raw = raw.get("order") or list(jobs_raw.keys())
        for jid in order_raw:
            item = jobs_raw.get(jid)
            if not isinstance(item, dict):
                continue
            try:
                rec = MigrationJobRecord.model_validate(item)
            except Exception:
                continue
            if jid not in self._jobs:
                self._jobs[jid] = rec
                if jid not in self._order:
                    self._order.append(jid)
                self._cancel_flags.setdefault(jid, threading.Event())
                if rec.status == "CANCELLED":
                    self._cancel_flags[jid].set()

    def _cleanup_cancelled_jobs_for_repo_locked(self, repo_url: str) -> bool:
        """Remove prior CANCELLED jobs for the same repo before starting a fresh run."""
        target_key = _normalize_repo_key(repo_url)
        to_remove = [
            jid
            for jid, job in self._jobs.items()
            if job.status == "CANCELLED"
            and _normalize_repo_key(job.request.repo_url) == target_key
        ]
        if not to_remove:
            return False
        for jid in to_remove:
            self._jobs.pop(jid, None)
            self._cancel_flags.pop(jid, None)
            self._gha_live_progress.discard(jid)
            self._active_pollers.discard(jid)
            self._removed_job_ids.add(jid)
        self._order = [jid for jid in self._order if jid not in to_remove]
        self._save_state_locked()
        return True

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

        with self._lock:
            cleaned_cancelled = self._cleanup_cancelled_jobs_for_repo_locked(
                request.repo_url
            )
            effective_request = (
                request.model_copy(update={"clean": True})
                if cleaned_cancelled and not request.clean
                else request
            )
            record = MigrationJobRecord(
                job_id=job_id,
                status="QUEUED",
                stage="queued",
                request=effective_request,
                events=[
                    MigrationJobEvent(
                        stage="queued",
                        message=f"Migration job queued for {effective_request.repo_url}",
                    )
                ],
            )
            self._jobs[job_id] = record
            self._order.append(job_id)
            self._cancel_flags[job_id] = threading.Event()
            self._evict_oldest_finished_locked()
            self._save_state_locked()

        worker = threading.Thread(
            target=self._execute_job,
            args=(job_id, effective_request, effective_runner),
            name=f"coderevive-job-{job_id}",
            daemon=True,
        )
        worker.start()

        with self._lock:
            return self._jobs[job_id].model_copy(deep=True)

    def _submit_github_actions(self, request: MigrationJobRequest) -> MigrationJobRecord:
        with self._lock:
            self._cleanup_cancelled_jobs_for_repo_locked(request.repo_url)
            active_gha = sum(
                1
                for job in self._jobs.values()
                if job.job_id.startswith(_GHA_JOB_ID_PREFIX)
                and job.status in ("QUEUED", "RUNNING")
            )
            if active_gha >= self._max_concurrent:
                raise JobConflictError(
                    f"Maximum {self._max_concurrent} concurrent migration "
                    f"job(s) already running. Try again after one completes."
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
            gha_run_url=dispatch.html_url,
            events=[
                MigrationJobEvent(
                    stage="queued",
                    message=f"Migration job queued for {request.repo_url}",
                )
            ],
        )

        with self._lock:
            self._jobs[job_id] = record
            if job_id not in self._order:
                self._order.append(job_id)
            self._cancel_flags[job_id] = threading.Event()
            self._evict_oldest_finished_locked()
            self._save_state_locked()

        self._ensure_gha_poller(job_id, run_id)

        with self._lock:
            return self._jobs[job_id].model_copy(deep=True)

    def _ensure_gha_poller(self, job_id: str, run_id: int) -> None:
        with self._lock:
            if job_id in self._active_pollers:
                return
            self._active_pollers.add(job_id)
        worker = threading.Thread(
            target=self._poll_github_actions_run,
            args=(job_id, run_id),
            name=f"coderevive-gha-{run_id}",
            daemon=True,
        )
        worker.start()

    # ------------------------------------------------------------------ #
    # Remote runner polling
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

    def _current_step_name(self, client: GitHubActionsClient, run_id: int) -> Optional[str]:
        """Best-effort: name of the step currently in_progress for this run."""
        try:
            jobs = client.get_job_steps(run_id)
        except GitHubActionsError:
            return None
        for job in jobs:
            for step in job.steps:
                if step.status == "in_progress":
                    return step.name
        return None

    def _poll_github_actions_run(self, job_id: str, run_id: int) -> None:
        client = self._get_github_client()
        cancel_flag = self._cancel_flags.get(job_id)
        deadline = (
            None
            if settings.github_actions_job_timeout_seconds <= 0
            else __import__("time").monotonic()
            + settings.github_actions_job_timeout_seconds
        )
        last_step_message: Optional[str] = None

        try:
            while True:
                if cancel_flag is not None and cancel_flag.is_set():
                    try:
                        client.cancel_run(run_id)
                    except (GitHubActionsError, Exception):
                        pass
                    self._finalize_gha_job(
                        job_id, "CANCELLED", "cancelled", "Migration cancelled by user."
                    )
                    return

                if deadline is not None and __import__("time").monotonic() > deadline:
                    self._finalize_gha_job(
                        job_id,
                        "FAILED",
                        "failed",
                        "Migration timed out before completing.",
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
                        "Migration runner session could not be found.",
                        error_type="RunNotFound",
                    )
                    return
                except GitHubActionsError:
                    sleep(self._poll_interval)
                    continue

                status, stage = _map_gha_status(run_status)

                if run_status.is_done:
                    if run_status.conclusion == "success":
                        self._finalize_successful_gha_job(job_id, run_id, client)
                    elif run_status.conclusion == "cancelled":
                        self._finalize_gha_job(
                            job_id,
                            "CANCELLED",
                            "cancelled",
                            "Migration cancelled by user.",
                        )
                    else:
                        errors = self._gather_gha_failure_details(
                            client, run_id, run_status
                        )
                        self._finalize_gha_job(
                            job_id,
                            "FAILED",
                            "failed",
                            errors or "Migration failed before completion.",
                            error_type="WorkflowFailed",
                        )
                    return

                live_progress = job_id in self._gha_live_progress
                if run_status.status == "in_progress" and not live_progress:
                    step_name = self._current_step_name(client, run_id)
                    if step_name:
                        mapped = _map_remote_step_to_event(step_name)
                        if mapped is not None:
                            step_stage, step_msg = mapped
                            if step_msg != last_step_message:
                                last_step_message = step_msg
                                self._append_event(job_id, step_stage, step_msg)
                            stage = step_stage
                        if step_name == _GHA_MIGRATION_STEP_NAME:
                            stage = _GHA_MIGRATION_STEP_STAGE_FLOOR
                elif live_progress:
                    with self._lock:
                        record = self._jobs.get(job_id)
                        if record is not None:
                            stage = record.stage

                self._update_gha_job_status(job_id, status, stage)
                sleep(self._poll_interval)
        finally:
            with self._lock:
                self._active_pollers.discard(job_id)

    def _update_gha_job_status(
        self, job_id: str, status: JobStatus, stage: str
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.status in ("COMPLETED", "FAILED", "CANCELLED"):
                return
            previous_status = record.status
            record.status = status
            if job_id not in self._gha_live_progress:
                record.stage = stage
            if status == "RUNNING" and record.started_at is None:
                record.started_at = _utc_now_iso()
            if (
                previous_status != "RUNNING"
                and status == "RUNNING"
                and job_id not in self._gha_live_progress
            ):
                start_msg = f"Starting migration for {record.request.repo_url}"
                if not any(ev.message == start_msg for ev in record.events):
                    record.events.append(
                        MigrationJobEvent(
                            stage="starting",
                            message=start_msg,
                        )
                    )
            self._save_state_locked()

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
                MigrationJobEvent(stage=stage, message=_sanitize_error(message))
            )
            self._gha_live_progress.discard(job_id)
            self._save_state_locked()

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
        except (GitHubActionsError, ValueError, OSError, zipfile.BadZipFile):
            summary = None
            summary_path = f"gha_run:{run_id}"

        self._finalize_gha_job(
            job_id,
            "COMPLETED",
            "completed",
            "Migration completed successfully.",
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
                    details.append(f"Step failed: {', '.join(failing_steps)}")
        if details:
            return "; ".join(details)
        conclusion = run_status.conclusion or "failure"
        return f"Migration ended with status: {conclusion}."

    # ------------------------------------------------------------------ #
    # Local execution
    # ------------------------------------------------------------------ #
    def _evict_oldest_finished_locked(self) -> None:
        while len(self._order) > self._max_jobs:
            evicted = False
            for idx, jid in enumerate(self._order):
                job = self._jobs.get(jid)
                if job is None or job.status in ("COMPLETED", "FAILED", "CANCELLED"):
                    self._order.pop(idx)
                    self._jobs.pop(jid, None)
                    self._cancel_flags.pop(jid, None)
                    self._gha_live_progress.discard(jid)
                    evicted = True
                    break
            if not evicted:
                break

    def _append_event(self, job_id: str, stage: str, message: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.status in ("COMPLETED", "FAILED", "CANCELLED"):
                return
            record.stage = stage
            record.events.append(MigrationJobEvent(stage=stage, message=message))
            self._save_state_locked()

    def append_remote_event(self, run_id: int, stage: str, message: str) -> bool:
        """Append an event relayed directly from a remote migration run."""
        job_id = f"{_GHA_JOB_ID_PREFIX}{run_id}"
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status not in ("QUEUED", "RUNNING"):
                return False
            if record.status == "QUEUED":
                record.status = "RUNNING"
                if record.started_at is None:
                    record.started_at = _utc_now_iso()
            record.stage = stage
            record.events.append(MigrationJobEvent(stage=stage, message=message))
            self._gha_live_progress.add(job_id)
            self._save_state_locked()
            return True

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
            if record.status == "CANCELLED":
                record.completed_at = _utc_now_iso()
                record.events.append(
                    MigrationJobEvent(
                        stage="cancelled",
                        message="Migration cancelled by user.",
                    )
                )
                self._save_state_locked()
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
            self._save_state_locked()

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
                if record.status == "CANCELLED":
                    record.completed_at = _utc_now_iso()
                    record.events.append(
                        MigrationJobEvent(
                            stage="cancelled",
                            message="Migration cancelled by user.",
                        )
                    )
                    self._save_state_locked()
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
                self._save_state_locked()
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
                if record.status == "CANCELLED":
                    self._save_state_locked()
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
                self._save_state_locked()

    def _hydrate_gha_job_from_remote(self, job_id: str) -> Optional[MigrationJobRecord]:
        """Recover a remote job record if a backend restart/worker switch lost RAM state."""
        if self._execution_mode != "github_actions" or not job_id.startswith(
            _GHA_JOB_ID_PREFIX
        ):
            return None
        with self._lock:
            if job_id in self._removed_job_ids:
                return None
        try:
            run_id = int(job_id[len(_GHA_JOB_ID_PREFIX) :])
        except ValueError:
            return None
        try:
            client = self._get_github_client()
            run_status = client.get_run_status(run_id)
        except Exception:
            return None

        status, stage = _map_gha_status(run_status)
        created_at = run_status.created_at or _utc_now_iso()
        req = MigrationJobRequest()
        events = [
            MigrationJobEvent(
                timestamp=created_at,
                stage="queued",
                message=f"Migration job queued for {req.repo_url}",
            )
        ]
        if status == "RUNNING":
            events.append(
                MigrationJobEvent(
                    stage="starting",
                    message=f"Starting migration for {req.repo_url}",
                )
            )
        elif status == "COMPLETED":
            events.append(
                MigrationJobEvent(
                    stage="completed",
                    message="Migration completed successfully.",
                )
            )
        elif status == "CANCELLED":
            events.append(
                MigrationJobEvent(
                    stage="cancelled",
                    message="Migration cancelled by user.",
                )
            )
        elif status == "FAILED":
            events.append(
                MigrationJobEvent(
                    stage="failed",
                    message="Migration failed before completion.",
                )
            )

        record = MigrationJobRecord(
            job_id=job_id,
            status=status,
            stage=stage,
            request=req,
            created_at=created_at,
            started_at=created_at if status != "QUEUED" else None,
            completed_at=run_status.updated_at if run_status.is_done else None,
            events=events,
            gha_run_url=run_status.html_url,
        )
        with self._lock:
            self._jobs[job_id] = record
            if job_id not in self._order:
                self._order.append(job_id)
            self._cancel_flags.setdefault(job_id, threading.Event())
            if status == "CANCELLED":
                self._cancel_flags[job_id].set()
            self._save_state_locked()

        if status in ("QUEUED", "RUNNING"):
            self._ensure_gha_poller(job_id, run_id)
        elif status == "COMPLETED":
            summary = self._download_gha_summary_on_demand(record)
            if summary is not None:
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id].summary = summary
                        self._save_state_locked()
                        return self._jobs[job_id].model_copy(deep=True)
        return record.model_copy(deep=True)

    def get(self, job_id: str) -> Optional[MigrationJobRecord]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None and self._state_file is not None:
                self._load_state_locked()
                record = self._jobs.get(job_id)
            if record is not None:
                return record.model_copy(deep=True)
        return self._hydrate_gha_job_from_remote(job_id)

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[MigrationJobRecord], int]:
        with self._lock:
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
        """Request cancellation of a queued or running job."""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None and self._state_file is not None:
                self._load_state_locked()
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
                run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX) :])
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
            self._save_state_locked()
            return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return False
            if record.status in ("QUEUED", "RUNNING"):
                if record.job_id.startswith(_GHA_JOB_ID_PREFIX):
                    run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX) :])
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
            self._gha_live_progress.discard(job_id)
            self._removed_job_ids.add(job_id)
            self._save_state_locked()
            return True

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._order.clear()
            self._cancel_flags.clear()
            self._gha_live_progress.clear()
            self._save_state_locked()

    # ------------------------------------------------------------------ #
    # Latest completed summary (used by /api/migrations/latest)
    # ------------------------------------------------------------------ #
    def get_latest_completed_summary(self) -> Optional[MigrationRunSummary]:
        """Return the summary of the latest completed job, if any."""
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
            run_id = int(record.job_id[len(_GHA_JOB_ID_PREFIX) :])
        except ValueError:
            return None
        client = self._get_github_client()
        try:
            artifact = client.find_artifact_by_name(run_id, SUMMARY_ARTIFACT_NAME)
            if artifact is None:
                return None
            content = client.download_artifact(artifact.artifact_id)
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
                    return None
                report_json = archive.read(report_name)
            return MigrationRunSummary.model_validate_json(report_json)
        except (GitHubActionsError, ValueError, OSError, zipfile.BadZipFile):
            return None


job_manager = MigrationJobManager(
    state_file=Path(tempfile.gettempdir()) / "coderevive_jobs_state.json"
)
