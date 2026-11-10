from pathlib import Path
import sys
from typing import List, Optional

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.models.execution import CompilerError, TestFailure

CODER_AGENT_SYSTEM_INSTRUCTION=(
    "You are an expert Spring Boot 3 migration engineer. You modernize legacy "
    "Java/Spring codebases (Spring Boot 2.x -> 3.x, Java 8/11 -> 17, "
    "javax.* -> jakarta.*) one file at a time.\n\n"
    "Rules you must never break:\n"
    "1. Never delete or weaken existing business logic. Only change what the "
    "migration rule below explicitly asks for.\n"
    "2. Never remove an @Test annotation, a test method, or test assertions.\n"
    "3. Only migrate the Java packages/dependencies specified in the task - "
    "leave every other import, annotation, and line of code untouched.\n"
    "4. Return the ENTIRE updated file content, not a diff, a snippet, or an "
    "excerpt - the caller writes new_content directly to disk as the whole file.\n"
    "5. Never wrap new_content in markdown code fences (```java, ```) - it must "
    "be raw source code, nothing else."
)

def _format_diagnostics(compiler_errors:List[CompilerError],failed_tests:List[TestFailure])->str:
    lines=[]
    for error in compiler_errors:
        location=f"{error.file_path}:{error.line_number}:{error.column}" if error.file_path else "(unknown location)"
        lines.append(f"[COMPILER ERROR] {location} - {error.message}")
    for failure in failed_tests:
        lines.append(f"[TEST FAILURE] {failure.test_class}.{failure.test_method} - {failure.failure_message}")

    return "\n".join(lines)

def build_coder_prompt(
        target_file_path:str,
        source_code:str,
        migration_rule:str,
        compiler_errors:Optional[List[CompilerError]]=None,
        failed_tests:Optional[List[TestFailure]]=None
)->str:
    sections=[
        f"Target file: {target_file_path}",
        f"Migration rule: {migration_rule}",
        f"Current source_code:\n```java\n{source_code}\n```"
    ]

    if compiler_errors or failed_tests:
        diagnostics=_format_diagnostics(compiler_errors or [],failed_tests or [])
        sections.append(
            "This is a REPAIR attempt: the previous patch to this file produced "
            "the diagnostics below when built in the sandbox. Fix them without "
            "violating the migration rule or any rule in the system instruction:\n"

            f"{diagnostics}"
        )

    return "\n\n".join(sections)