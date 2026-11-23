from pathlib import Path
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, Field

from app.config import BACKEND_DIR, settings
from app.ingestion.pipeline import IngestionPipeline
from app.llm.base import LLMProvider
from app.models.manifest import ProjectIngestionReport
from app.models.orchestrator import MigrationRunSummary
from app.orchestrator.workflow import MigrationOrchestrator
from app.sandbox.docker_runner import DockerRunner


class MigrationJobRequest(BaseModel):
    """Single canonical request payload shared by CLI, Python callers, and future UI/API."""

    repo_url: str = Field(default_factory=lambda: settings.repo_url)
    target_spring_boot: str = Field(default_factory=lambda: settings.target_spring_boot_version)
    target_java: str = Field(default_factory=lambda: settings.target_java_version)
    branch_name: str = Field(default_factory=lambda: settings.migration_branch_name)
    use_openrewrite: bool = Field(default_factory=lambda: settings.use_openrewrite)
    openrewrite_mode: str = Field(default_factory=lambda: settings.openrewrite_mode)
    retry_budget: int = Field(default_factory=lambda: settings.default_retry_budget)
    clean: bool = False


class MigrationServiceResult(BaseModel):
    ingestion_report: ProjectIngestionReport
    summary: MigrationRunSummary
    summary_path: str


def _repository_presets() -> list[dict[str, str]]:
    # Only the canonical, actively-maintained preset is shipped.
    return [
        {
            "label": "mybatis-spring-boot-jpetstore",
            "url": "https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git",
        },
    ]


def get_active_config() -> Dict[str, Any]:
    """Return active non-secret migration defaults from backend/.env + config.py."""
    llm_model_by_provider = {
        "gemini": settings.gemini_model,
        "nvidia": settings.nvidia_model,
        "openrouter": settings.openrouter_model,
    }
    return {
        "repository_presets": _repository_presets(),
        "repo_url": settings.repo_url,
        "migration_branch_name": settings.migration_branch_name,
        "target_spring_boot_version": settings.target_spring_boot_version,
        "target_java_version": settings.target_java_version,
        "use_openrewrite": settings.use_openrewrite,
        "openrewrite_mode": settings.openrewrite_mode,
        "default_retry_budget": settings.default_retry_budget,
        "llm_provider": settings.llm_provider,
        "llm_model": llm_model_by_provider.get(settings.llm_provider, settings.llm_provider),
        "workspace_root": str(settings.resolved_workspace_root),
    }


def load_latest_summary(
    workspace_root: Optional[Path] = None,
) -> Optional[MigrationRunSummary]:
    effective_ws = (
        Path(workspace_root) if workspace_root else settings.resolved_workspace_root
    )
    summary_file = effective_ws / "migration_run_summary.json"
    if not summary_file.is_file():
        return None
    return MigrationRunSummary.model_validate_json(
        summary_file.read_text(encoding="utf-8")
    )


def resolve_workspace_repo_path(
    repo_url: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> Path:
    effective_url = repo_url or settings.repo_url
    local_candidate = Path(effective_url)
    repo_name = (
        local_candidate.resolve().name
        if local_candidate.is_dir()
        else effective_url.rstrip("/").split("/")[-1].removesuffix(".git")
    )
    effective_ws = (
        Path(workspace_root) if workspace_root else settings.resolved_workspace_root
    )
    return effective_ws / repo_name


def run_migration_job(
    request: Optional[MigrationJobRequest] = None,
    workspace_root: Optional[Path] = None,
    llm_provider: Optional[LLMProvider] = None,
    docker_runner: Optional[DockerRunner] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional["threading.Event"] = None,
) -> MigrationServiceResult:
    """Execute ingestion, Pass 1 OpenRewrite migration, Pass 2 sandbox verification & AI error resolution, and audit export."""
    import threading

    req = request or MigrationJobRequest()
    effective_ws = (
        Path(workspace_root) if workspace_root else settings.resolved_workspace_root
    )

    if progress_callback:
        progress_callback(f"ingesting:{req.repo_url}")

    ingestion_report = IngestionPipeline(
        branch_name=req.branch_name,
        workspace_root=effective_ws,
        target_spring_boot=req.target_spring_boot,
        target_java=req.target_java,
    ).run(req.repo_url, clean=req.clean)

    repo_path = Path(ingestion_report.repo_info.target_path)

    if progress_callback:
        progress_callback(f"orchestrating:{repo_path}")

    orchestrator = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=llm_provider,
        docker_runner=docker_runner,
        retry_budget=req.retry_budget,
        use_openrewrite=req.use_openrewrite,
        openrewrite_mode=req.openrewrite_mode,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    summary = orchestrator.run(ingestion_report)

    out_path = effective_ws / "migration_run_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    return MigrationServiceResult(
        ingestion_report=ingestion_report,
        summary=summary,
        summary_path=str(out_path),
    )
