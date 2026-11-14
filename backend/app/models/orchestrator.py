from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

class WorkflowState(str,Enum):
    INITIALIZING="INITIALIZING"
    MIGRATING_FILE="MIGRATING_FILE"
    GENERATING_PATCH="GENERATING_PATCH"
    VERIFYING_SANDBOX="VERIFYING_SANDBOX"
    REPAIRING="REPAIRING"
    COMMITING="COMMITING"
    POST_INTEGRITY_CHECK="POST_INTEGRITY_CHECK"
    COMPLETED="COMPLETED"
    FAILED="FAILED"

class FileStatus(str,Enum):
    SUCCESS="SUCCESS"
    FAILED="FAILED"
    SKIPPED="SKIPPED"

class FileMigrationStatus(BaseModel):
    file_path:str
    attempts:int=0
    status:FileStatus
    commit_sha:Optional[str]=None
    errors_encountered:List[str]=Field(default_factory=list)

class MigrationRunSummary(BaseModel):
    repo_url:str
    baseline_commit:str
    final_commit:Optional[str]=None
    total_files_migrated:int=0
    total_retries:int=0
    file_statuses:List[FileMigrationStatus]=Field(default_factory=list)
    integrity_passed:bool=False
    total_duration_seconds:float=0.0
    final_state:WorkflowState=WorkflowState.INITIALIZING
    integrity_violations:List[str]=Field(default_factory=list)
    generated_at:str=Field(default_factory=lambda:datetime.now(timezone.utc).isoformat())