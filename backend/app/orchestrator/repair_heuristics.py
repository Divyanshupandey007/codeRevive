from pathlib import Path
import re
from typing import Any, List, Optional, Set, Tuple

from app.config import settings
from app.models.execution import CompilerError, TestFailure

_APP_RUNTIME_FAILURE_HINTS = (
    "element not found",
    "nosuchelementexception",
    "assertionerror",
    "assertionfailederror",
    "comparisonfailure",
    "org.opentest4j",
    "expected:",
    "status expected:",
    "dataintegrityviolation",
    "badcredentials",
    "credentials is invalid",
    "accessdenied",
    "sqldataexception",
    "templateinputexception",
    "templateprocessingexception",
    "thymeleaf",
    "spel",
    "failed to load applicationcontext",
    "unsatisfieddependencyexception",
    "nosuchbeandefinitionexception",
    "beancreationexception",
    "error creating bean with name",
    "beandefinitionoverrideexception",
    "no qualifying bean of type",
    "required a bean of type",
    "could not autowire",
)

_TEST_API_FAILURE_HINTS = (
    "nosuchmethoderror",
    "noclassdeffounderror",
    "classnotfoundexception",
    "webdriverexception",
    "sessionnotcreatedexception",
)

_BEAN_NAME_RE = re.compile(r"Error creating bean with name '([A-Za-z0-9_$.]+)'")

_TEST_PROFILE_FILENAMES = (
    "application-test.properties",
    "application-test.yml",
    "application-test.yaml",
)
_MAVEN_LIFECYCLE_GOALS = {"clean", "compile", "test-compile", "test", "package", "verify", "install"}


def looks_like_app_runtime_failure(
    failed_tests: List[TestFailure], raw_logs: str = ""
) -> bool:
    if not failed_tests:
        return False
    combined_parts = [
        f"{t.failure_message} {t.stack_trace or ''}" for t in failed_tests
    ]
    if raw_logs:
        combined_parts.append(raw_logs)
    combined = " ".join(combined_parts).lower()
    if any(hint in combined for hint in _TEST_API_FAILURE_HINTS):
        return False
    return any(hint in combined for hint in _APP_RUNTIME_FAILURE_HINTS)


def extract_implicated_bean_stems(raw_logs: str) -> List[str]:
    stems: List[str] = []
    for match in _BEAN_NAME_RE.finditer(raw_logs or ""):
        bean = match.group(1).rsplit(".", 1)[-1].lower()
        if bean and bean not in stems:
            stems.append(bean)
    return stems


def extract_runtime_log_excerpt(raw_logs: str, max_chars: int = 2000) -> str:
    interesting: List[str] = []
    for line in raw_logs.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if (
            "caused by:" in lower
            or "exception" in lower
            or "error" in lower
            or "thymeleaf" in lower
            or "sql" in lower
            or "security" in lower
        ):
            if not stripped.startswith("Downloading from"):
                interesting.append(stripped)
    if not interesting:
        return raw_logs[-max_chars:]
    excerpt = "\n".join(interesting[-30:])
    return excerpt[-max_chars:]


def app_candidate_priority(file_path: str, record: Any) -> Tuple[int, int, str]:
    lower = file_path.lower()
    via_rank = 0 if record.applied_by == "llm" else 1
    if "security" in lower or "auth" in lower:
        kind_rank = 0
    elif "config" in lower:
        kind_rank = 1
    elif "controller" in lower:
        kind_rank = 2
    elif lower.endswith((".html", ".xhtml", ".yml", ".yaml", ".properties")):
        kind_rank = 3
    elif lower == "pom.xml":
        kind_rank = 4
    else:
        kind_rank = 5
    return (via_rank, kind_rank, file_path)


def is_pure_cascading_override_failure(
    compiler_errors: List[CompilerError], failed_tests: List[TestFailure]
) -> bool:
    if failed_tests or not compiler_errors:
        return False
    return all(
        "is not abstract and does not override abstract method" in (e.message or "")
        for e in compiler_errors
    )


def has_spring_test_profile(repo_path: Path) -> bool:
    for rel_dir in ("src/main/resources", "src/test/resources"):
        res_dir = repo_path / rel_dir
        if not res_dir.is_dir():
            continue
        for filename in _TEST_PROFILE_FILENAMES:
            if (res_dir / filename).is_file():
                return True
    return False


def insert_maven_defines(base_args: List[str], defines: List[str]) -> List[str]:
    result = list(base_args)
    insert_at = len(result)
    for idx, arg in enumerate(result):
        if arg in _MAVEN_LIFECYCLE_GOALS:
            insert_at = idx
            break
    return result[:insert_at] + list(defines) + result[insert_at:]


def resolve_sandbox_db_fallback_args(
    repo_path: Path,
    pom_has_h2: bool,
    initial_maven_args: Optional[List[str]],
    current_maven_args: Optional[List[str]],
    tried_fallbacks: Set[str],
) -> Optional[Tuple[str, str, List[str]]]:
    effective_current = (
        list(current_maven_args)
        if current_maven_args is not None
        else list(settings.sandbox_default_maven_args)
    )
    if (
        "profile_test" not in tried_fallbacks
        and has_spring_test_profile(repo_path)
        and not any(a.startswith("-Dspring.profiles.active=") for a in effective_current)
    ):
        return (
            "profile_test",
            "Activating repository's existing 'test' Spring profile (-Dspring.profiles.active=test)",
            insert_maven_defines(effective_current, ["-Dspring.profiles.active=test"]),
        )

    clean_base = (
        list(initial_maven_args)
        if initial_maven_args is not None
        else list(settings.sandbox_default_maven_args)
    )
    if (
        "h2_override" not in tried_fallbacks
        and pom_has_h2
        and not any(a.startswith("-Dspring.datasource.url=") for a in clean_base)
    ):
        h2_defines = [
            "-Dspring.datasource.url=jdbc:h2:mem:coderevive;DB_CLOSE_DELAY=-1",
            "-Dspring.datasource.driver-class-name=org.h2.Driver",
            "-Dspring.datasource.username=sa",
            "-Dspring.datasource.password=",
            "-Dspring.jpa.database-platform=org.hibernate.dialect.H2Dialect",
            "-Dspring.jpa.hibernate.ddl-auto=create-drop",
        ]
        return (
            "h2_override",
            "Applying in-memory H2 Spring Boot overrides (H2 dependency detected in pom.xml)",
            insert_maven_defines(clean_base, h2_defines),
        )

    return None
