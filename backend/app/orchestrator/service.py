from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

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

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, v: str) -> str:
        """Guard against SSRF and git argument injection via repo_url.

        - Only https:// is allowed (blocks file://, git://, ssh://, http://).
        - The URL must not start with '-' (git clone treats leading '-' as a flag).
        - localhost, loopback, link-local, private/RFC1918 IPs and .local hosts
          are rejected to prevent scanning the deploy host's own network.
        """
        if not v or not v.strip():
            raise ValueError("repo_url is required")
        v = v.strip()
        if v.startswith("-"):
            raise ValueError("repo_url must not start with '-'")
        parsed = urlparse(v)
        if parsed.scheme != "https":
            raise ValueError("repo_url must use https://")
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
            raise ValueError("repo_url must not point to localhost or .local")
        try:
            ip = ip_address(host)
        except ValueError:
            ip = None  # a hostname, not an IP — that's fine
        if ip is not None and (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        ):
            raise ValueError("repo_url must not point to a private/internal address")
        return v


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


_REMOTE_LLM_CACHE: Dict[str, Any] = {"checked_at": 0.0, "provider": None, "model": None}
_REMOTE_LLM_CACHE_TTL_SECONDS = 60.0


def _resolve_remote_gha_llm_model(provider: str) -> Optional[str]:
    """In github_actions mode, if the backend host didn't explicitly configure a
    model env var, try reading the workflow's repository variables so the UI
    displays the real active production LLM model instead of a static default."""
    if settings.execution_mode != "github_actions":
        return None
    if not (settings.github_token and settings.github_owner and settings.github_repo):
        return None

    import time

    now = time.monotonic()
    if (
        _REMOTE_LLM_CACHE.get("provider") == provider
        and now - float(_REMOTE_LLM_CACHE.get("checked_at") or 0.0)
        < _REMOTE_LLM_CACHE_TTL_SECONDS
    ):
        return _REMOTE_LLM_CACHE.get("model")

    var_names = ["CODEREVIVE_LLM_MODEL"]
    provider_var = {
        "gemini": "CODEREVIVE_GEMINI_MODEL",
        "nvidia": "CODEREVIVE_NVIDIA_MODEL",
        "openrouter": "CODEREVIVE_OPENROUTER_MODEL",
    }.get(provider)
    if provider_var:
        var_names.append(provider_var)

    resolved: Optional[str] = None
    try:
        from app.integrations.github_actions_client import GitHubActionsClient

        with GitHubActionsClient(
            token=settings.github_token,
            owner=settings.github_owner,
            repo=settings.github_repo,
            workflow_id=settings.github_workflow_id,
            base_url=settings.github_api_base_url,
            timeout_seconds=5.0,
        ) as client:
            for name in var_names:
                val = client.get_repo_variable(name)
                if val:
                    resolved = val
                    break
    except Exception:
        resolved = None

    _REMOTE_LLM_CACHE["checked_at"] = now
    _REMOTE_LLM_CACHE["provider"] = provider
    _REMOTE_LLM_CACHE["model"] = resolved
    return resolved


def get_active_config() -> Dict[str, Any]:
    """Return active non-secret migration defaults from backend/.env + environment."""
    provider = settings.resolve_llm_provider()
    model = settings.resolve_llm_model(provider)
    if (
        settings.execution_mode == "github_actions"
        and not settings.has_explicit_llm_model_config(provider)
    ):
        remote_model = _resolve_remote_gha_llm_model(provider)
        if remote_model:
            model = remote_model
    return {
        "repository_presets": _repository_presets(),
        "repo_url": settings.repo_url,
        "migration_branch_name": settings.migration_branch_name,
        "target_spring_boot_version": settings.target_spring_boot_version,
        "target_java_version": settings.target_java_version,
        "use_openrewrite": settings.use_openrewrite,
        "openrewrite_mode": settings.openrewrite_mode,
        "default_retry_budget": settings.default_retry_budget,
        "llm_provider": provider,
        "llm_model": model,
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

    # Defense in depth: when running in production github_actions mode on Hostless,
    # the backend must never launch Docker locally. Migrations run exclusively on
    # GitHub Actions runners (see backend/app/api/jobs.py). The GHA workflow
    # invokes the run_migration CLI directly, bypassing this service function.
    if settings.execution_mode == "github_actions":
        raise RuntimeError(
            "Local Docker execution is disabled in github_actions execution mode. "
            "Run migrations via the GitHub Actions workflow instead."
        )

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
