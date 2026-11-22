from dataclasses import dataclass, field
from pathlib import Path
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from app.config import settings
from app.guardrails.post_execution import (
    PostExecutionIntegrityResult,
    run_post_execution_integrity,
)
from app.guardrails.pre_execution import SecurityViolationError
from app.ingestion.ast_parser import JavaAstParser
from app.ingestion.migration_planner import (
    build_migration_plan,
    render_pom_migration_instruction,
    render_resource_migration_instruction,
)
from app.ingestion.migration_rules import SPRING_BOOT_3_RULES
from app.ingestion.openrewrite_runner import (
    OpenRewriteExecutionResult,
    OpenRewriteRunner,
)
from app.ingestion.patch_applicator import (
    apply_patch_with_diff,
    commit_patch,
)
from app.llm.base import LLMProvider, LLMProviderError
from app.llm.factory import LLMFactory
from app.llm.prompts import CODER_AGENT_SYSTEM_INSTRUCTION, build_coder_prompt
from app.models.agent import FilePatchResult
from app.models.execution import BuildResult, CompilerError, TestFailure
from app.models.manifest import (
    JavaFileManifest,
    MigrationPlan,
    MigrationTarget,
    ProjectIngestionReport,
)
from app.models.orchestrator import (
    FileMigrationStatus,
    FileStatus,
    MigrationRunSummary,
    WorkflowState,
)
from app.orchestrator.dependency_context import (
    build_local_dep_lookup,
    collect_dependency_context,
    expand_type_ancestors,
    parse_manifest_cached,
)
from app.orchestrator.java_patch_utils import (
    repair_illegal_java_string_escapes as _repair_illegal_java_string_escapes,
    restore_stripped_java_backslashes as _restore_stripped_java_backslashes,
    validate_java_api_sanity as _validate_java_api_sanity,
    validate_multiline_java_formatting as _validate_multiline_java_formatting,
)
from app.orchestrator.repair_heuristics import (
    app_candidate_priority,
    extract_implicated_bean_stems,
    extract_runtime_log_excerpt,
    is_pure_cascading_override_failure,
    looks_like_app_runtime_failure,
    resolve_sandbox_db_fallback_args,
)
from app.sandbox.diagnostics_parser import (
    classify_build_failure,
    is_database_environment_failure,
)
from app.sandbox.docker_runner import DockerRunner
from app.sandbox.service_detector import ServiceRequirement, detect_required_services

# Build result reported when a migration is cancelled mid-run: red, no diagnostics,
# so the orchestrator rolls the workspace back to baseline without committing.
_CANCELLED_BUILD_RESULT = BuildResult(
    success=False,
    exit_code=-1,
    duration_seconds=0.0,
    compiler_errors=[],
    failed_tests=[],
    raw_logs="Migration cancelled by user.",
)

GENERIC_SPRING_BOOT_3_RULE = (
    "This file needs to be updated for Spring Boot 3 compatibility. "
    "Examine the file for any Spring Boot 2.x-era APIs, deprecated classes, "
    "or removed patterns (Jakarta EE javax.* -> jakarta.*, removed adapter superclasses, "
    "renamed packages) and apply the necessary changes. "
    "Do not change Java SE javax.* packages (e.g., javax.sql.*, javax.crypto.*) "
    "or unrelated business logic."
)


def _migration_spec_for(
    target_file_path: str,
    matched_rules: List[str],
    plan: MigrationPlan,
    file_content: str = "",
) -> Tuple[str, str]:
    if Path(target_file_path).name == "pom.xml":
        return (
            render_pom_migration_instruction(plan),
            f"upgrade pom.xml to Spring Boot {plan.target_spring_boot} / Java {plan.target_java}",
        )

    if "resource-migration" in matched_rules or Path(target_file_path).suffix.lower() in (
        ".html",
        ".xhtml",
        ".yml",
        ".yaml",
        ".properties",
        ".xml",
    ):
        return (
            render_resource_migration_instruction(target_file_path, file_content),
            "Spring Boot 3 resource/template compatibility",
        )

    rules = [r for r in SPRING_BOOT_3_RULES if r.id in matched_rules]
    if not rules:
        return GENERIC_SPRING_BOOT_3_RULE, "Spring Boot 3 migration"

    instruction = "\n\n".join(r.migration_instruction for r in rules)
    summary = "; ".join(r.commit_summary for r in rules)
    return instruction, summary


def _diagnostics_summary(
    compiler_errors: List[CompilerError], failed_tests: List[TestFailure]
) -> List[str]:
    summary = [
        f"{e.file_path}:{e.line_number}:{e.column} {e.message}"
        if e.line_number is not None
        else f"{e.file_path or '(unattributed)'}: {e.message}"
        for e in compiler_errors
    ]
    summary += [
        f"{t.test_class}:{t.test_method}: {t.failure_message}" for t in failed_tests
    ]
    return summary


def _group_diagnostics_by_target(
    compiler_errors: List[CompilerError],
    failed_tests: List[TestFailure],
    target_file_paths: Set[str],
    identifier_to_file: Dict[str, str],
) -> Dict[str, Tuple[List[CompilerError], List[TestFailure]]]:
    grouped: Dict[str, Tuple[List[CompilerError], List[TestFailure]]] = {}

    def _bucket(file_path: str) -> Tuple[List[CompilerError], List[TestFailure]]:
        return grouped.setdefault(file_path, ([], []))

    for error in compiler_errors:
        if error.file_path and error.file_path in target_file_paths:
            _bucket(error.file_path)[0].append(error)

    for failure in failed_tests:
        matched_file = None
        for identifier, file_path in identifier_to_file.items():
            if identifier == failure.test_class or identifier.endswith(
                f".{failure.test_class}"
            ):
                matched_file = file_path
                break

        if matched_file and matched_file in target_file_paths:
            _bucket(matched_file)[1].append(failure)

    return grouped


def _order_actionable_ancestors_first(
    ordered_items: List[Tuple[str, Tuple[List[CompilerError], List[TestFailure]]]],
    ancestors_by_path: Dict[str, Set[str]],
) -> List[Tuple[str, Tuple[List[CompilerError], List[TestFailure]]]]:
    """Order actionable files so parent interfaces/superclasses run before subclasses in the same round."""
    if len(ordered_items) <= 1:
        return ordered_items

    index_by_path = {fp: idx for idx, (fp, _) in enumerate(ordered_items)}
    dependents: Dict[str, List[str]] = {fp: [] for fp, _ in ordered_items}
    indegree: Dict[str, int] = {fp: 0 for fp, _ in ordered_items}

    for fp, _ in ordered_items:
        for anc in ancestors_by_path.get(fp, ()):
            if anc in index_by_path and anc != fp:
                dependents[anc].append(fp)
                indegree[fp] += 1

    ready = sorted(
        (fp for fp, deg in indegree.items() if deg == 0),
        key=lambda fp: index_by_path[fp],
    )
    item_by_path = dict(ordered_items)
    ordered_paths: List[str] = []
    seen: Set[str] = set()

    while ready:
        cur = ready.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        ordered_paths.append(cur)
        for child in dependents[cur]:
            indegree[child] -= 1
            if indegree[child] == 0 and child not in seen:
                ready.append(child)
        ready.sort(key=lambda fp: index_by_path[fp])

    if len(ordered_paths) < len(ordered_items):
        for fp, _ in ordered_items:
            if fp not in seen:
                ordered_paths.append(fp)

    return [(fp, item_by_path[fp]) for fp in ordered_paths]


@dataclass
class _FileWorkItem:
    target: MigrationTarget
    migration_rule: str
    commit_summary: str
    state: FileStatus = FileStatus.PENDING
    applied_by: Optional[str] = None
    attempts: int = 0
    diff: Optional[str] = None
    errors_encountered: List[str] = field(default_factory=list)
    prev_compiler_signatures: Set[str] = field(default_factory=set)


class MigrationOrchestrator:
    def __init__(
        self,
        repo_path: Path,
        llm_provider: Optional[LLMProvider] = None,
        docker_runner: Optional[DockerRunner] = None,
        openrewrite_runner: Optional[OpenRewriteRunner] = None,
        retry_budget: Optional[int] = None,
        maven_args: Optional[List[str]] = None,
        use_openrewrite: Optional[bool] = None,
        openrewrite_mode: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.repo_path = Path(repo_path)
        self._llm_provider = llm_provider
        self.docker_runner = docker_runner or DockerRunner()
        self.use_openrewrite = (
            settings.use_openrewrite if use_openrewrite is None else use_openrewrite
        )
        self.openrewrite_runner = openrewrite_runner or OpenRewriteRunner(
            executor=self.docker_runner if hasattr(self.docker_runner, "run_maven_rewrite") else None,
            allow_deterministic_fallback=True,
            mode=openrewrite_mode,
        )
        self.retry_budget = (
            settings.default_retry_budget if retry_budget is None else retry_budget
        )
        self.maven_args = maven_args
        self.progress_callback = progress_callback
        self._cancel_event = cancel_event
        self.state = WorkflowState.INITIALIZING
        self._ast_parser: Optional[JavaAstParser] = None
        self._manifest_cache: Dict[str, Optional[JavaFileManifest]] = {}

    @property
    def ast_parser(self) -> JavaAstParser:
        if self._ast_parser is None:
            self._ast_parser = JavaAstParser(str(self.repo_path))
        return self._ast_parser

    @property
    def llm_provider(self) -> LLMProvider:
        if self._llm_provider is None:
            self._llm_provider = LLMFactory.create()
        return self._llm_provider

    def _transition(self, new_state: WorkflowState) -> None:
        if self.state != new_state:
            print(f"[orchestrator] {self.state.value} -> {new_state.value}")
        self.state = new_state
        if self.progress_callback and new_state not in (
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
        ):
            messages = {
                WorkflowState.INITIALIZING: "Initializing migration workflow",
                WorkflowState.OPENREWRITE_PASS: "Running Pass 1 OpenRewrite migration",
                WorkflowState.MIGRATING_FILE: "Applying deterministic file migrations",
                WorkflowState.GENERATING_PATCH: "Generating an AI repair patch",
                WorkflowState.VERIFYING_SANDBOX: "Running Maven verification in the Docker sandbox",
                WorkflowState.REPAIRING: "Preparing an AI repair round",
                WorkflowState.POST_INTEGRITY_CHECK: "Running post-migration integrity checks",
                WorkflowState.COMMITING: "Committing validated migration changes",
            }
            self.progress_callback(f"{new_state.value}:{messages[new_state]}")

    def _request_patch_and_restore(
        self, prompt: str, target_file_path: str, source_code: str
    ) -> Tuple[FilePatchResult, Optional[str]]:
        patch = self.llm_provider.generate_structured(
            prompt=prompt,
            response_schema=FilePatchResult,
            system_instruction=CODER_AGENT_SYSTEM_INSTRUCTION,
        )
        patch.new_content = _restore_stripped_java_backslashes(
            target_file_path, source_code, patch.new_content
        )
        fmt_error = _validate_multiline_java_formatting(
            target_file_path, source_code, patch.new_content
        )
        return patch, fmt_error

    def _request_validated_patch(
        self,
        prompt: str,
        target_file_path: str,
        source_code: str,
        migration_rule: str,
        illegal_escape_lines: Optional[Set[int]] = None,
    ) -> FilePatchResult:
        patch, fmt_error = self._request_patch_and_restore(
            prompt, target_file_path, source_code
        )
        if fmt_error:
            print(f" [FORMAT RETRY] {target_file_path}: {fmt_error}")
            retry_prompt = (
                f"{prompt}\n\nCRITICAL FORMATTING CORRECTION: {fmt_error} "
                "Return the complete file with proper multi-line formatting and real '\\n' "
                "line breaks between every import, annotation, field, and method."
            )
            patch, fmt_error = self._request_patch_and_restore(
                retry_prompt, target_file_path, source_code
            )
            if fmt_error:
                raise SecurityViolationError(
                    f"Patch formatting validation failed after retry: {fmt_error}"
                )

        api_sanity_error = _validate_java_api_sanity(
            target_file_path, migration_rule, patch.new_content
        )
        if api_sanity_error:
            print(f" [API SANITY RETRY] {target_file_path}: {api_sanity_error}")
            sanity_prompt = (
                f"{prompt}\n\nCRITICAL API CORRECTION: {api_sanity_error} "
                "Update the entire file to fix this issue while preserving all business logic and method contracts."
            )
            patch, fmt_error = self._request_patch_and_restore(
                sanity_prompt, target_file_path, source_code
            )
            if fmt_error:
                raise SecurityViolationError(
                    f"Patch formatting validation failed after API sanity retry: {fmt_error}"
                )

        if target_file_path.endswith(".java") and illegal_escape_lines:
            patch.new_content = _repair_illegal_java_string_escapes(
                patch.new_content, target_lines=illegal_escape_lines
            )
        return patch

    def generate_patch(
        self,
        target_file_path: str,
        source_code: str,
        migration_rule: str,
        compiler_errors: List[CompilerError],
        failed_tests: List[TestFailure],
        runtime_log_excerpt: Optional[str] = None,
        migration_context: Optional[str] = None,
        dependency_context: Optional[str] = None,
    ) -> FilePatchResult:
        self._transition(WorkflowState.GENERATING_PATCH)
        prompt = build_coder_prompt(
            target_file_path=target_file_path,
            source_code=source_code,
            migration_rule=migration_rule,
            compiler_errors=compiler_errors or None,
            failed_tests=failed_tests or None,
            runtime_log_excerpt=runtime_log_excerpt,
            migration_context=migration_context,
            dependency_context=dependency_context,
        )
        illegal_escape_lines = {
            e.line_number
            for e in (compiler_errors or [])
            if e.line_number is not None
            and "illegal escape character" in e.message.lower()
        }
        patch = self._request_validated_patch(
            prompt,
            target_file_path,
            source_code,
            migration_rule,
            illegal_escape_lines=illegal_escape_lines,
        )

        if (compiler_errors or failed_tests) and (
            (patch.new_content or "").strip() == source_code.strip()
        ):
            print(
                f" [NO-OP RETRY] {target_file_path}: LLM returned unchanged code despite active diagnostics; retrying"
            )
            noop_prompt = (
                f"{prompt}\n\nCRITICAL NO-OP CORRECTION: Your previous response returned "
                f"'{target_file_path}' completely unchanged (zero diff), leaving all reported "
                "compiler error(s) / test failure(s) unfixed. You MUST modify the failing line(s) "
                "shown in the diagnostics above so the code compiles and passes tests cleanly."
            )
            patch = self._request_validated_patch(
                noop_prompt,
                target_file_path,
                source_code,
                migration_rule,
                illegal_escape_lines=illegal_escape_lines,
            )
        return patch

    def _verify_in_sandbox(
        self,
        maven_args: Optional[List[str]] = None,
        services: Optional[List[ServiceRequirement]] = None,
    ) -> BuildResult:
        self._transition(WorkflowState.VERIFYING_SANDBOX)
        effective_args = self.maven_args if maven_args is None else maven_args
        if services is not None:
            return self.docker_runner.run_tests(
                self.repo_path, effective_args, services=services
            )
        return self.docker_runner.run_tests(self.repo_path, effective_args)

    def _rollback_all(self) -> None:
        try:
            from git import InvalidGitRepositoryError, Repo

            repo = Repo(self.repo_path)
            repo.git.reset("--hard")
            repo.git.clean("-fd")
        except InvalidGitRepositoryError:
            return

    def _current_head_sha(self) -> Optional[str]:
        try:
            from git import Repo

            return Repo(self.repo_path).head.commit.hexsha
        except Exception:
            return None

    def _run_openrewrite_pass(
        self,
        plan: MigrationPlan,
        graph_targets_by_path: Dict[str, MigrationTarget],
        file_records: Dict[str, _FileWorkItem],
        modified_files: Set[str],
    ) -> Optional[OpenRewriteExecutionResult]:
        if not self.use_openrewrite:
            return None

        self._transition(WorkflowState.OPENREWRITE_PASS)
        result = self.openrewrite_runner.run(self.repo_path, plan)
        if result.executed:
            for file_path in result.modified_files:
                abs_file = self.repo_path / file_path
                if not abs_file.is_file():
                    continue
                known = graph_targets_by_path.get(file_path)
                matched_rules = list(known.matched_rules) if known else []
                content = abs_file.read_text(encoding="utf-8", errors="ignore")
                rule, summary = _migration_spec_for(file_path, matched_rules, plan, content)
                target = known or MigrationTarget(
                    identifier=file_path,
                    file_path=file_path,
                    package_name=None,
                    migration_depth=0,
                    in_degree=0,
                    out_degree=0,
                    is_leaf=True,
                    is_test="/src/test/" in f"/{file_path}",
                    has_javax=False,
                    matched_rules=matched_rules,
                )
                file_records[file_path] = _FileWorkItem(
                    target=target,
                    migration_rule=rule,
                    commit_summary=summary,
                    state=FileStatus.PATCH_APPLIED,
                    applied_by="openrewrite",
                    attempts=1,
                    diff=result.diffs.get(file_path),
                )
                modified_files.add(file_path)
                print(f" [OPENREWRITE] {file_path} ({summary})")

            post_or_manifests = self.ast_parser.parse_repo()
            self._manifest_cache = {m.file_path: m for m in post_or_manifests}
        if result.error:
            print(
                f" [OPENREWRITE NOTICE] {result.error}; "
                "Pass 2 will verify the build and use AI to resolve any errors."
            )
        return result

    def _ensure_work_item(
        self,
        file_path: str,
        file_records: Dict[str, _FileWorkItem],
        target_file_paths: Set[str],
        identifier_to_file: Dict[str, str],
        plan: MigrationPlan,
        graph_targets_by_path: Optional[Dict[str, MigrationTarget]],
        is_test: bool = False,
    ) -> Optional[_FileWorkItem]:
        if file_path in file_records:
            return file_records[file_path]
        target_file = self.repo_path / file_path
        if not target_file.is_file():
            return None

        target_file_paths.add(file_path)
        identifier_to_file.setdefault(file_path, file_path)
        known_target = (
            graph_targets_by_path.get(file_path)
            if graph_targets_by_path
            else None
        )
        matched_rules = list(known_target.matched_rules) if known_target else []
        rule, summary = _migration_spec_for(
            file_path,
            matched_rules,
            plan,
            target_file.read_text(encoding="utf-8", errors="ignore"),
        )
        target = known_target or MigrationTarget(
            identifier=file_path,
            file_path=file_path,
            package_name=None,
            migration_depth=0,
            in_degree=0,
            out_degree=0,
            is_leaf=True,
            is_test=is_test or ("/src/test/" in f"/{file_path}"),
            has_javax=False,
            matched_rules=matched_rules,
        )
        item = _FileWorkItem(
            target=target,
            migration_rule=rule,
            commit_summary=summary,
            state=FileStatus.PENDING,
        )
        file_records[file_path] = item
        return item

    def _discover_missing_targets(
        self,
        build_result: BuildResult,
        target_file_paths: Set[str],
        identifier_to_file: Dict[str, str],
        file_records: Dict[str, _FileWorkItem],
        plan: MigrationPlan,
        graph_targets_by_path: Optional[Dict[str, MigrationTarget]],
    ) -> None:
        for error in build_result.compiler_errors:
            if error.file_path and error.file_path not in file_records:
                added = self._ensure_work_item(
                    error.file_path,
                    file_records,
                    target_file_paths,
                    identifier_to_file,
                    plan,
                    graph_targets_by_path,
                    is_test=False,
                )
                if added is not None:
                    print(
                        f" [DISCOVERED] {error.file_path} - added as repair target"
                    )

        for failure in build_result.failed_tests:
            matched_path = None
            for ident, fpath in identifier_to_file.items():
                if ident == failure.test_class or ident.endswith(f".{failure.test_class}"):
                    matched_path = fpath
                    break
            if matched_path and matched_path not in file_records:
                added = self._ensure_work_item(
                    matched_path,
                    file_records,
                    target_file_paths,
                    identifier_to_file,
                    plan,
                    graph_targets_by_path,
                    is_test=True,
                )
                if added is not None:
                    print(
                        f" [DISCOVERED] {matched_path} - added failing test as repair target"
                    )

    def _resolve_repair_rule(
        self,
        file_path: str,
        record: _FileWorkItem,
        source_code: str,
        plan: MigrationPlan,
        compiler_errors: List[CompilerError],
        failed_tests: List[TestFailure],
    ) -> str:
        if (
            record.applied_by == "openrewrite"
            and (compiler_errors or failed_tests)
        ):
            if file_path.endswith(".java"):
                cur_manifest = parse_manifest_cached(
                    self.ast_parser,
                    self.repo_path,
                    file_path,
                    self._manifest_cache,
                )
                cur_matched = (
                    [
                        r.id
                        for r in SPRING_BOOT_3_RULES
                        if r.predicate(cur_manifest)
                    ]
                    if cur_manifest is not None
                    else []
                )
                if cur_matched:
                    rule, _ = _migration_spec_for(
                        file_path, cur_matched, plan, source_code
                    )
                    return rule
                return GENERIC_SPRING_BOOT_3_RULE
            rule, _ = _migration_spec_for(file_path, [], plan, source_code)
            return rule
        return record.migration_rule

    def _pass2_verify_and_repair(
        self,
        file_records: Dict[str, _FileWorkItem],
        target_file_paths: Set[str],
        identifier_to_file: Dict[str, str],
        plan: MigrationPlan,
        pom_has_h2: bool = False,
        modified_files: Optional[Set[str]] = None,
        fqcn_to_file: Optional[Dict[str, str]] = None,
        package_to_files: Optional[Dict[str, List[str]]] = None,
        graph_targets_by_path: Optional[Dict[str, MigrationTarget]] = None,
        planned_targets_by_path: Optional[Dict[str, MigrationTarget]] = None,
        openrewrite_error: Optional[str] = None,
    ) -> BuildResult:
        build_result: Optional[BuildResult] = None
        round_number = 0
        repaired_test_files: Set[str] = set()
        runtime_repaired_app_files: Set[str] = set()
        active_maven_args: Optional[List[str]] = (
            list(self.maven_args) if self.maven_args is not None else None
        )
        active_services: Optional[List[ServiceRequirement]] = None
        tried_db_fallbacks: Set[str] = set()

        while True:
            if self._cancel_event is not None and self._cancel_event.is_set():
                build_result = _CANCELLED_BUILD_RESULT
                break
            build_result = self._verify_in_sandbox(active_maven_args, active_services)

            if not build_result.success:
                print(build_result.raw_logs)

            if build_result.success:
                print(f" [BUILD_GREEN] after {round_number} repair round(s)")
                break

            failure_kind = classify_build_failure(
                build_result.raw_logs,
                build_result.compiler_errors,
                build_result.failed_tests,
                exit_code=build_result.exit_code,
            )
            if failure_kind == "environment":
                if (
                    "dynamic_services" not in tried_db_fallbacks
                    and getattr(self.docker_runner, "dynamic_services", False)
                ):
                    inferred = detect_required_services(
                        self.repo_path,
                        raw_logs=build_result.raw_logs,
                        maven_args=active_maven_args,
                    )
                    if inferred and active_services is None:
                        tried_db_fallbacks.add("dynamic_services")
                        active_services = inferred
                        svc_list = ", ".join(f"{s.kind}:{s.port}" for s in inferred)
                        print(
                            f" [SANDBOX SERVICES] Log diagnostics indicate missing service(s) "
                            f"({svc_list}); retrying with ephemeral sidecar containers..."
                        )
                        continue

                if is_database_environment_failure(
                    build_result.raw_logs, build_result.failed_tests
                ):
                    fallback = resolve_sandbox_db_fallback_args(
                        repo_path=self.repo_path,
                        pom_has_h2=pom_has_h2,
                        initial_maven_args=self.maven_args,
                        current_maven_args=active_maven_args,
                        tried_fallbacks=tried_db_fallbacks,
                    )
                    if fallback is not None:
                        key, reason, next_args = fallback
                        tried_db_fallbacks.add(key)
                        active_maven_args = next_args
                        active_services = None
                        print(
                            f" [SANDBOX DB FALLBACK] {reason}; "
                            "retrying verification without mutating repository config."
                        )
                        continue
                env_reason = "; ".join(
                    e.message for e in build_result.compiler_errors[:2] if e.message
                )
                suffix = f" ({env_reason})" if env_reason else ""
                print(
                    f" [ENV FAILURE] Build failed due to external/environment issue "
                    f"(exit_code={build_result.exit_code}){suffix}; "
                    "aborting repair loop without modifying code."
                )
                break

            round_number += 1
            if round_number > self.retry_budget:
                print(
                    f" [BUDGET SPENT] repair budget ({self.retry_budget} round(s) exhausted; build still red)"
                )
                break

            self._discover_missing_targets(
                build_result,
                target_file_paths,
                identifier_to_file,
                file_records,
                plan,
                graph_targets_by_path,
            )

            grouped = _group_diagnostics_by_target(
                build_result.compiler_errors,
                build_result.failed_tests,
                target_file_paths,
                identifier_to_file,
            )
            actionable = {
                file_path: diags
                for file_path, diags in grouped.items()
                if file_path in file_records
                and file_records[file_path].state not in (FileStatus.SKIPPED, FileStatus.REJECTED)
            }

            # javac defaults to -Xmaxerrs 100, hiding downstream failing targets on large repos.
            # When javac reaches its error cap, surface unattempted non-OpenRewrite planned targets
            # from ingestion so Pass 2 can migrate them together instead of leaking across extra rounds.
            javac_truncated = len(build_result.compiler_errors) >= 80 or (
                "too many errors" in (build_result.raw_logs or "").lower()
            )
            if javac_truncated and planned_targets_by_path:
                surfaced_count = 0
                for fpath, pt in planned_targets_by_path.items():
                    if fpath in actionable or fpath in file_records:
                        continue
                    added = self._ensure_work_item(
                        fpath,
                        file_records,
                        target_file_paths,
                        identifier_to_file,
                        plan,
                        graph_targets_by_path,
                        is_test=pt.is_test,
                    )
                    if added is not None and added.state not in (
                        FileStatus.SKIPPED,
                        FileStatus.REJECTED,
                    ):
                        actionable[fpath] = ([], [])
                        surfaced_count += 1
                if surfaced_count:
                    print(
                        f" [DISCOVERED] surfaced {surfaced_count} unattempted planned target(s) "
                        f"(javac reported {len(build_result.compiler_errors)} errors)"
                    )

            # If Pass 1 did not upgrade pom.xml (e.g. --no-openrewrite or maven_only failure)
            # and sandbox build failed with unattributed build/POM diagnostics, repair pom.xml first.
            if (
                not actionable
                and (not build_result.compiler_errors or all(e.file_path is None for e in build_result.compiler_errors))
                and not build_result.failed_tests
                and (self.repo_path / "pom.xml").is_file()
            ):
                pom_rec = self._ensure_work_item(
                    "pom.xml",
                    file_records,
                    target_file_paths,
                    identifier_to_file,
                    plan,
                    graph_targets_by_path,
                    is_test=False,
                )
                if pom_rec is not None and pom_rec.state not in (FileStatus.SKIPPED, FileStatus.REJECTED):
                    actionable = {"pom.xml": (list(build_result.compiler_errors), [])}

            runtime_log_excerpt: Optional[str] = None
            repair_context: Optional[str] = (
                f"Pass 1 OpenRewrite reported: {openrewrite_error}"
                if openrewrite_error
                else None
            )
            is_runtime_pivot = False
            if not build_result.compiler_errors and build_result.failed_tests:
                only_test_targets = bool(actionable) and all(
                    file_records[fp].target.is_test for fp in actionable
                )
                already_tried_test = bool(actionable) and all(
                    fp in repaired_test_files for fp in actionable
                )
                if only_test_targets and (
                    looks_like_app_runtime_failure(
                        build_result.failed_tests, build_result.raw_logs
                    )
                    or already_tried_test
                ):
                    implicated_stems = extract_implicated_bean_stems(build_result.raw_logs)

                    # Materialize unmigrated non-test planned targets and any implicated repo classes
                    if planned_targets_by_path:
                        for fpath, pt in planned_targets_by_path.items():
                            if not pt.is_test and fpath not in file_records:
                                self._ensure_work_item(
                                    fpath,
                                    file_records,
                                    target_file_paths,
                                    identifier_to_file,
                                    plan,
                                    graph_targets_by_path,
                                    is_test=False,
                                )

                    for stem in implicated_stems:
                        for fpath in set(identifier_to_file.values()):
                            if (
                                Path(fpath).stem.lower() == stem
                                and fpath not in file_records
                                and (self.repo_path / fpath).is_file()
                                and "/src/test/" not in f"/{fpath}"
                            ):
                                self._ensure_work_item(
                                    fpath,
                                    file_records,
                                    target_file_paths,
                                    identifier_to_file,
                                    plan,
                                    graph_targets_by_path,
                                    is_test=False,
                                )

                    app_candidates = sorted(
                        (
                            fp
                            for fp, rec in file_records.items()
                            if not rec.target.is_test
                            and rec.state not in (FileStatus.SKIPPED, FileStatus.REJECTED)
                            and fp not in runtime_repaired_app_files
                        ),
                        key=lambda fp: (
                            0 if Path(fp).stem.lower() in implicated_stems else 1,
                            0
                            if any(
                                r in file_records[fp].target.matched_rules
                                for r in (
                                    "security-configurer-adapter-removal",
                                    "resource-migration",
                                    "springfox-to-springdoc",
                                    "hibernate-5-to-6",
                                )
                            )
                            else 1,
                            app_candidate_priority(fp, file_records[fp]),
                        ),
                    )
                    prioritized_implicated = [
                        fp for fp in app_candidates if Path(fp).stem.lower() in implicated_stems
                    ]
                    unmigrated_or_llm = [
                        fp
                        for fp in app_candidates
                        if file_records[fp].applied_by in (None, "llm")
                    ]
                    high_risk_candidates = [
                        fp
                        for fp in app_candidates
                        if any(
                            r in file_records[fp].target.matched_rules
                            for r in (
                                "security-configurer-adapter-removal",
                                "resource-migration",
                                "springfox-to-springdoc",
                                "hibernate-5-to-6",
                            )
                        )
                    ]
                    if prioritized_implicated:
                        chosen = prioritized_implicated[:2]
                    elif unmigrated_or_llm:
                        chosen = unmigrated_or_llm[:2]
                    elif high_risk_candidates:
                        chosen = high_risk_candidates[:2]
                    elif app_candidates:
                        chosen = app_candidates[:1]
                    else:
                        chosen = []

                    if chosen:
                        is_runtime_pivot = True
                        actionable = {
                            fp: ([], list(build_result.failed_tests)) for fp in chosen
                        }
                        runtime_log_excerpt = extract_runtime_log_excerpt(
                            build_result.raw_logs
                        )
                        repair_context = (
                            "Integration/runtime test(s) or Spring ApplicationContext startup failed "
                            "during verification. Do NOT expect the test assertions to change; "
                            "inspect this application file for Spring Boot 3 / Spring Security 6 / "
                            "bean wiring / Thymeleaf / configuration compatibility bugs causing the failures."
                        )
                    elif already_tried_test:
                        actionable = {}

            if not actionable:
                unattributed = "; ".join(
                    e.message for e in build_result.compiler_errors[:3] if e.message
                )
                detail = f": {unattributed}" if unattributed else ""
                print(
                    f" [STUCK] build failed (exit_code={build_result.exit_code}) "
                    f"with no further repairable targets{detail}; stopping"
                )
                break

            self._transition(WorkflowState.REPAIRING)
            ordered_actionable = sorted(
                actionable.items(),
                key=lambda kv: (
                    file_records[kv[0]].target.migration_depth,
                    -file_records[kv[0]].target.in_degree,
                    kv[0],
                ),
            )

            if len(ordered_actionable) > 1 and fqcn_to_file:
                actionable_paths = {fp for fp, _ in ordered_actionable}
                ancestors_by_path: Dict[str, Set[str]] = {}
                for fp, _ in ordered_actionable:
                    if fp.endswith(".java"):
                        ancs = expand_type_ancestors(
                            [fp],
                            self.repo_path,
                            self.ast_parser,
                            fqcn_to_file,
                            self._manifest_cache,
                            max_depth=4,
                            include_seeds=False,
                        )
                        ancestors_by_path[fp] = set(ancs.keys()) & actionable_paths

                deferred_paths: Set[str] = set()
                for fp, (c_errs, f_tests) in ordered_actionable:
                    if not fp.endswith(".java") or not is_pure_cascading_override_failure(
                        c_errs, f_tests
                    ):
                        continue
                    if ancestors_by_path.get(fp):
                        deferred_paths.add(fp)
                if deferred_paths and len(deferred_paths) < len(ordered_actionable):
                    ordered_actionable = [
                        (fp, diags)
                        for fp, diags in ordered_actionable
                        if fp not in deferred_paths
                    ]
                    print(
                        f" [DEFERRED] {len(deferred_paths)} subclass(es) with cascading override errors "
                        "waiting on parent repair in this round"
                    )
                ordered_actionable = _order_actionable_ancestors_first(
                    ordered_actionable, ancestors_by_path
                )

            print(
                f" round {round_number}/{self.retry_budget}: resolving {len(ordered_actionable)} file(s) with AI "
                f"-> {', '.join(fp for fp, _ in ordered_actionable)}"
            )

            repaired_any = False
            for file_path, (compiler_errors, failed_tests) in ordered_actionable:
                record = file_records[file_path]
                record.attempts += 1
                record.state = FileStatus.GENERATING
                cur_compiler_sigs = {
                    re.sub(r"\s+", " ", e.message.strip())
                    for e in compiler_errors
                    if e.message and e.message.strip()
                }
                is_repeat_compiler_failure = bool(
                    record.prev_compiler_signatures & cur_compiler_sigs
                )
                record.prev_compiler_signatures = cur_compiler_sigs
                record.errors_encountered = _diagnostics_summary(
                    compiler_errors, failed_tests
                )
                if record.target.is_test:
                    repaired_test_files.add(file_path)
                elif is_runtime_pivot:
                    runtime_repaired_app_files.add(file_path)

                try:
                    source_code = (self.repo_path / file_path).read_text(
                        encoding="utf-8"
                    )
                    effective_excerpt = runtime_log_excerpt
                    if not effective_excerpt and (
                        Path(file_path).name == "pom.xml"
                        or any(e.file_path is None for e in build_result.compiler_errors)
                    ):
                        effective_excerpt = extract_runtime_log_excerpt(
                            build_result.raw_logs
                        )
                    effective_rule = self._resolve_repair_rule(
                        file_path,
                        record,
                        source_code,
                        plan,
                        compiler_errors,
                        failed_tests,
                    )
                    dep_context = collect_dependency_context(
                        repo_path=self.repo_path,
                        target_file_path=file_path,
                        file_records=file_records,
                        modified_files=modified_files or set(),
                        fqcn_to_file=fqcn_to_file or {},
                        package_to_files=package_to_files or {},
                        is_repair=True,
                        include_unmodified_locals=True,
                        compiler_errors=compiler_errors,
                        parser=self.ast_parser,
                        manifest_cache=self._manifest_cache,
                    )
                    effective_repair_context = repair_context
                    if is_repeat_compiler_failure:
                        repeat_note = (
                            "REPEAT REPAIR FAILURE: Your previous patch produced the EXACT same compiler error(s) again. "
                            "Do NOT keep calling the failing method with incompatible argument types; rewrite the offending line(s) "
                            "using a compatible method signature from 'Updated local project dependencies' or execute the query/logic "
                            "directly via Session / CriteriaBuilder / EntityManager."
                        )
                        effective_repair_context = (
                            f"{repair_context}\n\n{repeat_note}"
                            if repair_context
                            else repeat_note
                        )
                    patch = self.generate_patch(
                        file_path,
                        source_code,
                        effective_rule,
                        compiler_errors,
                        failed_tests,
                        runtime_log_excerpt=effective_excerpt,
                        migration_context=effective_repair_context,
                        dependency_context=dep_context,
                    )
                    _, diff = apply_patch_with_diff(
                        self.repo_path, patch, expected_target_path=file_path
                    )
                    if diff and diff.strip():
                        self._manifest_cache.pop(file_path, None)
                        record.state = FileStatus.PATCH_APPLIED
                        record.applied_by = "llm"
                        record.diff = diff
                        repaired_any = True
                        if modified_files is not None:
                            modified_files.add(file_path)
                    elif record.applied_by is not None:
                        record.state = FileStatus.PATCH_APPLIED
                    else:
                        record.state = FileStatus.PENDING
                except LLMProviderError as exc:
                    record.state = FileStatus.FAILED
                    record.errors_encountered.append(f"LLM generation failed: {exc}")
                    if self.progress_callback:
                        self.progress_callback(
                            f"REPAIRING:LLM generation failed for {file_path}: {exc}"
                        )
                except SecurityViolationError as exc:
                    record.state = FileStatus.REJECTED
                    record.errors_encountered.append(f"Guardrail rejected patch: {exc}")
                    if self.progress_callback:
                        self.progress_callback(
                            f"REPAIRING:Guardrail rejected patch for {file_path}: {exc}"
                        )

            if not repaired_any:
                if self.progress_callback:
                    self.progress_callback(
                        "REPAIRING:Repair pass produced no effective code changes; stopping"
                    )
                print(" [STUCK] repair pass produced no effective code changes; stopping")
                break

        return build_result

    def run(self, ingestion_report: ProjectIngestionReport) -> MigrationRunSummary:
        start = time.monotonic()
        plan = ingestion_report.migration_plan or build_migration_plan(
            ingestion_report.pom
        )

        baseline_manifests = self.ast_parser.parse_repo()
        self._manifest_cache = {m.file_path: m for m in baseline_manifests}
        baseline_test_count = sum(m.test_count for m in baseline_manifests)
        print(
            f"[orchestrator] baseline: {len(baseline_manifests)} Java file(s), "
            f"{baseline_test_count} @Test method(s), "
            f"{len(ingestion_report.files_to_migrate)} migration target(s), "
            f"target Boot={plan.target_spring_boot}"
        )

        fqcn_to_file, package_to_files = build_local_dep_lookup(baseline_manifests)
        graph_targets_by_path = {
            t.file_path: t for t in ingestion_report.graph.migration_order
        }
        identifier_to_file = {
            t.identifier: t.file_path for t in ingestion_report.graph.migration_order
        }
        planned_targets_by_path = {
            t.file_path: t for t in ingestion_report.files_to_migrate
        }
        for t in ingestion_report.files_to_migrate:
            identifier_to_file.setdefault(t.identifier, t.file_path)
            graph_targets_by_path.setdefault(t.file_path, t)

        file_records: Dict[str, _FileWorkItem] = {}
        modified_files: Set[str] = set()

        print(
            "[orchestrator] Pass 1/2: running OpenRewrite migration pass (OpenRewrite only)"
        )
        openrewrite_result = self._run_openrewrite_pass(
            plan=plan,
            graph_targets_by_path=graph_targets_by_path,
            file_records=file_records,
            modified_files=modified_files,
        )

        pom_has_h2 = any(
            dep.artifact_id == "h2" or dep.group_id == "com.h2database"
            for dep in ingestion_report.pom.dependencies
        )

        target_file_paths = {t.file_path for t in ingestion_report.files_to_migrate} | set(
            file_records.keys()
        )

        print(
            "[orchestrator] Pass 2/2: verifying code in sandbox and resolving any errors with AI"
        )
        build_result = self._pass2_verify_and_repair(
            file_records=file_records,
            target_file_paths=target_file_paths,
            identifier_to_file=identifier_to_file,
            plan=plan,
            pom_has_h2=pom_has_h2,
            modified_files=modified_files,
            fqcn_to_file=fqcn_to_file,
            package_to_files=package_to_files,
            graph_targets_by_path=graph_targets_by_path,
            planned_targets_by_path=planned_targets_by_path,
            openrewrite_error=(
                openrewrite_result.error if openrewrite_result else None
            ),
        )

        # Post-execution integrity checks MUST run BEFORE committing!
        if build_result.success:
            self._transition(WorkflowState.POST_INTEGRITY_CHECK)
            integrity = run_post_execution_integrity(
                self.repo_path, baseline_manifests, baseline_test_count
            )
            for violation in integrity.violations:
                print(f" [INTEGRITY VIOLATION] {violation}")
        else:
            integrity = PostExecutionIntegrityResult(
                baseline_test_count=baseline_test_count,
                final_test_count=baseline_test_count,
                test_count_passed=True,
                missing_public_methods=[],
                method_signatures_passed=True,
                passed=False,
                violations=[
                    "Migration aborted before a passing build was reached; "
                    "no commit was made and the workspace was reverted to baseline."
                ],
            )

        cancelled = self._cancel_event is not None and self._cancel_event.is_set()
        has_unrepaired_failures = any(
            rec.state in (FileStatus.FAILED, FileStatus.REJECTED)
            for rec in file_records.values()
        )
        if has_unrepaired_failures and integrity.passed:
            integrity.passed = False
            integrity.violations.append(
                "One or more target files failed transformation or were rejected by guardrails; "
                "aborting commit to prevent false-success migration."
            )

        commit_sha: Optional[str] = None
        can_commit = (
            build_result.success
            and integrity.passed
            and not has_unrepaired_failures
            and not cancelled
        )

        if can_commit:
            for rec in file_records.values():
                if rec.state == FileStatus.PATCH_APPLIED:
                    rec.state = FileStatus.VALIDATED

            self._transition(WorkflowState.COMMITING)
            migrated_paths = [
                fp
                for fp, rec in file_records.items()
                if rec.state == FileStatus.VALIDATED
            ]
            commit_sha = commit_patch(
                self.repo_path,
                commit_message=(
                    f"Migrate {len(migrated_paths)} file(s): "
                    f"Spring Boot {plan.source_spring_boot or '2.x'} -> {plan.target_spring_boot}, "
                    f"Java {plan.target_java}"
                ),
                file_path=None,
            )
            print(f" [COMMIT] {commit_sha[:10]} ({len(migrated_paths)} file(s))")
        else:
            reason = "Migration cancelled by user." if cancelled else "build or integrity checks failed"
            print(f" [ROLLBACK] {reason}; reverting workspace to baseline without committing")
            self._rollback_all()

        file_statuses: List[FileMigrationStatus] = []
        for file_path, record in file_records.items():
            if record.state == FileStatus.SKIPPED:
                final_file_status = FileStatus.SKIPPED
            elif record.state == FileStatus.REJECTED:
                final_file_status = FileStatus.REJECTED
            elif can_commit and record.state == FileStatus.VALIDATED:
                final_file_status = FileStatus.SUCCESS
            elif can_commit and record.state == FileStatus.PENDING:
                final_file_status = FileStatus.SKIPPED
            else:
                final_file_status = FileStatus.FAILED
                if not record.errors_encountered:
                    record.errors_encountered = [
                        "Migration rolled back because build or post-execution integrity checks failed."
                    ]

            file_statuses.append(
                FileMigrationStatus(
                    file_path=file_path,
                    attempts=record.attempts,
                    status=final_file_status,
                    applied_by=record.applied_by,
                    commit_sha=commit_sha
                    if final_file_status == FileStatus.SUCCESS
                    else None,
                    diff=record.diff,
                    errors_encountered=record.errors_encountered,
                )
            )

        total_retries = sum(max(r.attempts - 1, 0) for r in file_records.values())
        final_state = (
            WorkflowState.COMPLETED if can_commit else WorkflowState.FAILED
        )
        self._transition(final_state)

        last_build_errors = (
            _diagnostics_summary(
                build_result.compiler_errors, build_result.failed_tests
            )
            if not build_result.success
            else []
        )
        if not build_result.success and not last_build_errors:
            last_build_errors = [
                f"Maven sandbox exited with code {build_result.exit_code} after {build_result.duration_seconds}s without structured compiler/test diagnostics."
            ]

        return MigrationRunSummary(
            repo_url=ingestion_report.repo_info.repo_url,
            baseline_commit=ingestion_report.repo_info.baseline_sha,
            final_commit=self._current_head_sha(),
            target_spring_boot_version=plan.target_spring_boot,
            openrewrite_used=bool(openrewrite_result and openrewrite_result.executed),
            openrewrite_files_modified=len(openrewrite_result.modified_files)
            if openrewrite_result
            else 0,
            total_files_migrated=sum(
                1 for s in file_statuses if s.status == FileStatus.SUCCESS
            ),
            total_retries=total_retries,
            file_statuses=file_statuses,
            integrity_passed=integrity.passed,
            total_duration_seconds=round(time.monotonic() - start, 2),
            final_state=final_state,
            integrity_violations=integrity.violations,
            last_build_errors=last_build_errors,
            last_build_logs_tail=(
                build_result.raw_logs[-2000:] if not build_result.success else None
            ),
        )