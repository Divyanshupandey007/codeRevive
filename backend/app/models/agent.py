from typing import Optional

from pydantic import BaseModel


class FilePatchResult(BaseModel):
    file_path:str
    original_content_snippet:Optional[str]=None
    new_content:str
    explanation:str

class MigrationStepPlan(BaseModel):
    step_number:int
    target_file:str
    action:str
    instructions:str