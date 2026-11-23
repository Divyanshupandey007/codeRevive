from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Callable, Dict, List, Literal, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from app.config import settings
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


class MigrationJobManager:
    """Thread-safe in-memory tracker for background migration jobs."""

    def __init__(self, max_jobs: int = 100) -> None:
        self._max_jobs = max(1, max_jobs)
        self._lock = threading.RLock()
        self._jobs: Dict[str, MigrationJobRecord] = {}
        self._order: List[str] = []
        self._cancel_flags: Dict[str, threading.Event] = {}

    def submit(
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
        the next verification round. Terminal jobs cannot be cancelled.
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


job_manager = MigrationJobManager()
