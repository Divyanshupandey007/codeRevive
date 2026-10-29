from typing import Optional,List,Dict
from pydantic import BaseModel, Field

class MavenDependency(BaseModel):
    group_id:str
    artifact_id:str
    version: Optional[str]=None

class PomManifest(BaseModel):
    spring_boot_version: Optional[str]=None
    java_version: Optional[str]=None
    dependencies: List[MavenDependency]=Field(default_factory=list)