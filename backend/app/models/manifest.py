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

class JavaClassManifest(BaseModel):
    name:Optional[str]
    extends: List[str] = Field(default_factory=list)
    implements:List[str]=Field(default_factory=list)

class JavaMethodManifest(BaseModel):
    name: Optional[str] = None
    is_test:bool=False

class JavaFileManifest(BaseModel):
    file_path:str
    package_name: Optional[str] = None
    imports:List[str]
    javax_imports:List[str]
    classes:List[JavaClassManifest]
    methods: List[JavaMethodManifest] = Field(default_factory=list)
    test_count:int

class MigrationTarget(BaseModel):
    identifier:str
    file_path:str
    package_name:Optional[str]=None
    migration_depth:int
    in_degree:int
    out_degree:int
    is_leaf:bool
    is_test:bool
    has_javax:bool

class BlastRadiusEntry(BaseModel):
    identifier:str
    file_path:str
    in_degree:int
    affected_count:int
    affected_identifiers:List[str]

class DependencyGraphManifest(BaseModel):
    total_nodes:int
    total_edges:int
    unresolved_import_count:int
    migration_order:List[MigrationTarget]
    high_blast_radius:List[BlastRadiusEntry]