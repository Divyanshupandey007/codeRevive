from __future__ import annotations

import secrets
import threading
from typing import Any, Dict, List, Literal, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field

from app.api.jobs import (
    JobConflictError,
    MigrationJobEvent,
    MigrationJobRecord,
    job_manager,
)
from app.config import settings
from app.integrations.github_actions_client import (
    GitHubActionsClient,
    GitHubActionsError,
    GitHubActionsNotFound,
)
from app.orchestrator.service import (
    MigrationJobRequest,
    get_active_config,
    load_latest_summary,
    run_migration_job,
)


def _extract_gha_run_id(job_id: str) -> Optional[int]:
    """Extract the GitHub Actions run id from a ``gha_<run_id>`` job id."""
    prefix = "gha_"
    if not job_id.startswith(prefix):
        return None
    try:
        return int(job_id[len(prefix):])
    except ValueError:
        return None


_github_client_instance: Optional[GitHubActionsClient] = None
_github_client_lock = threading.Lock()


def _get_github_client() -> GitHubActionsClient:
    """Lazily build a shared GitHubActionsClient from backend settings."""
    global _github_client_instance
    with _github_client_lock:
        if _github_client_instance is None:
            _github_client_instance = GitHubActionsClient(
                token=settings.github_token,
                owner=settings.github_owner,
                repo=settings.github_repo,
                workflow_id=settings.github_workflow_id,
                base_url=settings.github_api_base_url,
            )
        return _github_client_instance


class HealthResponse(BaseModel):
    status: str
    service: str


class RouteDescriptor(BaseModel):
    path: str
    methods: List[str]
    summary: str = ""


class RouteCatalogResponse(BaseModel):
    routes: List[RouteDescriptor]


class ServiceIndexResponse(BaseModel):
    status: str
    service: str
    api_prefix: str = "/api"
    docs_url: str = "/api/docs"
    openapi_url: str = "/api/openapi.json"
    routes: List[RouteDescriptor]


class MigrationSyncResponse(BaseModel):
    summary: Dict[str, Any]
    summary_path: str


class MigrationJobListResponse(BaseModel):
    items: List[MigrationJobRecord]
    total: int
    limit: int
    offset: int
    latest_available: Optional[bool] = None


class MigrationJobEventsResponse(BaseModel):
    job_id: str
    status: str
    stage: str
    events: List[MigrationJobEvent]


class DeleteJobResponse(BaseModel):
    deleted: bool
    job_id: str


class RemoteProgressEventRequest(BaseModel):
    stage: str
    message: str


class RemoteProgressEventResponse(BaseModel):
    accepted: bool


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Require an X-API-Key header matching settings.api_key when an API key is configured.

    When CODEREVIVE_API_KEY is not set (local development), this dependency is a no-op.
    Applied only to state-changing endpoints: POST /api/migrations, POST /api/migrations/jobs,
    POST /cancel, DELETE /api/migrations/jobs/{id}.
    """
    if not settings.api_key:
        return
    if x_api_key is None or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def require_progress_callback_token(
    x_progress_token: Optional[str] = Header(
        default=None, alias="X-CodeRevive-Progress-Token"
    )
) -> None:
    """Authenticate the internal GitHub Actions progress-relay endpoint.

    This guards a separate secret from require_api_key: it is sent only by
    run_migration.py running inside a GitHub Actions runner (see
    .github/workflows/coderevive-migrate.yml), never by the Angular frontend.
    Unlike require_api_key, an unset token means the relay is disabled, not
    open - so this always rejects rather than no-op'ing when unconfigured.
    """
    if not settings.progress_callback_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Progress callback token is not configured on this backend.",
        )
    if x_progress_token is None or not secrets.compare_digest(
        x_progress_token, settings.progress_callback_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-CodeRevive-Progress-Token header.",
        )


def health_check() -> Dict[str, str]:
    return {"status": "ok", "service": "coderevive-migration-engine"}


def get_config_endpoint() -> Dict[str, Any]:
    return get_active_config()


def get_latest_report_endpoint() -> Optional[Dict[str, Any]]:
    summary = load_latest_summary()
    return summary.model_dump() if summary is not None else None


def start_migration_endpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    request = MigrationJobRequest.model_validate(payload or {})
    result = run_migration_job(request)
    return {
        "summary": result.summary.model_dump(),
        "summary_path": result.summary_path,
    }


def start_migration_job_endpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    request = MigrationJobRequest.model_validate(payload or {})
    record = job_manager.submit(request)
    return record.model_dump()


def list_migration_jobs_endpoint(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    jobs, total = job_manager.list_jobs(status=status, limit=limit, offset=offset)
    return {
        "items": [job.model_dump() for job in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_migration_job_endpoint(job_id: str) -> Optional[Dict[str, Any]]:
    record: Optional[MigrationJobRecord] = job_manager.get(job_id)
    return record.model_dump() if record is not None else None


def get_migration_job_events_endpoint(job_id: str) -> Optional[Dict[str, Any]]:
    record: Optional[MigrationJobRecord] = job_manager.get(job_id)
    if record is None:
        return None
    return {
        "job_id": record.job_id,
        "status": record.status,
        "stage": record.stage,
        "events": [event.model_dump() for event in record.events],
    }


def delete_migration_job_endpoint(job_id: str) -> bool:
    return job_manager.delete(job_id)


def cancel_migration_job_endpoint(job_id: str) -> bool:
    """Best-effort cancellation of a queued or running migration job."""
    return job_manager.cancel(job_id)


def _describe_router_routes(router: APIRouter) -> List[Dict[str, Any]]:
    descriptors: List[Dict[str, Any]] = []
    for route in router.routes:
        path = getattr(route, "path", None)
        methods = sorted(
            m for m in (getattr(route, "methods", None) or set()) if m != "HEAD"
        )
        if not path or not methods:
            continue
        descriptors.append(
            {
                "path": path,
                "methods": methods,
                "summary": getattr(route, "summary", "") or "",
            }
        )
    return descriptors


def _wants_async_execution(
    async_param: Optional[bool],
    mode: Optional[str],
    prefer: Optional[str],
) -> bool:
    if async_param:
        return True
    if mode and mode.strip().lower() == "async":
        return True
    if prefer and "respond-async" in prefer.lower():
        return True
    return False


def build_api_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/",
        response_model=ServiceIndexResponse,
        tags=["System"],
        summary="Service metadata and route discovery",
    )
    def root_route() -> Dict[str, Any]:
        return {
            **health_check(),
            "api_prefix": "/api",
            "docs_url": "/api/docs",
            "openapi_url": "/api/openapi.json",
            "routes": _describe_router_routes(router),
        }

    @router.get(
        "/api",
        response_model=ServiceIndexResponse,
        tags=["System"],
        summary="API root index and route discovery",
    )
    def api_index_route() -> Dict[str, Any]:
        return {
            **health_check(),
            "api_prefix": "/api",
            "docs_url": "/api/docs",
            "openapi_url": "/api/openapi.json",
            "routes": _describe_router_routes(router),
        }

    @router.get(
        "/api/routes",
        response_model=RouteCatalogResponse,
        tags=["System"],
        summary="List registered backend API routes",
    )
    def routes_catalog() -> Dict[str, Any]:
        return {"routes": _describe_router_routes(router)}

    @router.get(
        "/health",
        response_model=HealthResponse,
        tags=["Health"],
        summary="Root health probe",
    )
    @router.get(
        "/api/health",
        response_model=HealthResponse,
        tags=["Health"],
        summary="Liveness health check",
    )
    @router.get(
        "/api/health/live",
        response_model=HealthResponse,
        tags=["Health"],
        summary="Container liveness probe",
    )
    @router.get(
        "/api/health/ready",
        response_model=HealthResponse,
        tags=["Health"],
        summary="Container readiness probe",
    )
    def health_route() -> Dict[str, str]:
        return health_check()

    @router.get(
        "/api/config",
        tags=["Config"],
        summary="Active non-secret backend configuration",
    )
    def config_route() -> Dict[str, Any]:
        return get_config_endpoint()

    @router.get(
        "/api/migrations",
        response_model=MigrationJobListResponse,
        tags=["Migrations"],
        summary="List migration jobs and latest report availability",
    )
    def list_migrations_route(
        job_status: Optional[Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]] = Query(
            default=None, alias="status"
        ),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> Dict[str, Any]:
        data = list_migration_jobs_endpoint(
            status=job_status, limit=limit, offset=offset
        )
        try:
            data["latest_available"] = (
                data.get("total", 0) > 0 and get_latest_report_endpoint() is not None
            )
        except (ValueError, OSError):
            data["latest_available"] = False
        return data

    @router.post(
        "/api/migrations",
        tags=["Migrations"],
        summary="Run a migration synchronously (200) or asynchronously (?async=true or Prefer: respond-async -> 202)",
    )
    def create_migration_route(
        response: Response,
        request_body: Optional[MigrationJobRequest] = Body(default=None),
        async_flag: Optional[bool] = Query(default=None, alias="async"),
        mode: Optional[str] = Query(default=None),
        prefer: Optional[str] = Header(default=None, alias="Prefer"),
        _auth: None = Depends(require_api_key),
    ) -> Dict[str, Any]:
        payload = request_body.model_dump() if request_body is not None else {}
        if _wants_async_execution(async_flag, mode, prefer):
            job = start_migration_job_endpoint(payload)
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Location"] = f"/api/migrations/jobs/{job['job_id']}"
            return job
        # Synchronous execution runs the migration (and Docker sandbox) on the
        # Hostless backend. In production github_actions mode that is disabled:
        # migrations run exclusively on GitHub Actions runners.
        if settings.execution_mode == "github_actions":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Synchronous migration is disabled in github_actions mode. "
                    "Use POST /api/migrations/jobs (?async=true) to dispatch the "
                    "migration to GitHub Actions instead."
                ),
            )
        return start_migration_endpoint(payload)

    @router.get(
        "/api/migrations/latest",
        tags=["Migrations"],
        summary="Get latest completed migration summary (404 if no migration ran in this backend session)",
    )
    def latest_migration_route() -> Dict[str, Any]:
        # In github_actions mode the backend on Hostless does not run Docker and
        # has no local workspace summary file. The report lives in the completed
        # workflow's artifact, so retrieve it from the latest completed GHA job.
        if settings.execution_mode == "github_actions":
            summary = job_manager.get_latest_completed_summary()
            if summary is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No migration report found yet.",
                )
            return summary.model_dump()

        # The summary file survives backend restarts, but it must only be served
        # when it is backed by an actual job run in this session — otherwise a
        # stale report from before the restart would leak through.
        _, total = job_manager.list_jobs(limit=1, offset=0)
        if total == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No migration report found yet.",
            )
        report = get_latest_report_endpoint()
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No migration report found yet.",
            )
        return report

    @router.get(
        "/api/migrations/jobs",
        response_model=MigrationJobListResponse,
        tags=["Migration Jobs"],
        summary="List asynchronous migration jobs",
    )
    def list_jobs_route(
        job_status: Optional[Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]] = Query(
            default=None, alias="status"
        ),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> Dict[str, Any]:
        return list_migration_jobs_endpoint(
            status=job_status, limit=limit, offset=offset
        )

    @router.post(
        "/api/migrations/jobs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=MigrationJobRecord,
        tags=["Migration Jobs"],
        summary="Queue and start an asynchronous migration job",
    )
    def create_job_route(
        response: Response,
        request_body: Optional[MigrationJobRequest] = Body(default=None),
        _auth: None = Depends(require_api_key),
    ) -> Dict[str, Any]:
        payload = request_body.model_dump() if request_body is not None else {}
        job = start_migration_job_endpoint(payload)
        response.headers["Location"] = f"/api/migrations/jobs/{job['job_id']}"
        return job

    @router.get(
        "/api/migrations/jobs/{job_id}",
        response_model=MigrationJobRecord,
        tags=["Migration Jobs"],
        summary="Get migration job status and result",
    )
    def get_job_route(job_id: str) -> Dict[str, Any]:
        job = get_migration_job_endpoint(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Migration job '{job_id}' not found.",
            )
        return job

    @router.get(
        "/api/migrations/jobs/{job_id}/events",
        response_model=MigrationJobEventsResponse,
        tags=["Migration Jobs"],
        summary="Get migration job progress events",
    )
    def get_job_events_route(job_id: str) -> Dict[str, Any]:
        events_payload = get_migration_job_events_endpoint(job_id)
        if events_payload is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Migration job '{job_id}' not found.",
            )
        return events_payload

    @router.delete(
        "/api/migrations/jobs/{job_id}",
        response_model=DeleteJobResponse,
        tags=["Migration Jobs"],
        summary="Delete a completed or failed migration job record",
    )
    def delete_job_route(job_id: str, _auth: None = Depends(require_api_key)) -> Dict[str, Any]:
        try:
            deleted = delete_migration_job_endpoint(job_id)
        except JobConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Migration job '{job_id}' not found.",
            )
        return {"deleted": True, "job_id": job_id}

    @router.post(
        "/api/migrations/jobs/{job_id}/cancel",
        response_model=DeleteJobResponse,
        tags=["Migration Jobs"],
        summary="Request cancellation of a queued or running migration job",
    )
    def cancel_job_route(job_id: str, _auth: None = Depends(require_api_key)) -> Dict[str, Any]:
        cancelled = cancel_migration_job_endpoint(job_id)
        if not cancelled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Migration job '{job_id}' not found or cannot be cancelled.",
            )
        return {"deleted": True, "job_id": job_id}

    @router.get(
        "/api/migrations/jobs/{job_id}/artifacts",
        tags=["Artifacts"],
        summary="List workflow-run artifacts for a GitHub Actions migration job",
    )
    def list_job_artifacts_route(job_id: str) -> Dict[str, Any]:
        run_id = _extract_gha_run_id(job_id)
        if run_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Job '{job_id}' is not a GitHub Actions migration job.",
            )
        client = _get_github_client()
        try:
            artifacts = client.list_artifacts(run_id)
        except GitHubActionsNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow run {run_id} not found.",
            )
        except GitHubActionsError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to list artifacts for run {run_id}: {exc}",
            )
        return {
            "job_id": job_id,
            "run_id": run_id,
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "name": a.name,
                    "size_in_bytes": a.size_in_bytes,
                    "expired": a.expired,
                    "created_at": a.created_at,
                }
                for a in artifacts
            ],
        }

    @router.get(
        "/api/migrations/jobs/{job_id}/artifacts/{artifact_id}/download",
        tags=["Artifacts"],
        summary="Download a workflow-run artifact for a GitHub Actions migration job",
    )
    def download_job_artifact_route(job_id: str, artifact_id: int) -> Response:
        run_id = _extract_gha_run_id(job_id)
        if run_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Job '{job_id}' is not a GitHub Actions migration job.",
            )
        client = _get_github_client()
        try:
            content = client.download_artifact(artifact_id)
        except GitHubActionsNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Artifact {artifact_id} not found for run {run_id}.",
            )
        except GitHubActionsError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to download artifact {artifact_id}: {exc}",
            )
        # Look up the artifact name to set a clean .zip download filename.
        filename = f"artifact-{artifact_id}.zip"
        try:
            for a in client.list_artifacts(run_id):
                if a.artifact_id == artifact_id and a.name:
                    safe_name = a.name.replace('"', "").replace("/", "_").replace("\\", "_").strip()
                    if safe_name:
                        filename = (
                            safe_name
                            if safe_name.lower().endswith(".zip")
                            else f"{safe_name}.zip"
                        )
                    break
        except GitHubActionsError:
            pass
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.post(
        "/api/internal/github-actions/runs/{run_id}/events",
        response_model=RemoteProgressEventResponse,
        tags=["Internal"],
        summary="Internal: relay a real-time stage event from a GitHub Actions run",
        include_in_schema=False,
    )
    def relay_gha_progress_event(
        run_id: int,
        payload: RemoteProgressEventRequest,
        _auth: None = Depends(require_progress_callback_token),
    ) -> Dict[str, Any]:
        accepted = job_manager.append_remote_event(
            run_id, payload.stage, payload.message
        )
        return {"accepted": accepted}

    @router.get(
        "/api/migrations/{job_id}",
        response_model=MigrationJobRecord,
        tags=["Migration Jobs"],
        summary="Alias for getting migration job status by ID",
    )
    def get_migration_by_id_route(job_id: str, request: Request) -> Dict[str, Any]:
        _ = request
        job = get_migration_job_endpoint(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Migration job '{job_id}' not found.",
            )
        return job

    return router


api_router = build_api_router()