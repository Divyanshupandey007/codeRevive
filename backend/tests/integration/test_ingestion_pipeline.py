from pathlib import Path
import pytest

from app.ingestion.pipeline import IngestionPipeline, UnsupportedRepositoryError


def test_ingests_all_three_spring_boot_fixtures_in_isolated_workspaces(
    fixtures_dir: Path, tmp_path: Path
):
    basic_report = IngestionPipeline(
        workspace_root=tmp_path / "ws1", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-basic"), clean=True)

    assert basic_report.repo_info.isolated_workspace is True
    assert Path(basic_report.repo_info.target_path) != (
        fixtures_dir / "spring-boot-2.7-basic"
    )
    assert basic_report.pom.spring_boot_version == "2.7.14"
    assert basic_report.baseline_test_count == 1
    assert basic_report.migration_plan.target_spring_boot == "3.2.5"
    basic_targets = {t.file_path: t for t in basic_report.files_to_migrate}
    # User.java and UserController.java have Jakarta javax.*; UserService.java only has javax.sql.DataSource
    assert "src/main/java/com/example/crud/model/User.java" in basic_targets
    assert "src/main/java/com/example/crud/web/UserController.java" in basic_targets
    assert "src/main/java/com/example/crud/service/UserService.java" not in basic_targets

    sec_report = IngestionPipeline(
        workspace_root=tmp_path / "ws2", isolate_local=True
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)
    sec_targets = {t.file_path: t for t in sec_report.files_to_migrate}
    assert "security-configurer-adapter-removal" in sec_targets[
        "src/main/java/com/example/sec/config/SecurityConfig.java"
    ].matched_rules
    assert "pbkdf2-password-encoder-upgrade" in sec_targets[
        "src/main/java/com/example/sec/config/SecurityConfig.java"
    ].matched_rules

    th_report = IngestionPipeline(
        workspace_root=tmp_path / "ws3", isolate_local=True
    ).run(str(fixtures_dir / "spring-boot-2.7-thymeleaf"), clean=True)
    th_targets = {t.file_path: t for t in th_report.files_to_migrate}
    assert "src/main/resources/application.yml" in th_targets
    assert "src/main/resources/templates/layout.html" in th_targets
    assert "thymeleaf-spring5-to-spring6" in th_targets[
        "src/main/java/com/example/web/HomeController.java"
    ].matched_rules


def test_rejects_multi_module_repository_explicitly(tmp_path: Path):
    multi = tmp_path / "multi-repo"
    multi.mkdir()
    (multi / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><modules><module>a</module></modules></project>",
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedRepositoryError, match="Multi-module"):
        IngestionPipeline(workspace_root=tmp_path / "ws", isolate_local=True).run(
            str(multi), clean=True
        )
