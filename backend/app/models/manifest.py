from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class MavenDependency(BaseModel):
    group_id: str
    artifact_id: str
    version: Optional[str] = None
    scope: Optional[str] = None


class PomManifest(BaseModel):
    spring_boot_version: Optional[str] = None
    parent_group_id: Optional[str] = None
    parent_artifact_id: Optional[str] = None
    java_version: Optional[str] = None
    packaging: Optional[str] = None
    is_multi_module: bool = False
    modules: List[str] = Field(default_factory=list)
    properties: Dict[str, str] = Field(default_factory=dict)
    repositories: List[str] = Field(default_factory=list)
    dependencies: List[MavenDependency] = Field(default_factory=list)


class JavaClassManifest(BaseModel):
    name: Optional[str] = None
    extends: List[str] = Field(default_factory=list)
    implements: List[str] = Field(default_factory=list)


class JavaMethodManifest(BaseModel):
    name: Optional[str] = None
    return_type: Optional[str] = None
    parameters: List[str] = Field(default_factory=list)
    modifiers: List[str] = Field(default_factory=list)
    annotations: List[str] = Field(default_factory=list)
    is_test: bool = False
    is_public: bool = False
    has_assertions_or_body: bool = True

    def signature_key(self, file_path: str) -> str:
        params = ",".join(self.parameters)
        ret = self.return_type or "void"
        return f"{file_path}#{ret} {self.name}({params})"


class JavaFileManifest(BaseModel):
    file_path: str
    package_name: Optional[str] = None
    imports: List[str] = Field(default_factory=list)
    javax_imports: List[str] = Field(default_factory=list)
    jakarta_migratable_imports: List[str] = Field(default_factory=list)
    classes: List[JavaClassManifest] = Field(default_factory=list)
    methods: List[JavaMethodManifest] = Field(default_factory=list)
    test_count: int = 0
    has_deprecated_pbkdf2_constructor: Optional[bool] = None
    has_deprecated_spring_assert: Optional[bool] = None


class MigrationTarget(BaseModel):
    identifier: str
    file_path: str
    package_name: Optional[str] = None
    migration_depth: int
    in_degree: int
    out_degree: int
    is_leaf: bool
    is_test: bool
    has_javax: bool
    matched_rules: List[str] = Field(default_factory=list)


class BlastRadiusEntry(BaseModel):
    identifier: str
    file_path: str
    in_degree: int
    affected_count: int
    affected_identifiers: List[str]


class DependencyGraphManifest(BaseModel):
    total_nodes: int
    total_edges: int
    unresolved_import_count: int
    migration_order: List[MigrationTarget]
    high_blast_radius: List[BlastRadiusEntry]


class AdditionalArtifactSpec(BaseModel):
    group_id: str
    artifact_id: str
    version: Optional[str] = None
    classifier: Optional[str] = None
    exclusions: List[Tuple[str, str]] = Field(default_factory=list)


class DependencyMigrationAction(BaseModel):
    group_id: str
    artifact_id: str
    current_version: Optional[str] = None
    target_strategy: str
    target_group_id: Optional[str] = None
    target_artifact_id: Optional[str] = None
    target_version: Optional[str] = None
    target_classifier: Optional[str] = None
    remove_version: bool = False
    exclusions: List[Tuple[str, str]] = Field(default_factory=list)
    additional_artifacts: List[AdditionalArtifactSpec] = Field(default_factory=list)
    notes: str = ""


class MigrationPlan(BaseModel):
    source_spring_boot: Optional[str] = None
    target_spring_boot: str
    source_java: Optional[str] = None
    target_java: str = "17"
    dependencies: List[DependencyMigrationAction] = Field(default_factory=list)
    properties_to_remove: List[str] = Field(default_factory=list)
    properties_to_update: Dict[str, str] = Field(default_factory=dict)
    repositories_to_remove: List[str] = Field(default_factory=list)
    openrewrite_recipes: List[str] = Field(default_factory=list)


class RepoInfo(BaseModel):
    repo_url: str
    target_path: str
    active_branch: str
    baseline_sha: str
    isolated_workspace: bool = False


class ProjectIngestionReport(BaseModel):
    repo_info: RepoInfo
    pom: PomManifest
    graph: DependencyGraphManifest
    files_to_migrate: List[MigrationTarget]
    baseline_test_count: int
    migration_plan: Optional[MigrationPlan] = None