from pathlib import Path
import re
import sys
from typing import List, Tuple

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.models.execution import CompilerError, TestFailure

_COMPILER_ERROR_RE=re.compile(r"^\[ERROR\]\s+(?P<file>/\S+?\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s*(?P<message>.*)$")
_CONTINUATION_RE=re.compile(r"^\s{2,}\S")

def _normalize_path(container_path:str,container_workdir:str)->str:
    prefix=container_workdir.rstrip("/")+chr(47)
    return container_path[len(prefix):] if container_path.startswith(prefix) else container_path

def extract_compiler_errors(raw_logs:str,container_workdir:str="/app")->List[CompilerError]:
    lines=raw_logs.splitlines()
    errors:List[CompilerError]=[]
    seen:set=set()

    i=0
    while i<len(lines):
        match=_COMPILER_ERROR_RE.match(lines[i])
        if not match:
            i+=1
            continue

        message_parts=[match.group("message").strip()]
        i+=1
        while i<len(lines) and _CONTINUATION_RE.match(lines[i]):
            message_parts.append(lines[i].strip())
            i+=1

        file_path=_normalize_path(match.group("file"),container_workdir)
        line_number=int(match.group("line"))
        column=int(match.group("col"))
        message=" ".join(part for part in message_parts if part)

        key=(file_path,line_number,column,message)
        if key in seen:
            continue
        seen.add(key) 

        errors.append(
            CompilerError(
                file_path=file_path,
                line_number=line_number,
                column=column,
                message=message
            )
        )

    return errors

_TEST_CLASS_HEADER_RE=re.compile(
    r"^(?:\[ERROR\]\s*)?Tests run:.*<<<\s*(?:FAILURE|ERROR)!?\s*-\s*in\s+(?P<class>[\w$.]+)"
)

_TEST_FAILURE_HEADER_RE=re.compile(
    r"^(?:\[ERROR\]\s*)?"
    r"(?P<qualified>[\w$]+(?:\.[\w$]+)*)\s+"
    r"Time elapsed:\s*[\d.,]+\s*s\s*<<<\s*(?:FAILURE|ERROR)!?\s*$"
)

_STACK_FRAME_RE=re.compile(r"^\s*(?:at\s|Caused by:|\.\.\.\s*\d+\s+more)")

def extract_test_failures(raw_logs:str)->List[TestFailure]:
    lines=raw_logs.splitlines()
    failures:List[TestFailure]=[]
    current_test_class=""
    i=0
    while i<len(lines):
        class_match=_TEST_CLASS_HEADER_RE.match(lines[i])
        if class_match:
            current_test_class=class_match.group("class")
            i+=1
            continue

        match=_TEST_FAILURE_HEADER_RE.match(lines[i])
        if not match:
            i+=1
            continue

        qualified=match.group("qualified")
        test_class,_,test_method=qualified.rpartition(".")
        if not test_class:
            test_class=current_test_class or qualified
            test_method=qualified

        i+=1
        message_lines:List[str]=[]
        while(
            i<len(lines)
            and lines[i].strip()
            and not _STACK_FRAME_RE.match(lines[i])
            and not _TEST_FAILURE_HEADER_RE.match(lines[i])
        ):
            message_lines.append(lines[i].strip())
            i+=1

        stack_lines:List[str]=[]
        while i<len(lines) and _STACK_FRAME_RE.match(lines[i]):
            stack_lines.append(lines[i].strip())
            i+=1

        failures.append(
            TestFailure(
                test_class=test_class,
                test_method=test_method,
                failure_message=" ".join(message_lines) if message_lines else "(no failure message captured in logs)",
                stack_trace="\n".join(stack_lines) if stack_lines else None
            )
        )

    return failures

_FALLBACK_ERROR_LINE_RE=re.compile(r"^\s*\[(?:ERROR|FATAL)\]\s*(?P<message>.+?)\s*$")

_POM_FAILURE_HINTS=(
    "pom.xml","resolve dependencies","resolve artifact","could not find artifact",
    "non-resolvable parent pom","malformed pom","failed to read artifact descriptor",
    "could not transfer artifact","dependencyresolutionexception","unresolvable build extension"
)

def extract_fallback_errors(raw_logs:str)->List[CompilerError]:
    errors:List[CompilerError]=[]
    seen:set=set()
    for line in raw_logs.splitlines():
        match=_FALLBACK_ERROR_LINE_RE.match(line)
        if not match:
            continue
        message=match.group("message").strip()
        if not message or message in seen:
            continue
        seen.add(message)
        lower=message.lower()

        file_path="pom.xml" if any(hint in lower for hint in _POM_FAILURE_HINTS) else None
        errors.append(CompilerError(
            file_path=file_path,
            line_number=None,
            column=None,
            message=message
        ))

    return errors

def parse_maven_output(raw_logs:str,container_workdir="/app")->Tuple[List[CompilerError],List[TestFailure]]:
    compiler_errors=extract_compiler_errors(raw_logs,container_workdir)
    failed_tests=extract_test_failures(raw_logs)
    if not compiler_errors and not failed_tests:
        compiler_errors=extract_fallback_errors(raw_logs)
    return compiler_errors,failed_tests