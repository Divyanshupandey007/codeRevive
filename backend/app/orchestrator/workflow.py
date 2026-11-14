from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.ingestion.migration_rules import SPRING_BOOT_3_RULES
from app.guardrails.post_execution import PostExecutionIntegrityResult, run_post_execution_integrity
from app.guardrails.pre_execution import SecurityViolationError
from app.ingestion.ast_parser import JavaAstParser
from app.ingestion.patch_applicator import apply_patch, commit_patch, rollback_patch
from app.llm.base import LLMProvider, LLMProviderError
from app.llm.factory import LLMFactory
from app.llm.prompts import CODER_AGENT_SYSTEM_INSTRUCTION, build_coder_prompt
from app.models.agent import FilePatchResult
from app.models.execution import BuildResult, CompilerError, TestFailure
from app.models.manifest import MigrationTarget, ProjectIngestionReport
from app.models.orchestrator import FileMigrationStatus, FileStatus, MigrationRunSummary, WorkflowState
from app.sandbox.docker_runner import ContainerExecutionError, DockerRunner


POM_MIGRATION_RULE=(
    "Upgrade this Maven pom.xml for a Spring Boot 2 -> 3 / Java 17 migration: "
    "(1) change the spring-boot-starter-parent <parent> version from 2.7.0 to "
    "3.0.2 (or the latest available 3.0.x release), (2) set the <java.version> "
    "property to 17, (3) update the mybatis-spring-boot-starter dependency "
    "version from 2.2.2 to 3.0.1 (the Spring Boot 3-compatible release), and also DELETE the "
    "<thymeleaf.version>3.0.14.RELEASE</thymeleaf.version> property override so the "
    "Boot 3 BOM manages Thymeleaf 3.0.15.RELEASE (thymeleaf-spring6 3.0.14.RELEASE does "
    "not exist on Maven Central and fails dependency resolution). "
    "(4) remove the <repositories>/<pluginRepositories> entries pointing at https://repo.spring.io/... (they return 401 and break dependency resolution; all"
    "needed artifacts are on Maven Central), (5) rename thymeleaf-extras-springsecurity5 to thymeleaf-extras-springsecurity6 (the Boot 3 artifact)," 
    "(6) remove the <maven.compiler.source/target>1.8</...> properties (or set them to 17) since Boot 3 requires Java 17 bytecode, "
    "(7) update the <selenide.version> property from 6.6.1 to 6.19.1 and DELETE the <selenium.version>4.1.0</selenium.version> property override so the Boot 3 BOM manages Selenium. "
    "Do not change any other dependency, plugin, or property." 
)

POM_COMMIT_SUMMARY="spring-boot 2.7.0 -> 3.0.2, java -> 17, mybatis-spring-boot-starter -> 3.0.1, selenide -> 6.19.1"

JAVAX_VALIDATION_MIGRATION_RULE=(
    "Migrate all javax.validation.* imports and annotations in this file to "
    "their jakarta.validation.* equivalents. Do not change anything else "
    "about this file."
)

JAVAX_VALIDATION_COMMIT_SUMMARY="javax.validation -> jakarta.validation"

GENERIC_SPRING_BOOT_3_RULE=(
    "This file needs to be updated for Spring Boot 3 compatibility. "
    "Examine the file for any Spring Boot 2.x-era APIs, deprecated classes, "
    "or removed patterns (javax.* -> jakarta.*, removed adapter superclasses, "
    "renamed packages) and apply the necessary changes. "
    "Do not change any business logic."
)

LAYOUT_HTML_MIGRATION_RULE=(
    "In Thymeleaf 3.1 and Spring Boot 3, the #request object was removed from Thymeleaf expressions, "
    "and javax.servlet was renamed to jakarta.servlet. "
    "Replace <body th:with=\"isErrorResponse=${#request.getAttribute('javax.servlet.error.status_code') ge 400}\"> "
    "with <body th:with=\"isErrorResponse=${status != null and status ge 400}\">. "
    "Do not change any other part of the file."
)

LAYOUT_HTML_COMMIT_SUMMARY="javax.servlet -> jakarta.servlet, Thymeleaf 3.1 #request removal"

def _migration_spec_for(target_file_path:str,matched_rules:List[str])->Tuple[str,str]:
    if Path(target_file_path).name=="pom.xml":
        return POM_MIGRATION_RULE,POM_COMMIT_SUMMARY

    if "javax-in-resource" in matched_rules or Path(target_file_path).suffix in (".html",".xhtml"):
        return LAYOUT_HTML_MIGRATION_RULE,LAYOUT_HTML_COMMIT_SUMMARY

    rules=[r for r in SPRING_BOOT_3_RULES if r.id in matched_rules]
    if not rules:
        return GENERIC_SPRING_BOOT_3_RULE, "Spring Boot 3 migration"

    instruction="\n\n".join(r.migration_instruction for r in rules)
    summary="; ".join(r.commit_summary for r in rules)
    return instruction,summary

def _diagnostics_summary(compiler_errors:List[CompilerError],failed_tests:List[TestFailure])->List[str]:
    summary=[f"{e.file_path}:{e.line_number}:{e.column} {e.message}" if e.line_number is not None else f"{e.file_path or '(unattributed)'}" for e in compiler_errors]
    summary+=[f"{t.test_class}:{t.test_method}: {t.failure_message}" for t in failed_tests]
    return summary

def _group_diagnostics_by_target(
        compiler_errors:List[CompilerError],
        failed_tests:List[TestFailure],
        target_file_paths:Set[str],
        identifier_to_file:Dict[str,str]
)->Dict[str,Tuple[List[CompilerError],List[TestFailure]]]:
    grouped:Dict[str,Tuple[List[CompilerError],List[TestFailure]]]={}

    def _bucket(file_path:str)->Tuple[List[CompilerError],List[TestFailure]]:
        return grouped.setdefault(file_path,([],[]))

    for error in compiler_errors:
        if error.file_path and error.file_path in target_file_paths:
            _bucket(error.file_path)[0].append(error)

    for failure in failed_tests:
        matched_file=None
        for identifier,file_path in identifier_to_file.items():
            if identifier==failure.test_class or identifier.endswith(f".{failure.test_class}"):
                matched_file=file_path
                break

        if matched_file and matched_file in target_file_paths:
            _bucket(matched_file)[1].append(failure)

    return grouped

@dataclass
class _FileWorkItem:
    target:MigrationTarget
    migration_rule:str
    commit_summary:str
    attempts:int=0
    errors_encountered:List[str]=field(default_factory=list)
    skipped:bool=False

class MigrationOrchestrator:
    def __init__(
            self,
            repo_path:Path,
            llm_provider:Optional[LLMProvider]=None,
            docker_runner:Optional[DockerRunner]=None,
            retry_budget:int=3,
            maven_args:Optional[List[str]]=None
    ):
        self.repo_path=Path(repo_path)
        self.llm_provider=llm_provider or LLMFactory.create()
        self.docker_runner=docker_runner or DockerRunner()
        self.retry_budget=retry_budget
        self.maven_args=maven_args
        self.state=WorkflowState.INITIALIZING

    def _transition(self,new_state:WorkflowState)->None:
        print(f"[orchestrator] {self.state.value} -> {new_state.value}")
        self.state=new_state

    def generate_patch(
            self,
            target_file_path:str,
            source_code:str,
            migration_rule:str,
            compiler_errors:List[CompilerError],
            failed_tests:List[TestFailure]
    )->FilePatchResult:
        self._transition(WorkflowState.GENERATING_PATCH)
        prompt=build_coder_prompt(
            target_file_path=target_file_path,
            source_code=source_code,
            migration_rule=migration_rule,
            compiler_errors=compiler_errors or None,
            failed_tests=failed_tests or None
        )
        return self.llm_provider.generate_structured(
            prompt=prompt,
            response_schema=FilePatchResult,
            system_instruction=CODER_AGENT_SYSTEM_INSTRUCTION
        )

    def _verify_in_sandbox(self):
        self._transition(WorkflowState.VERIFYING_SANDBOX)
        return self.docker_runner.run_tests(self.repo_path,self.maven_args)

    def _rollback_all(self)->None:
        from git import Repo
        repo=Repo(self.repo_path)
        repo.git.reset("--hard")
        repo.git.clean("-fd")

    def _current_head_sha(self)->Optional[str]:
        try:
            from git import Repo
            return Repo(self.repo_path).head.commit.hexsha
        except Exception:
            return None

    """Pass 1 - Synchronized Transformation (no Docker):
        Every target file (pom.xml first, then each javax.validation user)
        gets exactly one LLM pass with no diagnostics, so pom.xml and every
        Java file move to the new namespace together."""
    def _pass1_transform(self,target:MigrationTarget)->_FileWorkItem:
        self._transition(WorkflowState.MIGRATING_FILE)
        migration_rule,commit_summary=_migration_spec_for(target.file_path,target.matched_rules)
        record=_FileWorkItem(target=target,migration_rule=migration_rule,commit_summary=commit_summary)
        target_file=self.repo_path/target.file_path
        if not target_file.is_file():
            record.skipped=True
            record.errors_encountered=[f"File not found in workspace: {target_file}"]
            print(f" [SKIP] {target.file_path}: not found in workspace")
            return record

        record.attempts=1
        source_code=target_file.read_text(encoding="utf-8")

        try:
            patch=self.generate_patch(target.file_path,source_code,migration_rule,[],[])
        except LLMProviderError as exc:
            record.errors_encountered=[f"LLM generation failed: {exc}"]
            print(f" [PASS1 REJECTED] {target.file_path}: {exc}")
            return record

        try:
            apply_patch(self.repo_path,patch)
        except SecurityViolationError as exc:
            record.errors_encountered=[f"Guardrail rejected path: {exc}"]
            print(f" [PASS1 REJECTED] {target.file_path}: {exc}")
            return record

        print(f" [TRANSFORMED] {target.file_path} ({commit_summary})")
        return record

    """Pass 2 - Verification & Targeted Self-Repair (Docker sandbox):
        Run the real build once. If it's green, commit everything and run
        the post-execution integrity guardrails. If it's red, group the
        diagnostics by which target file they belong to and send ONLY that
        file's own errors back to the LLM for a scoped repair, then rebuild."""
    def _pass2_verify_and_repair(
            self,
            file_records:Dict[str,_FileWorkItem],
            target_file_paths:Set[str],
            identifier_to_file:Dict[str,str]
    )->BuildResult:
        build_result:Optional[BuildResult]=None
        round_number=0

        while True:
            build_result=self._verify_in_sandbox()

            if not build_result.success:
                print(build_result.raw_logs)

            if build_result.success:
                print(f" [BUILD_GREEN] after {round_number} repair round(s)")
                break

            round_number+=1
            if round_number>self.retry_budget:
                print(f" [BUDGET SPENT] repair budget ({self.retry_budget} round(s) exhausted; build still red)")
                break

            # ── Dynamic target discovery ──────────────────────────────────
            # If errors come from files not yet in the target set, adopt them
            # so the repair loop can fix them instead of being stuck.
            for error in build_result.compiler_errors:
                if error.file_path and error.file_path not in target_file_paths:
                    new_file=self.repo_path/error.file_path
                    if not new_file.is_file():
                        continue
                    target_file_paths.add(error.file_path)
                    identifier_to_file[error.file_path]=error.file_path
                    rule,summary=_migration_spec_for(error.file_path,[])
                    new_target=MigrationTarget(
                        identifier=error.file_path,
                        file_path=error.file_path,
                        package_name=None,
                        migration_depth=0,
                        in_degree=0,
                        out_degree=0,
                        is_leaf=True,
                        is_test=False,
                        has_javax=False,
                        matched_rules=[]
                    )
                    file_records[error.file_path]=_FileWorkItem(
                        target=new_target,
                        migration_rule=rule,
                        commit_summary=summary
                    )
                    print(f" [DISCOVERED] {error.file_path} — added as new migration target")

            grouped=_group_diagnostics_by_target(
                build_result.compiler_errors,build_result.failed_tests,target_file_paths,identifier_to_file
            )
            actionable={
                file_path:diags for file_path,diags in grouped.items()
                if file_path in file_records and not file_records[file_path].skipped
            }
            if not actionable:
                print(" [STUCK] build failed but no diagnostics could be attributed to a tracked target; stopping")
                break

            self._transition(WorkflowState.REPAIRING)
            print(f" round {round_number}/{self.retry_budget}: repairing {len(actionable)} file(s) "
                  f"->{', '.join(sorted(actionable))}")

            repaired_any=False
            for file_path,(compiler_errors,failed_tests) in actionable.items():
                record=file_records[file_path]
                record.attempts+=1

                record.errors_encountered=_diagnostics_summary(compiler_errors,failed_tests)
                try:
                    source_code=(self.repo_path/file_path).read_text(encoding="utf-8")
                    patch=self.generate_patch(file_path,source_code,record.migration_rule,compiler_errors,failed_tests)
                    apply_patch(self.repo_path,patch)
                    repaired_any=True
                except LLMProviderError as exc:
                    record.errors_encountered.append(f"LLM generation failed: {exc}")
                except SecurityViolationError as exc:
                    record.errors_encountered.append(f"Guardrail rejected patch: {exc}")

            if not repaired_any:
                print(" [STUCK] every targeted file failed to produce a new patch; stopping")
                break

        return build_result

    def _current_head_sha(self)->Optional[str]:
        try:
            from git import Repo
            return Repo(self.repo_path).head.commit.hexsha
        except Exception:
            return None

    def run(self,ingestion_report:ProjectIngestionReport)->MigrationRunSummary:
        start=time.monotonic()
        self._transition(WorkflowState.INITIALIZING)

        baseline_manifests=JavaAstParser(str(self.repo_path)).parse_repo()
        baseline_test_count=sum(m.test_count for m in baseline_manifests)
        print(f"[orchestrator] baseline: {len(baseline_manifests)} Java file(s), "
              f"{baseline_test_count} @Test method(s), "
              f"{len(ingestion_report.files_to_migrate)} migration target(s)")

        identifier_to_file={t.identifier:t.file_path for t in ingestion_report.files_to_migrate}
        target_file_paths={t.file_path for t in ingestion_report.files_to_migrate}
        print("[orchestrator] Pass 1/2: batch-transforming every target file(no sandbox verification yet)")
        
        file_records:Dict[str,_FileWorkItem]={}
        for target in ingestion_report.files_to_migrate:
            file_records[target.file_path]=self._pass1_transform(target)

        print("[orchestrator] Pass 2/2: verifying the full build and repairing targeted failures")

        build_result=self._pass2_verify_and_repair(file_records,target_file_paths,identifier_to_file)
        commit_sha:Optional[str]=None
        if build_result.success:
            self._transition(WorkflowState.COMMITING)
            migrated_paths=[fp for fp,rec in file_records.items() if not rec.skipped]
            commit_sha=commit_patch(
                self.repo_path,
                commit_message=f"Migrate {len(migrated_paths)} file(s): Spring Boot 2 -> 3, Java 17, javax -> jakarta",
                file_path=None
            )
            print(f" [COMMIT] {commit_sha[:10]} ({len(migrated_paths)} file(s))")
        else:
            print(" [ROLLBACK] build never went green; reverting the entire workspace to baseline")
            self._rollback_all()

        file_statuses:List[FileMigrationStatus]=[]
        for file_path,record in file_records.items():
            if record.skipped:
                status=FileStatus.SKIPPED
            elif build_result.success:
                status=FileStatus.SUCCESS
            else:
                status=FileStatus.FAILED
                if not record.errors_encountered:
                    record.errors_encountered=[
                        "Build never went green because of sibling file's unresolved errors; "
                        "this file's own patch was never implicated"
                    ]

            file_statuses.append(
                FileMigrationStatus(
                    file_path=file_path,
                    attempts=record.attempts,
                    status=status,
                    commit_sha=commit_sha if status==FileStatus.SUCCESS else None,
                    errors_encountered=record.errors_encountered
                )
            )

        total_retries=sum(max(r.attempts-1,0) for r in file_records.values())

        if build_result.success:
            self._transition(WorkflowState.POST_INTEGRITY_CHECK)
            integrity=run_post_execution_integrity(self.repo_path,baseline_manifests,baseline_test_count)
            for violation in integrity.violations:
                print(f" [INTEGRITY VIOLATION] {violation}")
        else:
            integrity=PostExecutionIntegrityResult(
                baseline_test_count=baseline_test_count,
                final_test_count=baseline_test_count,
                test_count_passed=True,
                missing_public_methods=[],
                method_signatures_passed=True,
                passed=False,
                violations=["Migration aborted before a passing build was reached; "
                            "no commit was made and the workspace was reverted to baseline."]
            )

        final_state=WorkflowState.COMPLETED if (build_result.success and integrity.passed) else WorkflowState.FAILED
        self._transition(final_state)

        return MigrationRunSummary(
            repo_url=ingestion_report.repo_info.repo_url,
            baseline_commit=ingestion_report.repo_info.baseline_sha,
            final_commit=self._current_head_sha(),
            total_files_migrated=sum(1 for s in file_statuses if s.status==FileStatus.SUCCESS),
            total_retries=total_retries,
            file_statuses=file_statuses,
            integrity_passed=integrity.passed,
            total_duration_seconds=round(time.monotonic()-start,2),
            final_state=final_state,
            integrity_violations=integrity.violations
        )