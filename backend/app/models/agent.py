from pydantic import BaseModel


class FilePatchResult(BaseModel):
    file_path: str
    new_content: str
    explanation: str