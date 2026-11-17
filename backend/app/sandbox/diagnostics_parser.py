import re
from typing import List, Optional, Tuple

from app.models.execution import CompilerError, TestFailure

_COMPILER_ERROR_RE = re.compile(
    r"^\[ERROR\]\s+(?P<file>(?:/|[A-Za-z]:[\\/])\S+?\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s*(?P<message>.*)$"
)
_CONTINUATION_RE = re.compile(r"^\s{2,}\S")

_ENVIRONMENT_FAILURE_PATTERNS = (
    "connection to localhost:5432 refused",
    "connection to localhost:3306 refused",
    "connection to localhost:6379 refused",
    "connection to localhost:27017 refused",
    "communications link failure",
    "cannot get jdbc connection",
    "unable to acquire jdbc connection",
    "redisconnectionfailureexception",
    "mongosocketopenexception",
    "could not transfer artifact",
    "unknownhostexception",
    "connection timed out",
    "no space left on device",
    "temporary failure in name resolution",
    "failed to start container",
    "docker daemon",
    "sandbox command timed out",
    "sandbox container was killed",
    "sandbox container received sigterm",
    "outofmemoryerror",
    "java heap space",
    "gc overhead limit exceeded",
)

_DB_UNCONDITIONAL_PATTERNS = (
    "connection to localhost:5432 refused",
    "connection to localhost:3306 refused",
    "connection to localhost:27017 refused",
    "communications link failure",
    "cannot get jdbc connection",
    "unable to acquire jdbc connection",
)

_DB_CONTEXT_HINTS = (
    "jdbc",
    "mysql",
    "postgresql",
    "postgres",
    "hikari",
    "datasource",
    "communicationsexception",
    "redis",
    "mongo",
    ":3306",
    ":5432",
    ":6379",
    ":27017",
)


def is_database_environment_failure(
    raw_logs: str, failed_tests: List[TestFailure] | None = None
) -> bool:
    """Return True when build logs/tests show an unreachable external database service."""
    combined_parts = [raw_logs or ""]
    for test in failed_tests or []:
        combined_parts.append(test.failure_message or "")
        combined_parts.append(test.stack_trace or "")
    lower = "\n".join(combined_parts).lower()

    if any(pat in lower for pat in _DB_UNCONDITIONAL_PATTERNS):
        return True
    if "connection refused" in lower and any(hint in lower for hint in _DB_CONTEXT_HINTS):
        return True
    return False


def _normalize_path(container_path: str, container_workdir: str) -> str:
    normalized = container_path.replace("\\", "/")
    prefix = container_workdir.rstrip("/") + "/"
    if normalized.startswith(prefix):
        return normalized[len(prefix):]
    return normalized


def extract_compiler_errors(
    raw_logs: str, container_workdir: str = "/app"
) -> List[CompilerError]:
    lines = raw_logs.splitlines()
    errors: List[CompilerError] = []
    seen: set = set()

    i = 0
    while i < len(lines):
        match = _COMPILER_ERROR_RE.match(lines[i])
        if not match:
            i += 1
            continue

        message_parts = [match.group("message").strip()]
        i += 1
        while i < len(lines) and _CONTINUATION_RE.match(lines[i]):
            message_parts.append(lines[i].strip())
            i += 1

        file_path = _normalize_path(match.group("file"), container_workdir)
        line_number = int(match.group("line"))
        column = int(match.group("col"))
        message = " ".join(part for part in message_parts if part)

        key = (file_path, line_number, column, message)
        if key in seen:
            continue
        seen.add(key)

        errors.append(
            CompilerError(
                file_path=file_path,
                line_number=line_number,
                column=column,
                message=message,
            )
        )

    return errors


# Supports both Surefire 2.x (`<<< FAILURE! - in ClassName`) and Surefire 3.x (`-- in ClassName`)
_TEST_CLASS_HEADER_RE = re.compile(
    r"^(?:\[ERROR\]\s*)?Tests run:.*?(?:<<<\s*(?:FAILURE|ERROR)!?\s*-\s*in|--\s*in)\s+(?P<class>[\w$.]+)"
)

# Supports both Surefire 2.x (`method  Time elapsed: ...`) and Surefire 3.x (`Class.method -- Time elapsed: ...`)
_TEST_FAILURE_HEADER_RE = re.compile(
    r"^(?:\[ERROR\]\s*)?"
    r"(?P<qualified>[\w$]+(?:\.[\w$]+)*(?:\([^)]*\))?(?:\[\d+\])?)\s+"
    r"(?:--\s+)?Time elapsed:\s*[\d.,]+\s*s\s*<<<\s*(?:FAILURE|ERROR)!?\s*$"
)

_STACK_FRAME_RE = re.compile(r"^\s*(?:at\s|Caused by:|\.\.\.\s*\d+\s+more)")


def extract_test_failures(raw_logs: str) -> List[TestFailure]:
    lines = raw_logs.splitlines()
    failures: List[TestFailure] = []
    current_test_class = ""
    i = 0
    while i < len(lines):
        class_match = _TEST_CLASS_HEADER_RE.match(lines[i])
        if class_match:
            current_test_class = class_match.group("class")
            i += 1
            continue

        match = _TEST_FAILURE_HEADER_RE.match(lines[i])
        if not match:
            i += 1
            continue

        qualified = match.group("qualified")
        # Strip optional JUnit5 parameter signature e.g. `testFoo(String)[1]` before splitting class/method
        base_qualified = qualified.split("(", 1)[0].split("[", 1)[0]
        test_class, _, test_method = base_qualified.rpartition(".")
        if not test_class:
            test_class = current_test_class or base_qualified
            test_method = qualified

        i += 1
        message_lines: List[str] = []
        while (
            i < len(lines)
            and lines[i].strip()
            and not _STACK_FRAME_RE.match(lines[i])
            and not _TEST_FAILURE_HEADER_RE.match(lines[i])
            and not _TEST_CLASS_HEADER_RE.match(lines[i])
        ):
            message_lines.append(lines[i].strip())
            i += 1

        stack_lines: List[str] = []
        while i < len(lines) and _STACK_FRAME_RE.match(lines[i]):
            stack_lines.append(lines[i].strip())
            i += 1

        failures.append(
            TestFailure(
                test_class=test_class,
                test_method=test_method,
                failure_message=" ".join(message_lines)
                if message_lines
                else "(no failure message captured in logs)",
                stack_trace="\n".join(stack_lines) if stack_lines else None,
            )
        )

    return failures


_FALLBACK_ERROR_LINE_RE = re.compile(
    r"^\s*\[(?:ERROR|FATAL)\]\s*(?P<message>.+?)\s*$"
)

_POM_FAILURE_HINTS = (
    "pom.xml",
    "resolve dependencies",
    "resolve artifact",
    "could not find artifact",
    "non-resolvable parent pom",
    "non-resolvable import pom",
    "malformed pom",
    "non-parseable pom",
    "while processing the poms",
    "dependencies.dependency.version",
    "dependency.version",
    "projectbuildingexception",
    "failed to read artifact descriptor",
    "could not transfer artifact",
    "dependencyresolutionexception",
    "unresolvable build extension",
    "failed to execute goal",
    "invalid target release",
    "unsupported class file major version",
    "unable to parse configuration of mojo",
    "pluginexecutionexception",
)


def extract_fallback_errors(raw_logs: str) -> List[CompilerError]:
    errors: List[CompilerError] = []
    seen: set = set()
    for line in raw_logs.splitlines():
        match = _FALLBACK_ERROR_LINE_RE.match(line)
        if not match:
            continue
        message = match.group("message").strip()
        if not message or message in seen:
            continue
        seen.add(message)
        lower = message.lower()

        file_path = (
            "pom.xml" if any(hint in lower for hint in _POM_FAILURE_HINTS) else None
        )
        errors.append(
            CompilerError(
                file_path=file_path,
                line_number=None,
                column=None,
                message=message,
            )
        )

    return errors


def classify_build_failure(
    raw_logs: str,
    compiler_errors: List[CompilerError],
    failed_tests: List[TestFailure],
    exit_code: Optional[int] = None,
) -> str:
    """Distinguish 'environment' failures (DB/network/timeout/OOM/infra) from 'code' failures."""
    if compiler_errors and any(
        e.file_path and e.file_path.endswith(".java") for e in compiler_errors
    ):
        return "code"

    if exit_code in (124, 137, 143):
        return "environment"

    lower_logs = raw_logs.lower()
    if any(pattern in lower_logs for pattern in _ENVIRONMENT_FAILURE_PATTERNS):
        return "environment"

    if failed_tests:
        for test in failed_tests:
            msg = f"{test.failure_message} {test.stack_trace or ''}".lower()
            if any(pattern in msg for pattern in _ENVIRONMENT_FAILURE_PATTERNS):
                return "environment"
        return "code"

    return "code"


def parse_maven_output(
    raw_logs: str, container_workdir: str = "/app"
) -> Tuple[List[CompilerError], List[TestFailure]]:
    compiler_errors = extract_compiler_errors(raw_logs, container_workdir)
    failed_tests = extract_test_failures(raw_logs)
    if not compiler_errors and not failed_tests:
        compiler_errors = extract_fallback_errors(raw_logs)
    return compiler_errors, failed_tests