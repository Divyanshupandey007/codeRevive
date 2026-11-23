from pathlib import Path
from typing import Annotated, Any, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Anchor .env to the backend directory so keys load no matter which
# directory the app/tests are launched from. config.py lives at
# backend/app/config.py, so parents[1] is the backend root.
BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODEREVIVE_",
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
    )

    # Single source of truth for default migration target & run settings
    # Override in backend/.env via CODEREVIVE_REPO_URL=... or via CLI --repo-url / API request.
    repo_url: str = "https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git"
    migration_branch_name: str = "migration/spring-boot-3"
    default_retry_budget: int = 5
    workspace_root: Optional[Path] = None

    sandbox_docker_image: str = "coderevive-maven-chrome:17"
    sandbox_timeout_seconds: int = 600
    sandbox_memory_limit: str = "4g"
    sandbox_cpu_count: float = 2.0
    sandbox_network_disabled: bool = False
    sandbox_maven_cache_volume: str = "coderevive-m2-cache"
    sandbox_container_workdir: str = "/app"
    sandbox_raw_log_char_limit: int = 12000
    sandbox_dynamic_services: bool = True
    sandbox_service_ready_timeout_seconds: int = 60
    sandbox_mysql_image: str = "mysql:8.0"
    sandbox_postgres_image: str = "postgres:16-alpine"
    sandbox_redis_image: str = "redis:7-alpine"
    sandbox_mongo_image: str = "mongo:7"
    sandbox_default_maven_args: List[str] = Field(
        default_factory=lambda: [
            "-B",
            "-fae",
            "-Dwebdriver.chrome.driver=/usr/local/bin/chromedriver",
            "-Dwdm.chromeDriverPath=/usr/local/bin/chromedriver",
            "-Dselenide.clickViaJs=true",
            "clean",
            "test",
        ]
    )

    # Target migration versions (configurable, not hardcoded to one repo)
    target_spring_boot_version: str = "3.2.5"
    target_java_version: str = "17"

    # Optional API key. When set, state-changing endpoints (POST /api/migrations,
    # POST /api/migrations/jobs, POST /cancel, DELETE /api/migrations/jobs/{id})
    # require an X-API-Key header matching this value. Left empty for local dev.
    api_key: str = ""

    # Hybrid OpenRewrite configuration
    use_openrewrite: bool = True
    openrewrite_mode: str = "hybrid"  # "hybrid" | "deterministic" | "maven_only"
    openrewrite_timeout_seconds: int = 600
    openrewrite_plugin_version: str = "5.40.0"
    openrewrite_spring_recipe_version: str = "5.19.0"

    # Swap providers here
    llm_provider: str = "gemini"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "mistralai/mistral-large-2-instruct"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-oss-120b"
    openrouter_reasoning_enabled: bool = True
    openrouter_reasoning_effort: Optional[str] = None
    openrouter_site_url: str = ""
    openrouter_app_name: str = "CodeRevive"
    openrouter_timeout_seconds: float = 180.0

    # Guardrails
    guardrail_forbidden_patterns: List[str] = Field(
        default_factory=lambda: [
            "Runtime.getRuntime().exec",
            "ProcessBuilder",
            "System.exit",
        ]
    )

    # GitHub Actions execution mode (production on Hostless).
    # When CODEREVIVE_EXECUTION_MODE == "github_actions", the backend does NOT run
    # Docker locally. Migrations are dispatched to GitHub Actions ubuntu-24.04
    # runners via workflow_dispatch and tracked by run id.
    execution_mode: str = "local"  # "local" | "github_actions"
    github_token: str = ""
    github_owner: str = ""
    github_repo: str = ""
    github_workflow_id: str = "coderevive-migrate.yml"
    github_api_base_url: str = "https://api.github.com"
    max_concurrent_github_actions_jobs: int = 1
    github_actions_poll_interval_seconds: int = 15
    github_actions_job_timeout_seconds: int = 3600

    # HTTP API / Routing / Angular CORS & SPA settings
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_prefix: str = "/api"
    cors_allowed_origins: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:4200",
            "http://127.0.0.1:4200",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    cors_allow_credentials: bool = True
    cors_allowed_methods: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: [
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
            "HEAD",
        ]
    )
    cors_allowed_headers: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: [
            "Content-Type",
            "Authorization",
            "Accept",
            "Origin",
            "X-Requested-With",
            "X-Request-ID",
            "Prefer",
        ]
    )
    cors_max_age_seconds: int = 600
    max_request_body_bytes: int = 1_048_576
    frontend_dist_dir: Optional[Path] = None

    @field_validator(
        "cors_allowed_origins",
        "cors_allowed_methods",
        "cors_allowed_headers",
        mode="before",
    )
    @classmethod
    def _parse_comma_separated_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @property
    def resolved_workspace_root(self) -> Path:
        if self.workspace_root is not None:
            return Path(self.workspace_root)
        return BACKEND_DIR / "workspace"


settings = Settings()