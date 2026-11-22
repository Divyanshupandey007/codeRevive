from pathlib import Path

from app.api.routes import get_config_endpoint, health_check
from app.config import Settings, settings
from app.models.execution import TestFailure
from app.orchestrator.service import MigrationJobRequest, resolve_workspace_repo_path
from app.sandbox.diagnostics_parser import is_database_environment_failure


def test_settings_repo_url_configurable_via_env(monkeypatch):
    custom_url = "https://github.com/sinsangkug/SpringBoot_Jpa_Shopping"
    monkeypatch.setenv("CODEREVIVE_REPO_URL", custom_url)
    monkeypatch.setenv("CODEREVIVE_DEFAULT_RETRY_BUDGET", "5")
    custom = Settings(_env_file=None)
    assert custom.repo_url == custom_url
    assert custom.default_retry_budget == 5


def test_resolve_workspace_repo_path_handles_git_and_plain_urls(tmp_path: Path):
    assert (
        resolve_workspace_repo_path(
            "https://github.com/sinsangkug/SpringBoot_Jpa_Shopping",
            workspace_root=tmp_path,
        )
        == tmp_path / "SpringBoot_Jpa_Shopping"
    )
    assert (
        resolve_workspace_repo_path(
            "https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git",
            workspace_root=tmp_path,
        )
        == tmp_path / "mybatis-spring-boot-jpetstore"
    )


def test_api_handlers_expose_single_source_config():
    assert health_check()["status"] == "ok"
    cfg = get_config_endpoint()
    assert cfg["repo_url"] == settings.repo_url
    req = MigrationJobRequest()
    assert req.repo_url == settings.repo_url


def test_config_resolves_shipped_benchmark_fixture():
    cfg = get_config_endpoint()
    labels = [preset["label"] for preset in cfg["repository_presets"]]
    assert labels == ["mybatis-spring-boot-jpetstore"]


def test_database_environment_failure_detection():
    mysql_log = (
        "com.mysql.cj.jdbc.exceptions.CommunicationsException: "
        "Communications link failure\n"
        "Caused by: java.net.ConnectException: Connection refused"
    )
    assert is_database_environment_failure(mysql_log) is True

    postgres_fail = TestFailure(
        test_class="com.example.RepoTest",
        test_method="contextLoads",
        failure_message="PSQLException: Connection to localhost:5432 refused.",
    )
    assert is_database_environment_failure("", [postgres_fail]) is True

    non_db_http_refused = "org.openqa.selenium.WebDriverException: Connection refused to http://localhost:8080/login"
    assert is_database_environment_failure(non_db_http_refused) is False
