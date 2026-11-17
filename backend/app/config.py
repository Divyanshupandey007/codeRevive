from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    @property
    def resolved_workspace_root(self) -> Path:
        if self.workspace_root is not None:
            return Path(self.workspace_root)
        return BACKEND_DIR / "workspace"


settings = Settings()