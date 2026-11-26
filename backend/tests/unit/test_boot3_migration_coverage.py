from pathlib import Path

from app.ingestion.migration_planner import build_migration_plan
from app.ingestion.openrewrite_runner import DeterministicRewritePass
from app.models.execution import CompilerError, TestFailure
from app.models.manifest import MavenDependency, PomManifest
from app.orchestrator.repair_heuristics import (
    detect_jooq_codegen_skip_defines,
    detect_missing_pom_dependencies,
    is_dependency_classpath_runtime_failure,
    java_errors_only_missing_packages,
    looks_like_app_runtime_failure,
    resolve_sandbox_db_fallback_args,
)
from app.sandbox.diagnostics_parser import parse_maven_output
from app.sandbox.service_detector import detect_required_services


def test_migration_plan_upgrades_springdoc_1x_and_removes_security_companion():
    pom = PomManifest(
        spring_boot_version="2.7.18",
        java_version="11",
        dependencies=[
            MavenDependency(
                group_id="org.springdoc",
                artifact_id="springdoc-openapi-ui",
                version="1.8.0",
            ),
            MavenDependency(
                group_id="org.springdoc",
                artifact_id="springdoc-openapi-security",
                version="1.8.0",
            ),
            MavenDependency(
                group_id="org.springdoc",
                artifact_id="springdoc-openapi-webmvc-core",
                version="1.8.0",
            ),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    by_art = {d.artifact_id: d for d in plan.dependencies}

    assert by_art["springdoc-openapi-ui"].target_strategy == "upgrade_artifact"
    assert by_art["springdoc-openapi-ui"].target_artifact_id == "springdoc-openapi-starter-webmvc-ui"
    assert by_art["springdoc-openapi-ui"].target_version == "2.5.0"
    assert by_art["springdoc-openapi-security"].target_strategy == "remove_artifact"
    assert by_art["springdoc-openapi-webmvc-core"].target_strategy == "remove_artifact"


def test_migration_plan_upgrades_mybatis_plus_rocketmq_jooq_and_removes_mockito_inline():
    pom = PomManifest(
        spring_boot_version="2.7.18",
        java_version="11",
        properties={
            "jooq.version": "3.16.18",
            "org.projectlombok.version": "1.18.20",
        },
        dependencies=[
            MavenDependency(
                group_id="com.baomidou",
                artifact_id="mybatis-plus-boot-starter",
                version="3.4.3",
            ),
            MavenDependency(
                group_id="org.apache.rocketmq",
                artifact_id="rocketmq-spring-boot-starter",
                version="2.2.3",
            ),
            MavenDependency(
                group_id="org.mockito",
                artifact_id="mockito-inline",
                version="4.5.1",
                scope="test",
            ),
            MavenDependency(
                group_id="org.jooq",
                artifact_id="jooq-meta-extensions-liquibase",
                version="3.16.18",
                scope="provided",
            ),
            MavenDependency(
                group_id="org.projectlombok",
                artifact_id="lombok",
                version="1.18.20",
            ),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    by_art = {d.artifact_id: d for d in plan.dependencies}

    assert by_art["mybatis-plus-boot-starter"].target_strategy == "upgrade_artifact"
    assert by_art["mybatis-plus-boot-starter"].target_artifact_id == "mybatis-plus-spring-boot3-starter"
    assert by_art["mybatis-plus-boot-starter"].target_version == "3.5.7"

    assert by_art["rocketmq-spring-boot-starter"].target_strategy == "upgrade_version"
    assert by_art["rocketmq-spring-boot-starter"].target_version == "2.3.1"

    assert by_art["mockito-inline"].target_strategy == "remove_artifact"

    assert by_art["jooq-meta-extensions-liquibase"].target_strategy == "upgrade_version"
    assert by_art["jooq-meta-extensions-liquibase"].target_version == "3.18.14"
    assert plan.properties_to_update["jooq.version"] == "3.18.14"
    assert plan.properties_to_update["org.projectlombok.version"] == "1.18.32"


def test_diagnostics_parser_preserves_root_caused_by_in_test_failure_message():
    logs = """[ERROR] Tests run: 1, Failures: 0, Errors: 1, Time elapsed: 2.1 s -- in com.example.DemoTests
[ERROR] com.example.DemoTests.contextLoads -- Time elapsed: 1.5 s <<< ERROR!
java.lang.IllegalStateException: Failed to load ApplicationContext for [WebMergedContextConfiguration@123]
\tat org.springframework.test.context.cache.DefaultCacheAwareContextLoaderDelegate.loadContext(DefaultCacheAwareContextLoaderDelegate.java:180)
Caused by: org.springframework.beans.factory.BeanCreationException: Error creating bean with name 'openApiResource'
\tat org.springframework.beans.factory.support.AbstractAutowireCapableBeanFactory.doCreateBean(AbstractAutowireCapableBeanFactory.java:607)
Caused by: java.lang.NoClassDefFoundError: javax/servlet/http/HttpServletRequest
\tat org.springdoc.webmvc.api.OpenApiWebmvcResource.<init>(OpenApiWebmvcResource.java:70)
"""
    errors, tests = parse_maven_output(logs)
    assert errors == []
    assert len(tests) == 1
    assert "Failed to load ApplicationContext" in tests[0].failure_message
    assert "javax/servlet/http/HttpServletRequest" in tests[0].failure_message
    assert is_dependency_classpath_runtime_failure(tests, logs) is True
    assert looks_like_app_runtime_failure(tests, logs) is True


def test_missing_jakarta_package_detection_surfaces_bom_starter_and_defers_java_file():
    errs = [
        CompilerError(
            file_path="src/main/java/com/app/UpdatePasswordRequestDTO.java",
            line_number=4,
            column=35,
            message="package jakarta.validation.constraints does not exist",
        ),
        CompilerError(
            file_path="src/main/java/com/app/UpdatePasswordRequestDTO.java",
            line_number=12,
            column=6,
            message="cannot find symbol symbol: class NotNull location: class com.app.UpdatePasswordRequestDTO",
        ),
    ]
    missing = detect_missing_pom_dependencies(errs)
    assert len(missing) == 1
    assert missing[0][0] == "jakarta.validation.constraints"
    assert missing[0][1] == "org.springframework.boot:spring-boot-starter-validation"
    assert java_errors_only_missing_packages(errs, {"jakarta.validation.constraints"}) is True


def test_jooq_codegen_skip_detection_and_fallback(tmp_path: Path):
    gen_dir = tmp_path / "src/main/java/com/balanceservice/dao/generated"
    gen_dir.mkdir(parents=True)
    (gen_dir / "Tables.java").write_text("package com.balanceservice.dao.generated; public class Tables {}", encoding="utf-8")
    (tmp_path / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <build>
    <plugins>
      <plugin>
        <groupId>org.jooq</groupId>
        <artifactId>jooq-codegen-maven</artifactId>
        <configuration>
          <skip>${jooq.skip}</skip>
          <generator>
            <target>
              <packageName>com.balanceservice.dao.generated</packageName>
              <directory>src/main/java</directory>
            </target>
          </generator>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
""",
        encoding="utf-8",
    )
    defines = detect_jooq_codegen_skip_defines(tmp_path, require_existing_sources=True)
    assert "-Djooq.codegen.skip=true" in defines
    assert "-Djooq.skip=true" in defines

    fallback = resolve_sandbox_db_fallback_args(
        repo_path=tmp_path,
        pom_has_h2=False,
        initial_maven_args=["test"],
        current_maven_args=["test"],
        tried_fallbacks=set(),
        raw_logs="[ERROR] Failed to execute goal org.jooq:jooq-codegen-maven:3.16.18:generate: Connection to localhost:5432 refused",
    )
    assert fallback is not None
    key, _, next_args = fallback
    assert key == "jooq_skip"
    assert "-Djooq.skip=true" in next_args
    assert "-Djooq.codegen.skip=true" in next_args


def test_transform_config_resource_updates_mysql_driver_and_spring_redis():
    yaml_in = (
        "spring:\n"
        "  datasource:\n"
        "    driver-class-name: com.mysql.jdbc.Driver\n"
        "  redis:\n"
        "    host: 127.0.0.1\n"
        "    port: 6379\n"
        "  jackson:\n"
        "    default-property-inclusion: non_null\n"
    )
    yaml_out = DeterministicRewritePass.transform_config_resource(
        "src/main/resources/application.yaml",
        yaml_in,
        upgrade_mysql_driver=True,
    )
    assert "com.mysql.cj.jdbc.Driver" in yaml_out
    assert "  data:\n    redis:\n      host: 127.0.0.1\n      port: 6379\n" in yaml_out
    assert "  jackson:\n    default-property-inclusion: non_null\n" in yaml_out

    props_in = "spring.datasource.driver-class-name=com.mysql.jdbc.Driver\nspring.redis.host=localhost\n"
    props_out = DeterministicRewritePass.transform_config_resource(
        "src/main/resources/application.properties",
        props_in,
        upgrade_mysql_driver=True,
    )
    assert "com.mysql.cj.jdbc.Driver" in props_out
    assert "spring.data.redis.host=localhost" in props_out


def test_service_detector_skips_compose_db_when_active_profile_uses_h2(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  postgres:\n    image: postgres:15\n    ports:\n      - '5432:5432'\n",
        encoding="utf-8",
    )
    res_dir = tmp_path / "src/test/resources"
    res_dir.mkdir(parents=True)
    (res_dir / "application-test.yml").write_text(
        "spring:\n  datasource:\n    url: jdbc:h2:mem:testdb\n",
        encoding="utf-8",
    )
    services = detect_required_services(
        tmp_path, maven_args=["-Dspring.profiles.active=test", "test"]
    )
    assert [s.kind for s in services] == []


def test_format_failure_from_summary_classifies_guardrail_env_and_build_errors():
    from app.api.jobs import MigrationJobManager
    from app.models.orchestrator import MigrationRunSummary, WorkflowState

    guardrail_summary = MigrationRunSummary(
        repo_url="https://github.com/org/demo",
        baseline_commit="abc1234",
        final_commit=None,
        target_spring_boot_version="3.2.5",
        openrewrite_used=True,
        openrewrite_files_modified=2,
        total_files_migrated=0,
        total_retries=1,
        file_statuses=[],
        integrity_passed=False,
        total_duration_seconds=12.0,
        final_state=WorkflowState.FAILED,
        integrity_violations=["Baseline @Test count dropped from 5 to 3."],
        last_build_errors=[],
    )
    msg, err_type = MigrationJobManager._format_failure_from_summary(guardrail_summary)
    assert err_type == "IntegrityGuardrailFailed"
    assert "Baseline @Test count dropped" in msg

    env_summary = MigrationRunSummary(
        repo_url="https://github.com/org/demo",
        baseline_commit="abc1234",
        final_commit=None,
        target_spring_boot_version="3.2.5",
        openrewrite_used=True,
        openrewrite_files_modified=1,
        total_files_migrated=0,
        total_retries=0,
        file_statuses=[],
        integrity_passed=False,
        total_duration_seconds=8.0,
        final_state=WorkflowState.FAILED,
        integrity_violations=["Migration aborted before a passing build was reached."],
        last_build_errors=["pom.xml: Connection to localhost:5432 refused"],
    )
    msg, err_type = MigrationJobManager._format_failure_from_summary(env_summary)
    assert err_type == "EnvironmentFailure"
    assert "Sandbox environment failure" in msg
    assert "localhost:5432" in msg

    build_summary = MigrationRunSummary(
        repo_url="https://github.com/org/demo",
        baseline_commit="abc1234",
        final_commit=None,
        target_spring_boot_version="3.2.5",
        openrewrite_used=True,
        openrewrite_files_modified=3,
        total_files_migrated=0,
        total_retries=5,
        file_statuses=[],
        integrity_passed=False,
        total_duration_seconds=90.0,
        final_state=WorkflowState.FAILED,
        integrity_violations=["Migration aborted before a passing build was reached."],
        last_build_errors=[
            "com.example.AppTest.contextLoads: Failed to load ApplicationContext | Caused by: java.lang.NoClassDefFoundError: javax/servlet/http/HttpServletRequest"
        ],
    )
    msg, err_type = MigrationJobManager._format_failure_from_summary(build_summary)
    assert err_type == "MigrationFailed"
    assert "after 5 repair attempt(s)" in msg
    assert "javax/servlet/http/HttpServletRequest" in msg


def test_summarize_failed_run_formats_concise_callback_reason():
    from app.models.orchestrator import MigrationRunSummary, WorkflowState
    from app.orchestrator.run_migration import _summarize_failed_run

    summary = MigrationRunSummary(
        repo_url="https://github.com/org/demo",
        baseline_commit="abc1234",
        final_commit=None,
        target_spring_boot_version="3.2.5",
        openrewrite_used=True,
        openrewrite_files_modified=2,
        total_files_migrated=0,
        total_retries=4,
        file_statuses=[],
        integrity_passed=False,
        total_duration_seconds=60.0,
        final_state=WorkflowState.FAILED,
        integrity_violations=["Migration aborted before a passing build was reached."],
        last_build_errors=[
            "com.example.DemoTests.contextLoads: Failed to load ApplicationContext | Caused by: java.lang.IllegalStateException: Missing bean"
        ],
    )
    reason = _summarize_failed_run(summary)
    assert "after 4 repair attempt(s)" in reason
    assert "Missing bean" in reason
    assert len(reason) <= 400


def test_gha_failed_run_downloads_summary_artifact_and_surfaces_root_cause():
    import io
    import time
    import zipfile

    from app.api.jobs import MigrationJobManager, SUMMARY_ARTIFACT_NAME
    from app.integrations.github_actions_client import (
        RunArtifact,
        RunStatus,
        WorkflowDispatchResult,
    )
    from app.models.orchestrator import MigrationRunSummary, WorkflowState
    from app.orchestrator.service import MigrationJobRequest

    summary = MigrationRunSummary(
        repo_url="https://github.com/org/demo",
        baseline_commit="abc1234",
        final_commit=None,
        target_spring_boot_version="3.2.5",
        openrewrite_used=True,
        openrewrite_files_modified=2,
        total_files_migrated=0,
        total_retries=3,
        file_statuses=[],
        integrity_passed=False,
        total_duration_seconds=45.0,
        final_state=WorkflowState.FAILED,
        integrity_violations=["Migration aborted before a passing build was reached."],
        last_build_errors=[
            "com.example.DemoTests.contextLoads: Failed to load ApplicationContext | Caused by: java.lang.NoClassDefFoundError: javax/servlet/http/HttpServletRequest"
        ],
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("migration_run_summary.json", summary.model_dump_json())
    zip_bytes = buf.getvalue()

    class _FakeFailedClient:
        def dispatch_workflow(self, inputs, ref="main"):
            return WorkflowDispatchResult(
                run_id=77, run_number=1, status="queued", html_url=None, raw={}
            )

        def get_run_status(self, run_id):
            return RunStatus(run_id=run_id, status="completed", conclusion="failure")

        def find_artifact_by_name(self, run_id, name):
            if name == SUMMARY_ARTIFACT_NAME:
                return RunArtifact(
                    artifact_id=901,
                    name=name,
                    size_in_bytes=len(zip_bytes),
                    download_url=None,
                    expired=False,
                    created_at=None,
                )
            return None

        def download_artifact(self, artifact_id, follow_redirects=True):
            return zip_bytes

        def get_job_steps(self, run_id):
            return []

    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=_FakeFailedClient(),
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    rec = manager.submit(MigrationJobRequest(repo_url="https://github.com/org/demo"))
    deadline = time.monotonic() + 5.0
    final = None
    while time.monotonic() < deadline:
        final = manager.get(rec.job_id)
        if final and final.status == "FAILED":
            break
        time.sleep(0.01)

    assert final is not None
    assert final.status == "FAILED"
    assert final.summary is not None
    assert final.error_type == "MigrationFailed"
    assert final.error is not None
    assert "javax/servlet/http/HttpServletRequest" in final.error
    assert "Step failed:" not in final.error
    assert "github actions" not in final.error.lower()


def test_gha_failed_run_falls_back_to_relayed_failed_event_when_no_summary_artifact():
    import threading
    import time

    from app.api.jobs import MigrationJobManager
    from app.integrations.github_actions_client import (
        RunJob,
        RunJobStep,
        RunStatus,
        WorkflowDispatchResult,
    )
    from app.orchestrator.service import MigrationJobRequest

    polled_once = threading.Event()
    allow_finish = threading.Event()

    class _FakeRelayedClient:
        def __init__(self):
            self.polls = 0

        def dispatch_workflow(self, inputs, ref="main"):
            return WorkflowDispatchResult(
                run_id=88, run_number=2, status="queued", html_url=None, raw={}
            )

        def get_run_status(self, run_id):
            self.polls += 1
            if self.polls == 1:
                polled_once.set()
                return RunStatus(run_id=run_id, status="in_progress", conclusion=None)
            allow_finish.wait(timeout=5.0)
            return RunStatus(run_id=run_id, status="completed", conclusion="failure")

        def find_artifact_by_name(self, run_id, name):
            return None

        def download_artifact(self, artifact_id, follow_redirects=True):
            return b""

        def get_job_steps(self, run_id):
            return [
                RunJob(
                    job_id=1,
                    name="migrate",
                    status="completed",
                    conclusion="failure",
                    started_at=None,
                    completed_at=None,
                    steps=[
                        RunJobStep(
                            name="Run CodeRevive migration",
                            status="completed",
                            conclusion="failure",
                            number=1,
                        )
                    ],
                )
            ]

    manager = MigrationJobManager(
        execution_mode="github_actions",
        github_client=_FakeRelayedClient(),
        max_concurrent_jobs=1,
        poll_interval_seconds=0,
    )
    rec = manager.submit(MigrationJobRequest(repo_url="https://github.com/org/demo"))
    assert polled_once.wait(timeout=5.0)
    appended = manager.append_remote_event(
        88,
        stage="failed",
        message="Migration rolled back after 5 repair attempt(s): pom.xml: Missing spring-boot-starter-validation",
    )
    assert appended is True
    allow_finish.set()

    deadline = time.monotonic() + 5.0
    final = None
    while time.monotonic() < deadline:
        final = manager.get(rec.job_id)
        if final and final.status == "FAILED":
            break
        time.sleep(0.01)

    assert final is not None
    assert final.status == "FAILED"
    assert final.error is not None
    assert "Missing spring-boot-starter-validation" in final.error
    assert "Step failed:" not in final.error
