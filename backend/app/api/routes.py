from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import (
    APIRouter,
    Body,
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
from app.orchestrator.service import (
    MigrationJobRequest,
    get_active_config,
    load_latest_summary,
    run_migration_job,
)


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
        job_status: Optional[Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]] = Query(
            default=None, alias="status"
        ),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> Dict[str, Any]:
        data = list_migration_jobs_endpoint(
            status=job_status, limit=limit, offset=offset
        )
        try:
            data["latest_available"] = get_latest_report_endpoint() is not None
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
    ) -> Dict[str, Any]:
        payload = request_body.model_dump() if request_body is not None else {}
        if _wants_async_execution(async_flag, mode, prefer):
            job = start_migration_job_endpoint(payload)
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Location"] = f"/api/migrations/jobs/{job['job_id']}"
            return job
        return start_migration_endpoint(payload)

    @router.get(
        "/api/migrations/latest",
        tags=["Migrations"],
        summary="Get latest completed migration summary",
    )
    def latest_migration_route() -> Dict[str, Any]:
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
        job_status: Optional[Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]] = Query(
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
    def delete_job_route(job_id: str) -> Dict[str, Any]:
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