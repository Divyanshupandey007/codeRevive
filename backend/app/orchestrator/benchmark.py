import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.ingestion.pipeline import IngestionPipeline
from app.llm.base import LLMProvider
from app.models.agent import FilePatchResult
from app.models.execution import BuildResult, CompilerError, TestFailure
from app.models.orchestrator import WorkflowState
from app.orchestrator.workflow import MigrationOrchestrator

T = TypeVar("T", bound=BaseModel)


class _BenchmarkScriptedLLM(LLMProvider):
    """Deterministic LLM stand-in for offline/CI benchmark runs."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.calls = 0

    def generate_text(
        self, prompt: str, system_instruction: Optional[str] = None, temperature: float = 0.2
    ) -> str:
        return ""

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
    ) -> T:
        self.calls += 1
        target = ""
        for line in prompt.splitlines():
            if line.startswith("Target file: "):
                target = line.removeprefix("Target file: ").strip()
                break

        content = (self.repo_path / target).read_text(encoding="utf-8")

        if target.endswith("SecurityConfig.java"):
            updated = """package com.example.sec.config;

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
        http
            .csrf(csrf -> csrf.disable())
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
"""
        elif target.endswith("application.yml"):
            updated = (
                content.replace("  redis:", "  data:\n    redis:")
                .replace("    host: localhost", "      host: localhost")
                .replace("    port: 6379", "      port: 6379")
                .replace("      use-new-id-generator-mappings: true\n", "")
            )
        elif target.endswith("layout.html"):
            updated = content.replace(
                "${#request.getAttribute('javax.servlet.error.status_code') ge 400}",
                "${status != null and status ge 400}",
            )
        else:
            updated = content

        return FilePatchResult(
            file_path=target,
            new_content=updated,
            explanation=f"Migrated {target} for Spring Boot 3 compatibility",
        )


class _SimulatedSandboxRunner:
    """Content-driven verification simulator for offline CI benchmarks."""

    def run_tests(self, repo_path: Path, maven_args=None) -> BuildResult:
        sec_cfg = repo_path / "src/main/java/com/example/sec/config/SecurityConfig.java"
        if sec_cfg.is_file() and "WebSecurityConfigurerAdapter" in sec_cfg.read_text(encoding="utf-8"):
            return BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.1,
                compiler_errors=[
                    CompilerError(
                        file_path="src/main/java/com/example/sec/config/SecurityConfig.java",
                        line_number=9,
                        column=1,
                        message="cannot find symbol: class WebSecurityConfigurerAdapter",
                    )
                ],
                raw_logs="[ERROR] SecurityConfig.java:[9,1] cannot find symbol: class WebSecurityConfigurerAdapter",
            )

        layout = repo_path / "src/main/resources/templates/layout.html"
        if layout.is_file() and "#request" in layout.read_text(encoding="utf-8"):
            return BuildResult(
                success=False,
                exit_code=1,
                duration_seconds=0.1,
                failed_tests=[
                    TestFailure(
                        test_class="com.example.web.HomeControllerTest",
                        test_method="rendersLayout",
                        failure_message="TemplateProcessingException: Expression [#request] is not valid in Thymeleaf 3.1",
                    )
                ],
                raw_logs="[ERROR] Caused by: org.thymeleaf.exceptions.TemplateProcessingException: The '#request' object is no longer available in Thymeleaf 3.1",
            )

        return BuildResult(
            success=True,
            exit_code=0,
            duration_seconds=0.1,
            raw_logs="[INFO] Simulated sandbox verification passed",
        )


def run_benchmark(use_live_llm: bool = False, use_docker: bool = False) -> Dict:
    fixtures_root = backend_root / "tests" / "fixtures"
    fixture_names = [
        "spring-boot-2.7-basic",
        "spring-boot-2.7-security",
        "spring-boot-2.7-thymeleaf",
    ]

    results: List[Dict] = []
    sample_audit_report: Optional[Dict] = None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_ws = Path(tmpdir)
        for name in fixture_names:
            fixture_path = fixtures_root / name
            report = IngestionPipeline(
                workspace_root=tmp_ws / name,
                isolate_local=True,
                target_spring_boot="3.2.5",
            ).run(str(fixture_path), clean=True)

            repo_path = Path(report.repo_info.target_path)
            llm = None if use_live_llm else _BenchmarkScriptedLLM(repo_path)
            docker_runner = None if use_docker else _SimulatedSandboxRunner()

            orchestrator = MigrationOrchestrator(
                repo_path=repo_path,
                llm_provider=llm,
                docker_runner=docker_runner,
                use_openrewrite=True,
            )
            summary = orchestrator.run(report)

            llm_calls = 0 if use_live_llm else llm.calls
            openrewrite_handled = sum(
                1 for s in summary.file_statuses if s.applied_by == "openrewrite"
            )
            llm_handled = sum(
                1 for s in summary.file_statuses if s.applied_by == "llm"
            )

            entry = {
                "project": name,
                "source_spring_boot": report.pom.spring_boot_version,
                "target_spring_boot": summary.target_spring_boot_version,
                "final_state": summary.final_state.value,
                "build_success": summary.final_state == WorkflowState.COMPLETED,
                "integrity_passed": summary.integrity_passed,
                "baseline_tests": report.baseline_test_count,
                "targets_total": len(report.files_to_migrate),
                "files_migrated": summary.total_files_migrated,
                "openrewrite_handled": openrewrite_handled,
                "llm_handled": llm_handled,
                "llm_calls": llm_calls,
                "total_retries": summary.total_retries,
                "duration_seconds": summary.total_duration_seconds,
            }
            results.append(entry)

            if name == "spring-boot-2.7-security":
                sample_audit_report = summary.model_dump()

    total = len(results)
    passed = sum(1 for r in results if r["build_success"] and r["integrity_passed"])
    return {
        "evaluation_mode": {
            "llm": "live" if use_live_llm else "scripted_deterministic_mock",
            "sandbox": "docker_maven" if use_docker else "simulated_build_with_real_ast_integrity",
        },
        "summary": {
            "projects_evaluated": total,
            "projects_succeeded": passed,
            "success_rate": round(passed / total, 4) if total else 0.0,
            "total_targets": sum(r["targets_total"] for r in results),
            "total_openrewrite_handled": sum(r["openrewrite_handled"] for r in results),
            "total_llm_handled": sum(r["llm_handled"] for r in results),
        },
        "projects": results,
        "sample_audit_report": sample_audit_report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CodeRevive migration evaluation benchmark.")
    parser.add_argument("--live-llm", action="store_true", help="Use configured live LLM API instead of deterministic mock.")
    parser.add_argument("--docker", action="store_true", help="Use real DockerRunner instead of simulated build.")
    args = parser.parse_args()

    output = run_benchmark(use_live_llm=args.live_llm, use_docker=args.docker)
    bench_dir = backend_root.parent / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)

    sample = output.pop("sample_audit_report", None)
    (bench_dir / "latest_benchmark.json").write_text(
        json.dumps(output, indent=2), encoding="utf-8"
    )
    if sample is not None:
        (bench_dir / "sample_audit_report.json").write_text(
            json.dumps(sample, indent=2), encoding="utf-8"
        )

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
