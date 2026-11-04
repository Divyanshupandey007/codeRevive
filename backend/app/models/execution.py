from typing import List, Optional

from pydantic import BaseModel, Field


class CompilerError(BaseModel):
    file_path:Optional[str]=None
    line_number:Optional[str]=None
    column:Optional[int]=None
    message:str

class TestFailure(BaseModel):
    test_class:str
    test_method:str
    failure_message:str
    stack_trace:Optional[str]=None

class BuildResult(BaseModel):
    success:bool
    exit_code:int
    duration_seconds:float
    compiler_errors:List[CompilerError]=Field(default_factory=list)
    failed_tests:List[TestFailure]=Field(default_factory=list)
    raw_logs:str