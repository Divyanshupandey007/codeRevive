from pathlib import Path
from typing import List, Optional, Type, TypeVar

from git import Repo
from pydantic import BaseModel

from app.ingestion.pipeline import IngestionPipeline
from app.llm.base import LLMProvider, LLMProviderError
from app.models.agent import FilePatchResult
from app.models.execution import BuildResult, CompilerError
from app.models.orchestrator import FileStatus, WorkflowState
from app.orchestrator.workflow import MigrationOrchestrator

T = TypeVar("T", bound=BaseModel)


class MockLLMProvider(LLMProvider):
    def __init__(self, handler):
        self.handler = handler
        self.calls: List[str] = []

    def generate_text(self, prompt: str, system_instruction: Optional[str] = None, temperature: float = 0.2) -> str:
        return ""

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
    ) -> T:
        # Extract Target file line
        target = ""
        for line in prompt.splitlines():
            if line.startswith("Target file: "):
                target = line.removeprefix("Target file: ").strip()
                break
        self.calls.append(target)
        return self.handler(target, prompt)


class FakeDockerRunner:
    def __init__(self, results: List[BuildResult]):
        self.results = list(results)
        self.calls = 0

    def run_tests(self, repo_path: Path, maven_args=None) -> BuildResult:
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


def test_orchestrator_hybrid_openrewrite_plus_llm_and_repair_succeeds(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)

    repo_path = Path(report.repo_info.target_path)

    def llm_handler(target: str, prompt: str) -> FilePatchResult:
        if target.endswith("SecurityConfig.java"):
            if "missing SecurityFilterChain bean" in prompt:
                return FilePatchResult(
                    file_path=target,
                    new_content="""package com.example.sec.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
public class SecurityConfig {
    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http.csrf(csrf -> csrf.disable())
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/api/public/**").permitAll()
                .anyRequest().authenticated());
        return http.build();
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5();
    }
}
""",
                    explanation="Migrated WebSecurityConfigurerAdapter to SecurityFilterChain bean",
                )
            # Round 1 repair still missed the SecurityFilterChain bean
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.sec.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;

@Configuration
public class SecurityConfig {
    @Bean
    public PasswordEncoder passwordEncoder() {
        return Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5();
    }
}
""",
                explanation="Partial Pass 2 AI repair",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.2,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=8,
                        column=5,
                        message="cannot find symbol: class WebSecurityConfigurerAdapter",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            ),
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.1,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=10,
                        column=5,
                        message="missing SecurityFilterChain bean",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            ),
            BuildResult(
                success=True,
                exit_code=0,
                duration_seconds=1.5,
                raw_logs="[INFO] BUILD SUCCESS",
            ),
        ]
    )

    mock_llm = MockLLMProvider(llm_handler)
    orchestrator = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=mock_llm,
        docker_runner=fake_docker,
        use_openrewrite=True,
    )
    summary = orchestrator.run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert summary.openrewrite_used is True
    assert summary.final_commit != report.repo_info.baseline_sha
    assert fake_docker.calls == 3

    # OpenRewrite handled pom.xml and JwtAuthFilter.java deterministically in Pass 1 without calling LLM on them!
    assert "pom.xml" not in mock_llm.calls
    assert "src/main/java/com/example/sec/filter/JwtAuthFilter.java" not in mock_llm.calls
    assert mock_llm.calls == [
        "src/main/java/com/example/sec/config/SecurityConfig.java",
        "src/main/java/com/example/sec/config/SecurityConfig.java",
    ]
    # Verify Java SE javax.crypto.Cipher stayed javax.* while javax.servlet became jakarta.servlet
    jwt_text = (repo_path / "src/main/java/com/example/sec/filter/JwtAuthFilter.java").read_text(encoding="utf-8")
    assert "import javax.crypto.Cipher;" in jwt_text
    assert "import jakarta.servlet.FilterChain;" in jwt_text


def test_orchestrator_prevents_false_success_when_pass2_llm_fails(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    def failing_llm(target: str, prompt: str) -> FilePatchResult:
        raise LLMProviderError("simulated API outage")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.5,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=8,
                        column=1,
                        message="cannot find symbol: class WebSecurityConfigurerAdapter",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            )
        ]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(failing_llm),
        docker_runner=fake_docker,
        use_openrewrite=True,
    ).run(report)

    # SecurityConfig.java failed LLM generation in Pass 2 -> MUST NOT report COMPLETED or SUCCESS!
    assert summary.final_state == WorkflowState.FAILED
    assert summary.final_commit == report.repo_info.baseline_sha
    by_path = {s.file_path: s for s in summary.file_statuses}
    assert by_path["src/main/java/com/example/sec/config/SecurityConfig.java"].status == FileStatus.FAILED


def test_orchestrator_rejects_unexpected_file_path_and_never_commits(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    def malicious_llm(target: str, prompt: str) -> FilePatchResult:
        return FilePatchResult(
            file_path="pom.xml",  # Different valid file inside the repo!
            new_content="<project/>",
            explanation="sneaky overwrite",
        )

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.5,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=8,
                        column=1,
                        message="cannot find symbol: class WebSecurityConfigurerAdapter",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            )
        ]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(malicious_llm),
        docker_runner=fake_docker,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.FAILED
    assert summary.final_commit == report.repo_info.baseline_sha
    by_path = {s.file_path: s for s in summary.file_statuses}
    assert by_path["src/main/java/com/example/sec/config/SecurityConfig.java"].status == FileStatus.REJECTED


def test_orchestrator_runs_integrity_checks_before_commit_and_rolls_back_on_violation(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True
    ).run(str(fixtures_dir / "spring-boot-2.7-basic"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    # Disable OpenRewrite so Pass 2 handles User.java on compiler error and drops public getName() signature
    def signature_breaking_llm(target: str, prompt: str) -> FilePatchResult:
        if target.endswith("User.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.crud.model;
import jakarta.persistence.Entity;
@Entity
public class User {
    public String getName(String suffix) { return suffix; }
}
""",
                explanation="broke public signature",
            )
        content = (repo_path / target).read_text(encoding="utf-8")
        return FilePatchResult(
            file_path=target,
            new_content=content.replace("javax.validation", "jakarta.validation").replace("2.7.14", "3.2.5"),
            explanation="migrated",
        )

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.5,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/crud/model/User.java",
                        line_number=3,
                        column=8,
                        message="package javax.persistence does not exist",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            ),
            BuildResult(success=True, exit_code=0, duration_seconds=0.5, raw_logs="BUILD SUCCESS"),
        ]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(signature_breaking_llm),
        docker_runner=fake_docker,
        use_openrewrite=False,
    ).run(report)

    assert summary.final_state == WorkflowState.FAILED
    assert summary.integrity_passed is False
    # Crucial: NO commit was created because integrity checks ran BEFORE commit!
    assert summary.final_commit == report.repo_info.baseline_sha
    assert Repo(repo_path).head.commit.hexsha == report.repo_info.baseline_sha


def test_orchestrator_pivots_runtime_test_failures_to_migrated_app_files(
    fixtures_dir: Path, tmp_path: Path
):
    from app.models.execution import TestFailure

    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    # Round 1 fails on a Selenium/UI runtime TestFailure on SecurityConfigTest.
    # Repair MUST pivot to unmigrated/planned SecurityConfig.java with server log excerpt instead of rewriting the test!
    prompts_seen = {}

    def llm_handler(target: str, prompt: str) -> FilePatchResult:
        prompts_seen.setdefault(target, []).append(prompt)
        if target.endswith("SecurityConfig.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.sec.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
public class SecurityConfig {
    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http.csrf(csrf -> csrf.disable())
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/api/public/**").permitAll()
                .anyRequest().authenticated());
        return http.build();
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5();
    }
}
""",
                explanation="Switched to SecurityFilterChain and v5_5 encoder",
            )
        raise AssertionError(f"Should not mutate unrelated/test file: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                failed_tests=[
                    TestFailure(
                        test_class="com.example.sec.config.SecurityConfigTest",
                        test_method="testLogin",
                        failure_message="Element not found {#WelcomeContent} Expected: exist",
                    )
                ],
                raw_logs="[ERROR] Caused by: org.hsqldb.HsqlException: data exception: string data, right truncation",
            ),
            BuildResult(
                success=True,
                exit_code=0,
                duration_seconds=1.0,
                raw_logs="[INFO] BUILD SUCCESS",
            ),
        ]
    )

    mock_llm = MockLLMProvider(llm_handler)
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=mock_llm,
        docker_runner=fake_docker,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert mock_llm.calls == [
        "src/main/java/com/example/sec/config/SecurityConfig.java",
    ]
    assert "HsqlException: data exception" in prompts_seen["src/main/java/com/example/sec/config/SecurityConfig.java"][0]


def test_orchestrator_skips_rebuild_when_repair_produces_no_diff(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    def noop_repair_llm(target: str, prompt: str) -> FilePatchResult:
        current = (repo_path / target).read_text(encoding="utf-8")
        return FilePatchResult(file_path=target, new_content=current, explanation="no change")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=10,
                        column=5,
                        message="cannot find symbol",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            )
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(noop_repair_llm),
        docker_runner=fake_docker,
        retry_budget=3,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.FAILED
    # Stops immediately after no-op repair instead of burning 3 wasted Docker runs!
    assert fake_docker.calls == 1


class _RecordingDockerRunner:
    def __init__(self, results: List[BuildResult]):
        self.results = list(results)
        self.calls: List[Optional[List[str]]] = []

    def run_tests(self, repo_path: Path, maven_args=None) -> BuildResult:
        idx = min(len(self.calls), len(self.results) - 1)
        self.calls.append(list(maven_args) if maven_args is not None else None)
        return self.results[idx]


def _make_mysql_plus_h2_repo(
    root: Path, include_test_profile: bool = True, include_h2_dep: bool = True
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    h2_dep = (
        """
        <dependency>
            <groupId>com.h2database</groupId>
            <artifactId>h2</artifactId>
            <scope>runtime</scope>
        </dependency>"""
        if include_h2_dep
        else ""
    )
    (root / "pom.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.11</version>
    </parent>
    <groupId>com.shop</groupId>
    <artifactId>shop</artifactId>
    <version>0.0.1-SNAPSHOT</version>
    <properties>
        <java.version>11</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>{h2_dep}
        <dependency>
            <groupId>com.mysql</groupId>
            <artifactId>mysql-connector-j</artifactId>
            <scope>runtime</scope>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )
    res_dir = root / "src/main/resources"
    res_dir.mkdir(parents=True, exist_ok=True)
    prod_props = (
        "spring.datasource.driver-class-name=com.mysql.cj.jdbc.Driver\n"
        "spring.datasource.url=jdbc:mysql://localhost:3306/shop?serverTimezone=UTC\n"
    )
    (res_dir / "application.properties").write_text(prod_props, encoding="utf-8")
    if include_test_profile:
        (res_dir / "application-test.properties").write_text(
            "spring.datasource.driver-class-name=org.h2.Driver\n"
            "spring.datasource.url=jdbc:h2:mem:test\n",
            encoding="utf-8",
        )

    entity_dir = root / "src/main/java/com/shop/entity"
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "Item.java").write_text(
        """package com.shop.entity;

import javax.persistence.Entity;
import javax.persistence.Id;

@Entity
public class Item {
    @Id
    private Long id;
    public Long getId() { return id; }
}
""",
        encoding="utf-8",
    )
    test_dir = root / "src/test/java/com/shop"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "ShopApplicationTests.java").write_text(
        """package com.shop;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class ShopApplicationTests {
    @Test
    void contextLoads() {
        assert true;
    }
}
""",
        encoding="utf-8",
    )
    return root


def test_orchestrator_retries_with_test_profile_on_mysql_sandbox_connection_failure(
    tmp_path: Path,
):
    src_repo = _make_mysql_plus_h2_repo(
        tmp_path / "shop-src", include_test_profile=True, include_h2_dep=True
    )
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    mysql_failure = BuildResult(
        success=False,
        exit_code=1,
        duration_seconds=1.5,
        raw_logs=(
            "[ERROR] ShopApplicationTests.contextLoads -- Time elapsed: 1.2 s <<< ERROR!\n"
            "com.mysql.cj.jdbc.exceptions.CommunicationsException: Communications link failure\n"
            "Caused by: java.net.ConnectException: Connection refused"
        ),
    )
    green_with_profile = BuildResult(
        success=True, exit_code=0, duration_seconds=2.0, raw_logs="[INFO] BUILD SUCCESS"
    )
    runner = _RecordingDockerRunner([mysql_failure, green_with_profile])

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(lambda t, p: None),
        docker_runner=runner,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert len(runner.calls) == 2
    assert runner.calls[0] is None
    assert runner.calls[1] is not None
    assert "-Dspring.profiles.active=test" in runner.calls[1]
    # Production application.properties remains completely untouched
    prod_text = (repo_path / "src/main/resources/application.properties").read_text(
        encoding="utf-8"
    )
    assert "jdbc:mysql://localhost:3306/shop" in prod_text


def test_orchestrator_retries_with_h2_overrides_when_no_test_profile_exists(
    tmp_path: Path,
):
    src_repo = _make_mysql_plus_h2_repo(
        tmp_path / "shop-no-profile", include_test_profile=False, include_h2_dep=True
    )
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    postgres_failure = BuildResult(
        success=False,
        exit_code=1,
        duration_seconds=1.0,
        raw_logs="org.postgresql.util.PSQLException: Connection to localhost:5432 refused.",
    )
    green = BuildResult(
        success=True, exit_code=0, duration_seconds=1.5, raw_logs="[INFO] BUILD SUCCESS"
    )
    runner = _RecordingDockerRunner([postgres_failure, green])

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(lambda t, p: None),
        docker_runner=runner,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert len(runner.calls) == 2
    assert runner.calls[1] is not None
    assert any(a.startswith("-Dspring.datasource.url=jdbc:h2:mem:") for a in runner.calls[1])


def test_orchestrator_aborts_on_db_env_failure_when_no_h2_or_test_profile(
    tmp_path: Path,
):
    src_repo = _make_mysql_plus_h2_repo(
        tmp_path / "shop-no-h2", include_test_profile=False, include_h2_dep=False
    )
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    mysql_failure = BuildResult(
        success=False,
        exit_code=1,
        duration_seconds=1.0,
        raw_logs="com.mysql.cj.jdbc.exceptions.CommunicationsException: Communications link failure",
    )
    runner = _RecordingDockerRunner([mysql_failure])

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(lambda t, p: None),
        docker_runner=runner,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.FAILED
    assert len(runner.calls) == 1


def test_orchestrator_retries_collapsed_java_formatting_without_wasting_build(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-security"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    prompts_seen: List[str] = []

    def formatting_llm(target: str, prompt: str) -> FilePatchResult:
        prompts_seen.append(prompt)
        if len(prompts_seen) == 1:
            # Simulates Gemini collapsing newlines so @Beanpublic / @Autowiredprivate appear
            return FilePatchResult(
                file_path=target,
                new_content=(
                    "package com.example.sec.config; "
                    "import org.springframework.context.annotation.Bean; "
                    "import org.springframework.context.annotation.Configuration; "
                    "@Configurationpublic class SecurityConfig { @Beanpublic String x() { return \"ok\"; } }"
                ),
                explanation="Collapsed single-line patch",
            )
        return FilePatchResult(
            file_path=target,
            new_content="""package com.example.sec.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
public class SecurityConfig {
    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http.csrf(csrf -> csrf.disable())
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/api/public/**").permitAll()
                .anyRequest().authenticated());
        return http.build();
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5();
    }
}
""",
            explanation="Clean multi-line retry",
        )

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.8,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=8,
                        column=1,
                        message="cannot find symbol: class WebSecurityConfigurerAdapter",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=1.0, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(formatting_llm),
        docker_runner=fake_docker,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert len(prompts_seen) == 2
    assert "CRITICAL FORMATTING CORRECTION" in prompts_seen[1]
    # Format failure was caught immediately before writing to disk or wasting an extra Docker build!
    assert fake_docker.calls == 2


def _make_security_di_and_exception_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.5.1</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>security-auth</artifactId>
    <version>0.0.1-SNAPSHOT</version>
    <properties>
        <java.version>11</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-security</artifactId>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )

    cfg_dir = root / "src/main/java/com/example/security/config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "PasswordEncoderConfig.java").write_text(
        """package com.example.security.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;

@Configuration
public class PasswordEncoderConfig {
    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }
}
""",
        encoding="utf-8",
    )
    (cfg_dir / "BasicSecurityConfig.java").write_text(
        """package com.example.security.config;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.security.config.annotation.authentication.builders.AuthenticationManagerBuilder;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;

public class BasicSecurityConfig extends WebSecurityConfigurerAdapter {
    @Autowired
    private BCryptPasswordEncoder passwordEncoder;

    @Override
    protected void configure(HttpSecurity http) throws Exception {
        http.authorizeRequests().anyRequest().authenticated().and().httpBasic();
    }

    @Override
    protected void configure(AuthenticationManagerBuilder auth) throws Exception {
        auth.inMemoryAuthentication().withUser("user").password(passwordEncoder.encode("pass")).roles("USER");
    }
}
""",
        encoding="utf-8",
    )

    ex_dir = root / "src/main/java/com/example/security/exception"
    ex_dir.mkdir(parents=True, exist_ok=True)
    (ex_dir / "SecurityExceptionHandler.java").write_text(
        """package com.example.security.exception;

import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ControllerAdvice;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

@ControllerAdvice
public class SecurityExceptionHandler extends ResponseEntityExceptionHandler {
    @Override
    protected ResponseEntity<Object> handleMethodArgumentNotValid(
            MethodArgumentNotValidException ex,
            HttpHeaders headers,
            HttpStatus status,
            WebRequest request) {
        return new ResponseEntity<>("invalid", HttpStatus.BAD_REQUEST);
    }
}
""",
        encoding="utf-8",
    )

    test_dir = root / "src/test/java/com/example/security"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "SecurityOaut25ApplicationTests.java").write_text(
        """package com.example.security;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class SecurityOaut25ApplicationTests {
    @Test
    void contextLoads() {
    }
}
""",
        encoding="utf-8",
    )
    return root


def test_orchestrator_pivots_spring_context_di_failure_even_after_prior_compiler_repair(
    tmp_path: Path,
):
    from app.models.execution import TestFailure

    src_repo = _make_security_di_and_exception_repo(tmp_path / "sec-src")
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    def llm_handler(target: str, prompt: str) -> FilePatchResult:
        if target.endswith("SecurityOaut25ApplicationTests.java"):
            raise AssertionError("Orchestrator must NEVER mutate SecurityOaut25ApplicationTests.java")
        if target.endswith("SecurityExceptionHandler.java"):
            raise AssertionError("Deterministic pass should handle SecurityExceptionHandler.java without LLM")
        if target.endswith("BasicSecurityConfig.java"):
            if "UnsatisfiedDependencyException" in prompt or "No qualifying bean" in prompt:
                return FilePatchResult(
                    file_path=target,
                    new_content="""package com.example.security.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
@EnableWebSecurity
public class BasicSecurityConfig {
    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http.authorizeHttpRequests(auth -> auth.anyRequest().authenticated())
            .httpBasic(Customizer.withDefaults());
        return http.build();
    }
}
""",
                    explanation="Removed concrete BCryptPasswordEncoder autowiring so Spring Security uses PasswordEncoder bean",
                )
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.security.config;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
@EnableWebSecurity
public class BasicSecurityConfig {
    @Autowired
    private BCryptPasswordEncoder passwordEncoder;

    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http.authorizeHttpRequests(auth -> auth.anyRequest().authenticated())
            .httpBasic(Customizer.withDefaults());
        return http.build();
    }
}
""",
                explanation="Converted WebSecurityConfigurerAdapter to SecurityFilterChain but kept BCryptPasswordEncoder field",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            # Round 1: Compiler error on BasicSecurityConfig (WebSecurityConfigurerAdapter removed in Security 6)
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/security/config/BasicSecurityConfig.java",
                        line_number=9,
                        column=1,
                        message="cannot find symbol: class WebSecurityConfigurerAdapter",
                    )
                ],
                raw_logs="[ERROR] COMPILATION ERROR",
            ),
            # Round 2: Compiles cleanly, but ApplicationContext fails on BasicSecurityConfig bean DI!
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.2,
                failed_tests=[
                    TestFailure(
                        test_class="com.example.security.SecurityOaut25ApplicationTests",
                        test_method="contextLoads",
                        failure_message="Failed to load ApplicationContext for [WebMergedContextConfiguration]",
                    )
                ],
                raw_logs=(
                    "[ERROR] SecurityOaut25ApplicationTests.contextLoads -- Time elapsed: 1.2 s <<< ERROR!\n"
                    "java.lang.IllegalStateException: Failed to load ApplicationContext\n"
                    "Caused by: org.springframework.beans.factory.UnsatisfiedDependencyException: "
                    "Error creating bean with name 'basicSecurityConfig': Unsatisfied dependency expressed through field 'passwordEncoder': "
                    "No qualifying bean of type 'org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder' available"
                ),
            ),
            # Round 3: Green after pivoting back to BasicSecurityConfig!
            BuildResult(
                success=True,
                exit_code=0,
                duration_seconds=1.5,
                raw_logs="[INFO] BUILD SUCCESS",
            ),
        ]
    )

    mock_llm = MockLLMProvider(llm_handler)
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=mock_llm,
        docker_runner=fake_docker,
        use_openrewrite=True,
        retry_budget=3,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert fake_docker.calls == 3
    assert mock_llm.calls == [
        "src/main/java/com/example/security/config/BasicSecurityConfig.java",
        "src/main/java/com/example/security/config/BasicSecurityConfig.java",
    ]
    ex_text = (
        repo_path / "src/main/java/com/example/security/exception/SecurityExceptionHandler.java"
    ).read_text(encoding="utf-8")
    assert "import org.springframework.http.HttpStatusCode;" in ex_text
    assert "HttpStatusCode status" in ex_text


def test_orchestrator_aborts_cleanly_with_diagnostics_on_sandbox_timeout(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-basic"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    class ExplodingLLM(LLMProvider):
        def generate_text(self, prompt: str, system_instruction: Optional[str] = None, temperature: float = 0.2) -> str:
            raise AssertionError("LLM should not be called on deterministic pass + timeout abort")

        def generate_structured(self, prompt: str, response_schema: Type[T], system_instruction: Optional[str] = None, temperature: float = 0.1) -> T:
            raise AssertionError("LLM should not be called on timeout abort")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=124,
                duration_seconds=600.0,
                compiler_errors=[
                    CompilerError(
                        file_path=None,
                        message="Sandbox command timed out after 600s (exit code 124).",
                    )
                ],
                raw_logs="[INFO] Compiling 430 source files\n[ERROR] Sandbox command timed out after 600s (exit code 124).",
            )
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=ExplodingLLM(),
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
    ).run(report)

    assert summary.final_state == WorkflowState.FAILED
    assert summary.final_commit == report.repo_info.baseline_sha
    assert fake_docker.calls == 1
    assert any("timed out" in err.lower() for err in summary.last_build_errors)


def _make_regex_escape_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>regex-demo</artifactId>
    <version>0.0.1-SNAPSHOT</version>
    <properties>
        <java.version>11</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )
    svc_dir = root / "src/main/java/com/example/service"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "SurveyStatsService.java").write_text(
        """package com.example.service;

import javax.servlet.http.HttpServletRequest;
import org.springframework.stereotype.Service;

@Service
public class SurveyStatsService {
    public String stripTags(String htmlStr, HttpServletRequest req) {
        String regExScript = "<[\\\\s]*?script[^>]*?>[\\\\s\\\\S]*?<[\\\\s]*?\\\\/[\\\\s]*?script[\\\\s]*?>";
        return htmlStr.replaceAll(regExScript, "").replaceAll("<(.*)>(.*)<\\\\/(.*)>", "");
    }
}
""",
        encoding="utf-8",
    )
    return root


def test_orchestrator_pass1_restores_stripped_java_backslashes_from_llm_output(
    tmp_path: Path,
):
    src_repo = _make_regex_escape_repo(tmp_path / "regex-pass1-src")
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    def backslash_stripping_llm(target: str, prompt: str) -> FilePatchResult:
        if target == "pom.xml":
            pom_text = (repo_path / "pom.xml").read_text(encoding="utf-8")
            return FilePatchResult(
                file_path=target,
                new_content=pom_text.replace("2.7.14", "3.2.5").replace("<java.version>11</java.version>", "<java.version>17</java.version>"),
                explanation="Upgraded pom.xml",
            )
        if target.endswith("SurveyStatsService.java"):
            # Simulate LLM updating javax.servlet -> jakarta.servlet while collapsing \\ -> \
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.service;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.stereotype.Service;

@Service
public class SurveyStatsService {
    public String stripTags(String htmlStr, HttpServletRequest req) {
        String regExScript = "<[\\s]*?script[^>]*?>[\\s\\S]*?<[\\s]*?\\/[\\s]*?script[\\s]*?>";
        return htmlStr.replaceAll(regExScript, "").replaceAll("<(.*)>(.*)<\\/(.*)>", "");
    }
}
""",
                explanation="Migrated javax.servlet to jakarta.servlet",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.5,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/service/SurveyStatsService.java",
                        line_number=3,
                        column=1,
                        message="package javax.servlet.http does not exist",
                    )
                ],
                raw_logs="[ERROR] compilation failed",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=0.5, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(backslash_stripping_llm),
        docker_runner=fake_docker,
        use_openrewrite=False,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    migrated = (
        repo_path / "src/main/java/com/example/service/SurveyStatsService.java"
    ).read_text(encoding="utf-8")
    assert "import jakarta.servlet.http.HttpServletRequest;" in migrated
    assert "<[\\\\s]*?script[^>]*?>[\\\\s\\\\S]*?<[\\\\s]*?\\\\/[\\\\s]*?script[\\\\s]*?>" in migrated
    assert "<(.*)>(.*)<\\\\/(.*)>" in migrated


def test_orchestrator_pass2_repairs_illegal_escape_character_even_when_llm_repeats_single_backslash(
    tmp_path: Path,
):
    src_repo = _make_regex_escape_repo(tmp_path / "regex-pass2-src")
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    # First AI repair returns a modified line with single backslashes (cannot match original directly
    # because the method body changed slightly), triggering javac illegal escape character on the next round.
    broken_version = """package com.example.service;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.stereotype.Service;

@Service
public class SurveyStatsService {
    public String stripTags(String htmlStr, HttpServletRequest req) {
        String regExScript = "<[\\s]*?script[^>]*?>[\\s\\S]*?<[\\s]*?\\/[\\s]*?script[\\s]*?>";
        return htmlStr.replaceAll(regExScript, "").replaceAll("<(.*)\\/>", "");
    }
}
"""

    prompts_seen: List[str] = []

    def stubborn_llm(target: str, prompt: str) -> FilePatchResult:
        if target == "pom.xml":
            pom_text = (repo_path / "pom.xml").read_text(encoding="utf-8")
            return FilePatchResult(
                file_path=target,
                new_content=pom_text.replace("2.7.14", "3.2.5").replace("<java.version>11</java.version>", "<java.version>17</java.version>"),
                explanation="Upgraded pom.xml",
            )
        prompts_seen.append(prompt)
        # On BOTH Pass 2 repair rounds, LLM returns broken_version with single \s and \/
        return FilePatchResult(
            file_path=target,
            new_content=broken_version,
            explanation="Updated servlet import",
        )

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/service/SurveyStatsService.java",
                        line_number=3,
                        column=1,
                        message="package javax.servlet.http does not exist",
                    )
                ],
                raw_logs="[ERROR] package javax.servlet.http does not exist",
            ),
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/service/SurveyStatsService.java",
                        line_number=9,
                        column=55,
                        message="illegal escape character",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/service/SurveyStatsService.java",
                        line_number=10,
                        column=72,
                        message="illegal escape character",
                    ),
                ],
                raw_logs="[ERROR] illegal escape character",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=1.0, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(stubborn_llm),
        docker_runner=fake_docker,
        use_openrewrite=False,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert fake_docker.calls == 3
    assert len(prompts_seen) == 2
    assert "Note on 'illegal escape character'" in prompts_seen[1]
    repaired = (
        repo_path / "src/main/java/com/example/service/SurveyStatsService.java"
    ).read_text(encoding="utf-8")
    assert "[\\\\s\\\\S]" in repaired
    assert "<[\\\\s]*?\\\\/[\\\\s]*?script" in repaired
    assert "<(.*)\\\\/>" in repaired


def _make_hibernate_5_dao_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>hib-demo</artifactId>
    <version>0.0.1-SNAPSHOT</version>
    <properties>
        <java.version>11</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )
    dao_dir = root / "src/main/java/com/example/dao"
    dao_dir.mkdir(parents=True, exist_ok=True)
    (dao_dir / "BaseDao.java").write_text(
        """package com.example.dao;

import java.util.List;
import org.hibernate.criterion.Criterion;

public interface BaseDao<T> {
    List<T> findByCriteria(Criterion... criterions);
    T getById(Long id);
}
""",
        encoding="utf-8",
    )
    (dao_dir / "SqlReportDao.java").write_text(
        """package com.example.dao;

import org.hibernate.SQLQuery;
import org.hibernate.Session;

public class SqlReportDao {
    public int countAll(Session session) {
        SQLQuery q = session.createSQLQuery("SELECT count(*) FROM orders");
        return ((Number) q.uniqueResult()).intValue();
    }
}
""",
        encoding="utf-8",
    )
    svc_dir = root / "src/main/java/com/example/service"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "OrderService.java").write_text(
        """package com.example.service;

import com.example.dao.BaseDao;
import java.util.List;
import org.hibernate.criterion.Restrictions;

public class OrderService {
    private final BaseDao<String> baseDao;

    public OrderService(BaseDao<String> baseDao) {
        this.baseDao = baseDao;
    }

    public List<String> findActive() {
        return baseDao.findByCriteria(Restrictions.eq("status", "ACTIVE"));
    }
}
""",
        encoding="utf-8",
    )
    return root


def test_orchestrator_injects_updated_local_dependency_signatures_and_allows_hibernate5_migration(
    tmp_path: Path,
):
    src_repo = _make_hibernate_5_dao_repo(tmp_path / "hib5-src")
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(src_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    prompts_by_file: dict[str, str] = {}

    def hib6_llm(target: str, prompt: str) -> FilePatchResult:
        prompts_by_file[target] = prompt
        if target.endswith("SqlReportDao.java"):
            raise AssertionError("SqlReportDao.java should be migrated deterministically without LLM")
        if target.endswith("BaseDao.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.dao;

import jakarta.persistence.criteria.CriteriaBuilder;
import jakarta.persistence.criteria.Predicate;
import jakarta.persistence.criteria.Root;
import java.util.List;
import java.util.function.BiFunction;

public interface BaseDao<T> {
    List<T> findByPredicate(BiFunction<CriteriaBuilder, Root<T>, Predicate> spec);
    T getById(Long id);
}
""",
                explanation="Migrated BaseDao to JPA Criteria BiFunction signature",
            )
        if target.endswith("OrderService.java"):
            assert "Updated local project dependencies" in prompt
            assert "findByPredicate(BiFunction<CriteriaBuilder, Root<T>, Predicate> spec)" in prompt
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.service;

import com.example.dao.BaseDao;
import java.util.List;

public class OrderService {
    private final BaseDao<String> baseDao;

    public OrderService(BaseDao<String> baseDao) {
        this.baseDao = baseDao;
    }

    public List<String> findActive() {
        return baseDao.findByPredicate((cb, root) -> cb.equal(root.get("status"), "ACTIVE"));
    }
}
""",
                explanation="Aligned OrderService with updated BaseDao.findByPredicate",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.8,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/dao/BaseDao.java",
                        line_number=4,
                        column=1,
                        message="package org.hibernate.criterion does not exist",
                    )
                ],
                raw_logs="[ERROR] BaseDao.java:[4,1] package org.hibernate.criterion does not exist",
            ),
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.8,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/service/OrderService.java",
                        line_number=5,
                        column=1,
                        message="package org.hibernate.criterion does not exist",
                    )
                ],
                raw_logs="[ERROR] OrderService.java:[5,1] package org.hibernate.criterion does not exist",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=0.8, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(hib6_llm),
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert fake_docker.calls == 3
    assert "src/main/java/com/example/dao/BaseDao.java" in prompts_by_file
    assert "src/main/java/com/example/service/OrderService.java" in prompts_by_file
    sql_dao_text = (repo_path / "src/main/java/com/example/dao/SqlReportDao.java").read_text(
        encoding="utf-8"
    )
    assert "import org.hibernate.query.NativeQuery;" in sql_dao_text
    assert "session.createNativeQuery(" in sql_dao_text


def test_orchestrator_shiro_2_deterministic_and_post_openrewrite_repair_rule(tmp_path: Path):
    source_repo = tmp_path / "src_repo"
    (source_repo / "src" / "main" / "java" / "com" / "ex" / "security").mkdir(parents=True)
    (source_repo / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.18</version>
    </parent>
    <groupId>com.ex</groupId>
    <artifactId>shiro-demo</artifactId>
    <version>1.0.0</version>
    <properties>
        <java.version>1.8</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.apache.shiro</groupId>
            <artifactId>shiro-all</artifactId>
            <version>1.10.0</version>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )
    (
        source_repo / "src/main/java/com/ex/security/RolesOrAuthorizationFilter.java"
    ).write_text(
        """package com.ex.security;

import javax.servlet.ServletRequest;
import org.apache.shiro.util.StringUtils;

public class RolesOrAuthorizationFilter {
    public boolean check(ServletRequest req, String role) {
        return req != null && StringUtils.hasText(role);
    }
}
""",
        encoding="utf-8",
    )
    (
        source_repo / "src/main/java/com/ex/security/GuavaHelperFilter.java"
    ).write_text(
        """package com.ex.security;

import javax.servlet.ServletRequest;
import com.google.common.collect.Lists;
import java.util.List;

public class GuavaHelperFilter {
    public List<String> roles(ServletRequest req) {
        return Lists.newArrayList("admin");
    }
}
""",
        encoding="utf-8",
    )

    repo = Repo.init(source_repo)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("baseline")

    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(source_repo), clean=True)
    repo_path = Path(report.repo_info.target_path)

    captured_prompts: dict[str, str] = {}

    def repair_llm(target: str, prompt: str) -> FilePatchResult:
        captured_prompts[target] = prompt
        if target.endswith("GuavaHelperFilter.java"):
            # Ensure OpenRewrite's narrow javax-only rule does NOT block Guava repair!
            assert "Migrate ONLY Jakarta EE javax.* imports" not in prompt
            return FilePatchResult(
                file_path=target,
                new_content="""package com.ex.security;

import jakarta.servlet.ServletRequest;
import java.util.List;

public class GuavaHelperFilter {
    public List<String> roles(ServletRequest req) {
        return List.of("admin");
    }
}
""",
                explanation="Replaced removed transitive Guava Lists with List.of",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/ex/security/GuavaHelperFilter.java",
                        line_number=4,
                        column=33,
                        message="package com.google.common.collect does not exist",
                    )
                ],
                raw_logs="[ERROR] package com.google.common.collect does not exist",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=0.8, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(repair_llm),
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    # RolesOrAuthorizationFilter was fixed 100% deterministically by OpenRewrite pass (zero LLM calls!)
    roles_text = (
        repo_path / "src/main/java/com/ex/security/RolesOrAuthorizationFilter.java"
    ).read_text(encoding="utf-8")
    assert "import jakarta.servlet.ServletRequest;" in roles_text
    assert "import org.apache.shiro.lang.util.StringUtils;" in roles_text
    assert "src/main/java/com/ex/security/RolesOrAuthorizationFilter.java" not in captured_prompts
    assert "src/main/java/com/ex/security/GuavaHelperFilter.java" in captured_prompts


def test_orchestrator_injects_multihop_unmodified_dao_hierarchy_and_defers_cascading_subclass_overrides(
    tmp_path: Path,
):
    root = tmp_path / "hib-hierarchy-src"
    (root / "src/main/java/com/example/entity").mkdir(parents=True)
    (root / "src/main/java/com/example/dao/impl").mkdir(parents=True)
    (root / "src/main/java/com/example/service/impl").mkdir(parents=True)
    (root / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>hib-hierarchy</artifactId>
    <version>1.0.0</version>
    <properties><java.version>11</java.version></properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )

    # Create 8 alphabetically-early @Entity classes (AEntity1..8) to verify they don't crowd out DAOs
    for idx in range(1, 9):
        (root / f"src/main/java/com/example/entity/AEntity{idx}.java").write_text(
            f"""package com.example.entity;
import javax.persistence.Entity;
@Entity
public class AEntity{idx} {{
    private Long id;
    public Long getId() {{ return id; }}
}}
""",
            encoding="utf-8",
        )

    # Large ISimpleHibernateDao (>1400 chars) with `distinct(Query)` near the bottom
    extra_methods = "\n".join(
        f"    List<T> findByCustomFilterStep{i}(String propertyName, Object value, int maxRows);"
        for i in range(1, 20)
    )
    (root / "src/main/java/com/example/dao/ISimpleHibernateDao.java").write_text(
        f"""package com.example.dao;

import java.util.List;
import org.hibernate.Query;
import org.hibernate.Session;

public interface ISimpleHibernateDao<T, ID> {{
    Session getSession();
    <X> List<X> find(String hql, Object... values);
{extra_methods}
    Query distinct(Query query);
}}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/IHibernateDao.java").write_text(
        """package com.example.dao;

public interface IHibernateDao<T, ID> extends ISimpleHibernateDao<T, ID> {
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/BaseDao.java").write_text(
        """package com.example.dao;

public interface BaseDao<T, ID> extends IHibernateDao<T, ID> {
}
""",
        encoding="utf-8",
    )
    # Unmodified marker DAO interface (no Hibernate imports!)
    (root / "src/main/java/com/example/dao/ZOrderDao.java").write_text(
        """package com.example.dao;

import com.example.entity.AEntity1;

public interface ZOrderDao extends BaseDao<AEntity1, Long> {
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/impl/SimpleHibernateDao.java").write_text(
        """package com.example.dao.impl;

import com.example.dao.ISimpleHibernateDao;
import java.util.List;
import org.hibernate.Query;
import org.hibernate.Session;
import org.hibernate.criterion.Criterion;

public class SimpleHibernateDao<T, ID> implements ISimpleHibernateDao<T, ID> {
    @Override public Session getSession() { return null; }
    @Override public <X> List<X> find(String hql, Object... values) { return List.of(); }
    public List<T> find(Criterion... criterions) { return List.of(); }
    @Override public Query distinct(Query query) { return query; }
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/impl/ZOrderDaoImpl.java").write_text(
        """package com.example.dao.impl;

import com.example.dao.ZOrderDao;
import com.example.entity.AEntity1;

public class ZOrderDaoImpl extends SimpleHibernateDao<AEntity1, Long> implements ZOrderDao {
}
""",
        encoding="utf-8",
    )

    entity_imports = "\n".join(
        f"import com.example.entity.AEntity{idx};" for idx in range(1, 9)
    )
    (root / "src/main/java/com/example/service/impl/OrderManagerImpl.java").write_text(
        f"""package com.example.service.impl;

import com.example.dao.ZOrderDao;
{entity_imports}
import java.util.List;
import org.hibernate.criterion.Restrictions;

public class OrderManagerImpl {{
    private ZOrderDao orderDao;

    public List<AEntity1> findActive() {{
        Restrictions.eq("status", "ACTIVE");
        return orderDao.find("from AEntity1");
    }}
}}
""",
        encoding="utf-8",
    )

    repo = Repo.init(root)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("baseline")

    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(root), clean=True)
    repo_path = Path(report.repo_info.target_path)

    captured_prompts: dict[str, list[str]] = {}

    def hib_llm(target: str, prompt: str) -> FilePatchResult:
        captured_prompts.setdefault(target, []).append(prompt)
        if target.endswith("ZOrderDaoImpl.java"):
            raise AssertionError("Leaf ZOrderDaoImpl should be deferred while SimpleHibernateDao is repaired")
        if target.endswith("ISimpleHibernateDao.java"):
            return FilePatchResult(
                file_path=target,
                new_content=f"""package com.example.dao;

import java.util.List;
import org.hibernate.query.Query;
import org.hibernate.Session;

public interface ISimpleHibernateDao<T, ID> {{
    Session getSession();
    <X> List<X> find(String hql, Object... values);
{extra_methods}
    Query distinct(Query query);
}}
""",
                explanation="Migrated ISimpleHibernateDao Query import",
            )
        if target.endswith("SimpleHibernateDao.java"):
            if "REPAIR attempt" in prompt:
                # Repair prompt must show untruncated `Query distinct(Query query);` from ISimpleHibernateDao!
                assert "Query distinct(Query query);" in prompt
                return FilePatchResult(
                    file_path=target,
                    new_content="""package com.example.dao.impl;

import com.example.dao.ISimpleHibernateDao;
import java.util.List;
import org.hibernate.query.Query;
import org.hibernate.Session;

public class SimpleHibernateDao<T, ID> implements ISimpleHibernateDao<T, ID> {
    @Override public Session getSession() { return null; }
    @Override public <X> List<X> find(String hql, Object... values) { return List.of(); }
    @Override public Query distinct(Query query) { return query; }
}
""",
                    explanation="Aligned distinct(Query) with ISimpleHibernateDao",
                )
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.dao.impl;

import com.example.dao.ISimpleHibernateDao;
import java.util.List;
import org.hibernate.query.Query;
import org.hibernate.Session;

public class SimpleHibernateDao<T, ID> implements ISimpleHibernateDao<T, ID> {
    @Override public Session getSession() { return null; }
    @Override public <X> List<X> find(String hql, Object... values) { return List.of(); }
    @Override public <X> Query<X> distinct(Query<X> query) { return query; }
}
""",
                explanation="Initial pass with Query<X> clash",
            )
        if target.endswith("OrderManagerImpl.java"):
            # Even on its first Pass 2 error, OrderManagerImpl sees unmodified ZOrderDao -> BaseDao -> IHibernateDao -> ISimpleHibernateDao!
            assert "ZOrderDao" in prompt
            assert "ISimpleHibernateDao" in prompt
            assert "Query distinct(Query query);" in prompt
            return FilePatchResult(
                file_path=target,
                new_content=f"""package com.example.service.impl;

import com.example.dao.ZOrderDao;
{entity_imports}
import java.util.List;

public class OrderManagerImpl {{
    private ZOrderDao orderDao;

    public List<AEntity1> findActive() {{
        return orderDao.find("from AEntity1 where status = ?1", "ACTIVE");
    }}
}}
""",
                explanation="Used HQL find() declared on ISimpleHibernateDao",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            # Round 1: Parent ISimpleHibernateDao & OrderManagerImpl fail; SimpleHibernateDao & ZOrderDaoImpl deferred
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/dao/ISimpleHibernateDao.java",
                        line_number=4,
                        column=1,
                        message="package org.hibernate does not have Query",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/dao/impl/SimpleHibernateDao.java",
                        line_number=9,
                        column=8,
                        message="com.example.dao.impl.SimpleHibernateDao is not abstract and does not override abstract method distinct(org.hibernate.query.Query) in com.example.dao.ISimpleHibernateDao",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/dao/impl/ZOrderDaoImpl.java",
                        line_number=6,
                        column=8,
                        message="com.example.dao.impl.ZOrderDaoImpl is not abstract and does not override abstract method distinct(org.hibernate.query.Query) in com.example.dao.ISimpleHibernateDao",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/service/impl/OrderManagerImpl.java",
                        line_number=11,
                        column=1,
                        message="package org.hibernate.criterion does not exist",
                    ),
                ],
                raw_logs="[ERROR] COMPILATION ERROR",
            ),
            # Round 2: SimpleHibernateDao has distinct(Query<X>) clash and ZOrderDaoImpl has cascading override error -> ZOrderDaoImpl deferred!
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/dao/impl/SimpleHibernateDao.java",
                        line_number=9,
                        column=8,
                        message="com.example.dao.impl.SimpleHibernateDao is not abstract and does not override abstract method distinct(org.hibernate.query.Query) in com.example.dao.ISimpleHibernateDao",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/dao/impl/SimpleHibernateDao.java",
                        line_number=12,
                        column=34,
                        message="name clash: distinct(org.hibernate.query.Query<X>) in com.example.dao.impl.SimpleHibernateDao and distinct(org.hibernate.query.Query) in com.example.dao.ISimpleHibernateDao have the same erasure, yet neither overrides the other",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/dao/impl/ZOrderDaoImpl.java",
                        line_number=6,
                        column=8,
                        message="com.example.dao.impl.ZOrderDaoImpl is not abstract and does not override abstract method distinct(org.hibernate.query.Query) in com.example.dao.ISimpleHibernateDao",
                    ),
                ],
                raw_logs="[ERROR] COMPILATION ERROR",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=0.8, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(hib_llm),
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
        retry_budget=3,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert fake_docker.calls == 3
    assert "src/main/java/com/example/dao/impl/ZOrderDaoImpl.java" not in captured_prompts
    assert len(captured_prompts["src/main/java/com/example/dao/impl/SimpleHibernateDao.java"]) == 1
    assert len(captured_prompts["src/main/java/com/example/service/impl/OrderManagerImpl.java"]) == 1
    by_path = {s.file_path: s for s in summary.file_statuses}
    assert by_path["src/main/java/com/example/dao/impl/ZOrderDaoImpl.java"].status == FileStatus.SKIPPED
    assert by_path["src/main/java/com/example/dao/impl/ZOrderDaoImpl.java"].errors_encountered == []
    assert all(s.status != FileStatus.FAILED for s in summary.file_statuses)


def test_build_coder_prompt_renders_error_line_snippets_and_predicate_mismatch_hints():
    from app.llm.prompts import build_coder_prompt

    src = (
        "package com.example.service;\n\n"
        "import jakarta.persistence.criteria.Predicate;\n\n"
        "public class GenericServiceImpl {\n"
        "    public Object findPage(Object page, Predicate... criterions) {\n"
        "        return baseDao.findPage(page, criterions);\n"
        "    }\n"
        "}\n"
    )
    prompt = build_coder_prompt(
        target_file_path="src/main/java/com/example/service/GenericServiceImpl.java",
        source_code=src,
        migration_rule="Migrate Hibernate 5 to 6",
        compiler_errors=[
            CompilerError(
                file_path="src/main/java/com/example/service/GenericServiceImpl.java",
                line_number=7,
                column=39,
                message="incompatible types: jakarta.persistence.criteria.Predicate[] cannot be converted to java.lang.String",
            )
        ],
    )
    assert "Exact failing source line(s):" in prompt
    assert ">    7:         return baseDao.findPage(page, criterions);" in prompt
    assert "Note on Criteria/Predicate argument type mismatch:" in prompt
    assert "Arrays.asList(...)" in prompt


def test_orchestrator_api_sanity_retry_error_line_receiver_and_repeat_escalation(
    tmp_path: Path,
):
    root = tmp_path / "service-receiver-src"
    (root / "src/main/java/com/example/entity").mkdir(parents=True)
    (root / "src/main/java/com/example/dao").mkdir(parents=True)
    (root / "src/main/java/com/example/service").mkdir(parents=True)
    (root / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>service-receiver</artifactId>
    <version>1.0.0</version>
    <properties><java.version>11</java.version></properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )

    for idx in range(1, 10):
        (root / f"src/main/java/com/example/entity/AItem{idx}.java").write_text(
            f"""package com.example.entity;
import javax.persistence.Entity;
@Entity
public class AItem{idx} {{
    private Long id;
    public Long getId() {{ return id; }}
}}
""",
            encoding="utf-8",
        )

    (root / "src/main/java/com/example/dao/ISimpleHibernateDao.java").write_text(
        """package com.example.dao;

import java.util.List;
import org.hibernate.Session;

public interface ISimpleHibernateDao<T, ID> {
    Session getSession();
    <X> List<X> find(String hql, Object... values);
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/IHibernateDao.java").write_text(
        """package com.example.dao;

import java.util.List;
import org.hibernate.criterion.Criterion;

public interface IHibernateDao<T, ID> extends ISimpleHibernateDao<T, ID> {
    List<T> findByPredicates(List<jakarta.persistence.criteria.Predicate> predicates);
    List<T> findByCriteria(Criterion... criterions);
}
""",
        encoding="utf-8",
    )
    # Unmodified interface extending IHibernateDao
    (root / "src/main/java/com/example/dao/ZBaseDao.java").write_text(
        """package com.example.dao;

public interface ZBaseDao<T, ID> extends IHibernateDao<T, ID> {
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/service/BaseService.java").write_text(
        """package com.example.service;

import java.util.List;
import org.hibernate.criterion.Criterion;

public interface BaseService<T, ID> {
    List<T> findList(Criterion... criterions);
}
""",
        encoding="utf-8",
    )

    entity_imports = "\n".join(
        f"import com.example.entity.AItem{idx};" for idx in range(1, 10)
    )
    (root / "src/main/java/com/example/service/BaseServiceImpl.java").write_text(
        f"""package com.example.service;

import com.example.dao.ZBaseDao;
{entity_imports}
import java.util.List;
import org.hibernate.criterion.Criterion;

public abstract class BaseServiceImpl<T, ID> implements BaseService<T, ID> {{
    protected ZBaseDao<T, ID> baseDao;

    @Override
    public List<T> findList(Criterion... criterions) {{
        return baseDao.findByCriteria(criterions);
    }}
}}
""",
        encoding="utf-8",
    )

    repo = Repo.init(root)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("baseline")

    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(root), clean=True)
    repo_path = Path(report.repo_info.target_path)

    captured_prompts: dict[str, list[str]] = {}

    def llm_flow(target: str, prompt: str) -> FilePatchResult:
        captured_prompts.setdefault(target, []).append(prompt)
        if target.endswith("IHibernateDao.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.dao;

import jakarta.persistence.criteria.Predicate;
import java.util.List;

public interface IHibernateDao<T, ID> extends ISimpleHibernateDao<T, ID> {
    List<T> findByPredicates(List<Predicate> predicates);
}
""",
                explanation="Removed Criterion overload in favor of List<Predicate>",
            )
        if target.endswith("BaseService.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.service;

import jakarta.persistence.criteria.Predicate;
import java.util.List;

public interface BaseService<T, ID> {
    List<T> findList(Predicate... criterions);
}
""",
                explanation="Updated BaseService to Predicate...",
            )
        if target.endswith("BaseServiceImpl.java"):
            calls = len(captured_prompts[target])
            if calls == 1:
                # Round 1: error-line receiver resolution surfaces ZBaseDao -> IHibernateDao -> ISimpleHibernateDao,
                # and triggers API SANITY RETRY by calling cb.root(...)
                assert "Exact failing source line(s):" in prompt
                assert "IHibernateDao" in prompt
                assert "findByPredicates(List<Predicate> predicates)" in prompt
                assert "Note on Criteria/Predicate argument type mismatch:" in prompt
                assert "REPEAT REPAIR FAILURE" not in prompt
                return FilePatchResult(
                    file_path=target,
                    new_content=f"""package com.example.service;

import com.example.dao.ZBaseDao;
{entity_imports}
import jakarta.persistence.criteria.CriteriaBuilder;
import jakarta.persistence.criteria.Predicate;
import java.util.List;

public abstract class BaseServiceImpl<T, ID> implements BaseService<T, ID> {{
    protected ZBaseDao<T, ID> baseDao;

    @Override
    public List<T> findList(Predicate... criterions) {{
        CriteriaBuilder cb = baseDao.getSession().getCriteriaBuilder();
        cb.root(Object.class);
        return (List<T>) baseDao.find(criterions);
    }}
}}
""",
                    explanation="Bad cb.root hallucination in Round 1",
                )
            if calls == 2:
                # Response to API SANITY RETRY: fixed cb.root, still passes Predicate[] to find()
                assert "CRITICAL API CORRECTION" in prompt
                return FilePatchResult(
                    file_path=target,
                    new_content=f"""package com.example.service;

import com.example.dao.ZBaseDao;
{entity_imports}
import jakarta.persistence.criteria.Predicate;
import java.util.List;

public abstract class BaseServiceImpl<T, ID> implements BaseService<T, ID> {{
    protected ZBaseDao<T, ID> baseDao;

    @Override
    public List<T> findList(Predicate... criterions) {{
        return (List<T>) baseDao.find(criterions);
    }}
}}
""",
                    explanation="Fixed cb.root, kept bad find(criterions) call",
                )
            # Round 2 repair: same compiler error repeated -> REPEAT REPAIR FAILURE escalation fires!
            assert "REPEAT REPAIR FAILURE" in prompt
            return FilePatchResult(
                file_path=target,
                new_content=f"""package com.example.service;

import com.example.dao.ZBaseDao;
{entity_imports}
import jakarta.persistence.criteria.Predicate;
import java.util.Arrays;
import java.util.List;

public abstract class BaseServiceImpl<T, ID> implements BaseService<T, ID> {{
    protected ZBaseDao<T, ID> baseDao;

    @Override
    public List<T> findList(Predicate... criterions) {{
        return baseDao.findByPredicates(Arrays.asList(criterions));
    }}
}}
""",
                explanation="Switched to IHibernateDao.findByPredicates(Arrays.asList(criterions))",
            )
        raise AssertionError(f"Unexpected LLM target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            # Round 1: Initial Hibernate 5 compiler errors on interfaces + BaseServiceImpl
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/dao/IHibernateDao.java",
                        line_number=4,
                        column=1,
                        message="package org.hibernate.criterion does not exist",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/service/BaseService.java",
                        line_number=4,
                        column=1,
                        message="package org.hibernate.criterion does not exist",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/service/BaseServiceImpl.java",
                        line_number=19,
                        column=30,
                        message="incompatible types: jakarta.persistence.criteria.Predicate[] cannot be converted to java.lang.String",
                    ),
                ],
                raw_logs="[ERROR] BaseServiceImpl.java:[19,30] incompatible types",
            ),
            # Round 2: Same compiler error repeated on BaseServiceImpl -> triggers REPEAT REPAIR FAILURE
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/service/BaseServiceImpl.java",
                        line_number=19,
                        column=25,
                        message="incompatible types: jakarta.persistence.criteria.Predicate[] cannot be converted to java.lang.String",
                    )
                ],
                raw_logs="[ERROR] BaseServiceImpl.java:[19,25] incompatible types",
            ),
            BuildResult(
                success=True, exit_code=0, duration_seconds=0.8, raw_logs="[INFO] BUILD SUCCESS"
            ),
        ]
    )

    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(llm_flow),
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
        retry_budget=3,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert fake_docker.calls == 3
    assert len(captured_prompts["src/main/java/com/example/service/BaseServiceImpl.java"]) == 3


def test_api_sanity_catches_inline_hibernate_5_fqcn_and_pipeline_flags_transitive_guava(
    tmp_path: Path,
):
    root = tmp_path / "repo-guava"
    (root / "src/main/java/com/example/util").mkdir(parents=True)
    (root / "src/main/java/com/example/dao").mkdir(parents=True)

    pom_template = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>2.5.4</version>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>guava-demo</artifactId>
  <version>1.0.0</version>
  <properties><java.version>1.8</java.version></properties>
  <dependencies>
    <dependency>
      <groupId>io.springfox</groupId>
      <artifactId>springfox-boot-starter</artifactId>
      <version>3.0.0</version>
    </dependency>
    {extra_deps}
  </dependencies>
</project>
"""
    (root / "pom.xml").write_text(pom_template.format(extra_deps=""), encoding="utf-8")
    (root / "src/main/java/com/example/util/PageHelper.java").write_text(
        """package com.example.util;
import com.google.common.collect.Lists;
import java.util.List;
public class PageHelper {
    public List<String> makeList() { return Lists.newArrayList(); }
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/ItemDao.java").write_text(
        """package com.example.dao;
import java.util.List;
import org.hibernate.criterion.Criterion;
public interface ItemDao<T> {
    List<T> find(Criterion... criterions);
}
""",
        encoding="utf-8",
    )

    repo = Repo.init(root)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("baseline")

    report = IngestionPipeline(
        workspace_root=tmp_path / "ws1", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(root), clean=True)

    targets_by_path = {t.file_path: t for t in report.files_to_migrate}
    assert "src/main/java/com/example/util/PageHelper.java" in targets_by_path
    assert "guava-transitive-removal" in targets_by_path["src/main/java/com/example/util/PageHelper.java"].matched_rules

    # Explicit com.google.guava:guava in pom.xml must NOT flag PageHelper.java for removal
    (root / "pom.xml").write_text(
        pom_template.format(
            extra_deps="<dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId><version>32.1.3-jre</version></dependency>"
        ),
        encoding="utf-8",
    )
    repo.git.add(A=True)
    repo.index.commit("add explicit guava")
    report_with_guava = IngestionPipeline(
        workspace_root=tmp_path / "ws2", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(root), clean=True)
    assert "src/main/java/com/example/util/PageHelper.java" not in {
        t.file_path for t in report_with_guava.files_to_migrate
    }

    # Now run orchestrator on report (without explicit Guava) and test inline org.hibernate.criterion.* rejection in Pass 2
    repo_path = Path(report.repo_info.target_path)
    dao_prompts: list[str] = []

    def llm_flow(target: str, prompt: str) -> FilePatchResult:
        if target.endswith("PageHelper.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.util;
import java.util.ArrayList;
import java.util.List;
public class PageHelper {
    public List<String> makeList() { return new ArrayList<>(); }
}
""",
                explanation="Replaced Lists.newArrayList with new ArrayList<>()",
            )
        if target.endswith("ItemDao.java"):
            dao_prompts.append(prompt)
            if len(dao_prompts) == 1:
                # Drops import but sneaks inline org.hibernate.criterion.Criterion!
                return FilePatchResult(
                    file_path=target,
                    new_content="""package com.example.dao;
import java.util.List;
public interface ItemDao<T> {
    List<T> find(org.hibernate.criterion.Criterion... criterions);
}
""",
                    explanation="Left inline Hibernate 5 FQCN",
                )
            assert "CRITICAL API CORRECTION" in prompt
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.dao;
import jakarta.persistence.criteria.Predicate;
import java.util.List;
public interface ItemDao<T> {
    List<T> find(Predicate... predicates);
}
""",
                explanation="Replaced inline Hibernate 5 FQCN with Jakarta Predicate",
            )
        raise AssertionError(f"Unexpected target: {target}")

    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.5,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/util/PageHelper.java",
                        line_number=2,
                        column=1,
                        message="package com.google.common.collect does not exist",
                    ),
                    CompilerError(
                        file_path="src/main/java/com/example/dao/ItemDao.java",
                        line_number=3,
                        column=1,
                        message="package org.hibernate.criterion does not exist",
                    ),
                ],
                raw_logs="[ERROR] compilation failed",
            ),
            BuildResult(success=True, exit_code=0, duration_seconds=0.5, raw_logs="BUILD SUCCESS"),
        ]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=MockLLMProvider(llm_flow),
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert len(dao_prompts) == 2


def test_orchestrator_pass1_is_openrewrite_only_when_build_succeeds(
    fixtures_dir: Path, tmp_path: Path
):
    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(fixtures_dir / "spring-boot-2.7-basic"), clean=True)
    repo_path = Path(report.repo_info.target_path)

    mock_llm = MockLLMProvider(
        lambda t, p: (_ for _ in ()).throw(AssertionError(f"LLM should not be called in Pass 1: {t}"))
    )
    fake_docker = FakeDockerRunner(
        results=[BuildResult(success=True, exit_code=0, duration_seconds=0.5, raw_logs="[INFO] BUILD SUCCESS")]
    )
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=mock_llm,
        docker_runner=fake_docker,
        use_openrewrite=True,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.openrewrite_used is True
    assert mock_llm.calls == []
    assert fake_docker.calls == 1
    assert all(s.applied_by == "openrewrite" for s in summary.file_statuses)


def test_build_coder_prompt_renders_springdoc_and_hibernate_tuple_hints():
    from app.llm.prompts import build_coder_prompt

    prompt = build_coder_prompt(
        target_file_path="src/main/java/com/example/web/DemoController.java",
        source_code='@Tag(tags = "Demo")\npublic class DemoController { void f(@Hidden Object x) {} }\n',
        migration_rule="Springfox Swagger 2 -> SpringDoc OpenAPI 3",
        compiler_errors=[
            CompilerError(
                file_path="src/main/java/com/example/web/DemoController.java",
                line_number=1,
                column=6,
                message="cannot find symbol: method tags() location: @interface io.swagger.v3.oas.annotations.tags.Tag",
            ),
            CompilerError(
                file_path="src/main/java/com/example/web/DemoController.java",
                line_number=2,
                column=38,
                message="annotation type not applicable to this kind of declaration",
            ),
            CompilerError(
                file_path="src/main/java/com/example/service/BaseServiceImpl.java",
                line_number=10,
                column=20,
                message="incompatible types: java.util.List<java.lang.Object[]> cannot be converted to java.util.List<T>",
            ),
        ],
    )
    assert "Note on SpringDoc / OpenAPI 3 annotations:" in prompt
    assert "@io.swagger.v3.oas.annotations.Parameter(hidden = true)" in prompt
    assert "Note on Hibernate 6 Query generic return type mismatch:" in prompt


def test_orchestrator_surfaces_unattempted_targets_on_javac_truncation_and_orders_ancestors_first(
    tmp_path: Path,
):
    root = tmp_path / "trunc-order-src"
    (root / "src/main/java/com/example/dao").mkdir(parents=True)
    (root / "src/main/java/com/example/service").mkdir(parents=True)
    (root / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>trunc-order</artifactId>
    <version>1.0.0</version>
    <properties><java.version>11</java.version></properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )

    (root / "src/main/java/com/example/dao/IHibernateDao.java").write_text(
        """package com.example.dao;

import java.util.List;
import org.hibernate.criterion.Criterion;

public interface IHibernateDao<T> {
    List<T> findByCriteria(Criterion... criterions);
}
""",
        encoding="utf-8",
    )
    (root / "src/main/java/com/example/dao/HibernateDao.java").write_text(
        """package com.example.dao;

import java.util.List;
import org.hibernate.criterion.Criterion;

public class HibernateDao<T> implements IHibernateDao<T> {
    @Override
    public List<T> findByCriteria(Criterion... criterions) {
        return List.of();
    }
}
""",
        encoding="utf-8",
    )
    # Extra target NOT in the first 100 javac errors
    (root / "src/main/java/com/example/service/ExtraServiceImpl.java").write_text(
        """package com.example.service;

import org.hibernate.criterion.Criterion;

public class ExtraServiceImpl {
    public int count(Criterion c) { return 0; }
}
""",
        encoding="utf-8",
    )

    repo = Repo.init(root)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("baseline")

    report = IngestionPipeline(
        workspace_root=tmp_path / "ws", isolate_local=True, target_spring_boot="3.2.5"
    ).run(str(root), clean=True)
    repo_path = Path(report.repo_info.target_path)

    captured_prompts: dict[str, str] = {}

    def llm_flow(target: str, prompt: str) -> FilePatchResult:
        captured_prompts[target] = prompt
        if target.endswith("IHibernateDao.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.dao;

import jakarta.persistence.criteria.Predicate;
import java.util.List;

public interface IHibernateDao<T> {
    List<T> findByPredicates(List<Predicate> predicates);
}
""",
                explanation="Migrated IHibernateDao to List<Predicate>",
            )
        if target.endswith("HibernateDao.java"):
            # Because IHibernateDao ran first in the same round, HibernateDao sees the NEW signature!
            assert "findByPredicates(List<Predicate> predicates);" in prompt
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.dao;

import jakarta.persistence.criteria.Predicate;
import java.util.List;

public class HibernateDao<T> implements IHibernateDao<T> {
    @Override
    public List<T> findByPredicates(List<Predicate> predicates) {
        return List.of();
    }
}
""",
                explanation="Aligned HibernateDao with updated IHibernateDao",
            )
        if target.endswith("ExtraServiceImpl.java"):
            return FilePatchResult(
                file_path=target,
                new_content="""package com.example.service;

import jakarta.persistence.criteria.Predicate;

public class ExtraServiceImpl {
    public int count(Predicate c) { return 0; }
}
""",
                explanation="Migrated ExtraServiceImpl surfaced from truncated javac run",
            )
        raise AssertionError(f"Unexpected target: {target}")

    # 80+ errors on HibernateDao and IHibernateDao only (simulating javac -Xmaxerrs 100 hiding ExtraServiceImpl)
    many_errors = [
        CompilerError(
            file_path="src/main/java/com/example/dao/HibernateDao.java",
            line_number=7,
            column=1,
            message=f"error #{i}: cannot find symbol class Criterion",
        )
        for i in range(79)
    ] + [
        CompilerError(
            file_path="src/main/java/com/example/dao/IHibernateDao.java",
            line_number=4,
            column=1,
            message="package org.hibernate.criterion does not exist",
        )
    ]
    fake_docker = FakeDockerRunner(
        results=[
            BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=1.0,
                compiler_errors=many_errors,
                raw_logs="[ERROR] javac truncated at 100 errors",
            ),
            BuildResult(
                success=True,
                exit_code=0,
                duration_seconds=0.8,
                raw_logs="[INFO] BUILD SUCCESS",
            ),
        ]
    )

    mock_llm = MockLLMProvider(llm_flow)
    summary = MigrationOrchestrator(
        repo_path=repo_path,
        llm_provider=mock_llm,
        docker_runner=fake_docker,
        use_openrewrite=True,
        openrewrite_mode="deterministic",
        retry_budget=3,
    ).run(report)

    assert summary.final_state == WorkflowState.COMPLETED
    assert summary.integrity_passed is True
    assert fake_docker.calls == 2
    assert mock_llm.calls.index("src/main/java/com/example/dao/IHibernateDao.java") < mock_llm.calls.index(
        "src/main/java/com/example/dao/HibernateDao.java"
    )
    assert "src/main/java/com/example/service/ExtraServiceImpl.java" in mock_llm.calls

