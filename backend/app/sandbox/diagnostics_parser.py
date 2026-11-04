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

def extract_compiler_errors(raw_logs:str)->List[CompilerError]:
    lines=raw_logs.splitlines()
    errors:List[CompilerError]=[]
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

        errors.append(
            CompilerError(
                file_path=match.group("file"),
                line_number=int(match.group("line")),
                column=int(match.group("col")),
                message=" ".join(part for part in message_parts if part)
            )
        )

    return errors

_TEST_FAILURE_HEADER_RE=re.compile(
    r"^(?:\[ERROR\]\s*)?"
    r"(?P<qualified>[\w$]+(?:\.[\w$]+)*)\s+"
    r"Time elapsed:\s*[\d.,]+\s*s\s*<<<\s*(?:FAILURE|ERROR)!?\s*$"
)

_STACK_FRAME_RE=re.compile(r"^\s*(?:at\s|Caused by:|\.\.\.\s*\d+\s+more)")

def extract_test_failures(raw_logs:str)->List[TestFailure]:
    lines=raw_logs.splitlines()
    failures:List[TestFailure]=[]
    i=0
    while i<len(lines):
        match=_TEST_FAILURE_HEADER_RE.match(lines[i])
        if not match:
            i+=1
            continue

        qualified=match.group("qualified")
        test_class,_,test_method=qualified.rpartition(".")
        if not test_class:
            test_class,test_method=qualified,""

        i+=1
        message_lines:List[str]=[]
        while(
            i<len(lines)
            and lines[i].strip()
            and not _STACK_FRAME_RE.match(lines[i])
            and not _TEST_FAILURE_HEADER_RE(lines[i])
        ):
            message_lines.append(lines[i].strip())
            i+=1

        stack_lines:List[str]=[]
        while i<len(lines) and _STACK_FRAME_RE.match(lines[i]):
            stack_lines.append(lines[i].strip())
            i==1

        failures.append(
            TestFailure(
                test_class=test_class,
                test_method=test_method,
                failure_message=" ".join(message_lines) if message_lines else "(no failure message captured in logs)",
                stack_trace="\n".join(stack_lines) if stack_lines else None
            )
        )

    return failures

def parse_maven_output(raw_logs:str)->Tuple[List[CompilerError],List[TestFailure]]:
    return extract_compiler_errors(raw_logs),extract_test_failures(raw_logs)