from pathlib import Path
from typing import List, Optional

from git import InvalidGitRepositoryError, Repo

from app.ingestion.ast_parser import JavaAstParser
from app.ingestion.git_manager import GitWorkspaceManager
from app.ingestion.graph_builder import DependencyGraphBuilder
from app.ingestion.migration_planner import build_migration_plan
from app.ingestion.pom_parser import PomParser
from app.models.manifest import MigrationTarget, ProjectIngestionReport, RepoInfo

_BOOT2_RESOURCE_PATTERNS = (
    "javax.",
    "javax/",
    "#request",
    "#session",
    "#servletContext",
    "spring.redis.",
    "spring.zipkin.",
    "spring.jpa.hibernate.use-new-id-generator-mappings",
    "spring.mvc.pathmatch.matching-strategy",
)

_BOOT2_YAML_KEYS = (
    "redis:",
    "zipkin:",
    "use-new-id-generator-mappings:",
    "matching-strategy:",
)


def _resource_needs_migration(path: Path, content: str) -> bool:
    if any(pat in content for pat in _BOOT2_RESOURCE_PATTERNS):
        return True
    if path.suffix.lower() in (".yml", ".yaml"):
        return any(key in content for key in _BOOT2_YAML_KEYS)
    return False


class UnsupportedRepositoryError(ValueError):
    """Raised when a repository structure is outside the supported single-module Maven MVP."""


class IngestionPipeline:
    def __init__(
        self,
        branch_name: str = "migration/spring-boot-3",
        workspace_root: Optional[Path] = None,
        isolate_local: bool = True,
        target_spring_boot: Optional[str] = None,
        target_java: Optional[str] = None,
    ):
        self.branch_name = branch_name
        self.workspace_root = workspace_root
        self.isolate_local = isolate_local
        self.target_spring_boot = target_spring_boot
        self.target_java = target_java

    @staticmethod
    def _files_to_migrate(
        migration_order: List[MigrationTarget], target_path: Optional[Path] = None
    ) -> List[MigrationTarget]:
        targets = [t for t in migration_order if t.matched_rules]

        pom_target = MigrationTarget(
            identifier="pom.xml",
            file_path="pom.xml",
            package_name=None,
            migration_depth=-1,
            in_degree=0,
            out_degree=0,
            is_leaf=True,
            is_test=False,
            has_javax=False,
            matched_rules=["pom-migration"],
        )

        extra_targets: List[MigrationTarget] = []
        if target_path and Path(target_path).is_dir():
            res_dir = Path(target_path) / "src" / "main" / "resources"
            if res_dir.is_dir():
                for p in sorted(res_dir.rglob("*")):
                    if p.is_file() and p.suffix.lower() in (
                        ".html",
                        ".xhtml",
                        ".xml",
                        ".properties",
                        ".yml",
                        ".yaml",
                    ):
                        try:
                            content = p.read_text(encoding="utf-8", errors="ignore")
                            if _resource_needs_migration(p, content):
                                rel = p.relative_to(target_path).as_posix()
                                extra_targets.append(
                                    MigrationTarget(
                                        identifier=rel,
                                        file_path=rel,
                                        package_name=None,
                                        migration_depth=0,
                                        in_degree=0,
                                        out_degree=0,
                                        is_leaf=True,
                                        is_test=False,
                                        has_javax=("javax." in content or "javax/" in content),
                                        matched_rules=["resource-migration"],
                                    )
                                )
                        except OSError:
                            pass

        return sorted(targets + [pom_target] + extra_targets, key=lambda t: t.migration_depth)

    def _repo_info_from_local_path(self, path: Path, clean: bool) -> RepoInfo:
        manager = GitWorkspaceManager(workspace_root=self.workspace_root)
        if self.isolate_local:
            result = manager.prepare_local_workspace(
                source_path=path,
                branch_name=self.branch_name,
                clean=clean,
            )
            return RepoInfo(
                repo_url=str(path),
                target_path=str(result["target_path"]),
                active_branch=result["active_branch"],
                baseline_sha=result["baseline_sha"],
                isolated_workspace=True,
            )

        try:
            repo = Repo(path)
            if self.branch_name not in repo.heads:
                repo.create_head(self.branch_name)
            repo.heads[self.branch_name].checkout()
            active_branch = repo.active_branch.name
            baseline_sha = repo.head.commit.hexsha
        except InvalidGitRepositoryError:
            active_branch = "Unknown (not a git repo)"
            baseline_sha = "Unknown (not a repo)"

        return RepoInfo(
            repo_url=str(path),
            target_path=str(path),
            active_branch=active_branch,
            baseline_sha=baseline_sha,
            isolated_workspace=False,
        )

    def _prepare_repo(self, repo_source: str, clean: bool) -> RepoInfo:
        local_path = Path(repo_source)
        if local_path.is_dir():
            return self._repo_info_from_local_path(local_path, clean=clean)

        result = GitWorkspaceManager(workspace_root=self.workspace_root).prepare_workspace(
            repo_url=repo_source, branch_name=self.branch_name, clean=clean
        )

        return RepoInfo(
            repo_url=repo_source,
            target_path=str(result["target_path"]),
            active_branch=result["active_branch"],
            baseline_sha=result["baseline_sha"],
            isolated_workspace=True,
        )

    def run(self, repo_source: str, clean: bool = False) -> ProjectIngestionReport:
        repo_info = self._prepare_repo(repo_source, clean)
        target_path = Path(repo_info.target_path)

        pom = PomParser(target_path / "pom.xml").parse()
        if pom.is_multi_module:
            raise UnsupportedRepositoryError(
                f"Multi-module Maven projects ({', '.join(pom.modules)}) are not supported in this MVP. "
                "Only single-module Maven Spring Boot repositories are supported."
            )

        migration_plan = build_migration_plan(
            pom,
            target_spring_boot=self.target_spring_boot,
            target_java=self.target_java,
        )

        manifests = JavaAstParser(str(target_path)).parse_repo()
        graph = DependencyGraphBuilder().build_graph(manifests)

        drops_springfox = any(
            d.group_id == "io.springfox"
            and d.target_strategy in ("upgrade_artifact", "remove_artifact")
            for d in migration_plan.dependencies
        )
        has_explicit_guava = any(
            d.group_id == "com.google.guava" and d.artifact_id == "guava"
            for d in pom.dependencies
        )
        if drops_springfox and not has_explicit_guava:
            guava_paths = {
                m.file_path
                for m in manifests
                if any(imp.startswith("com.google.common.") for imp in m.imports)
            }
            for target in graph.migration_order:
                if target.file_path in guava_paths and "guava-transitive-removal" not in target.matched_rules:
                    target.matched_rules.append("guava-transitive-removal")

        return ProjectIngestionReport(
            repo_info=repo_info,
            pom=pom,
            graph=graph,
            files_to_migrate=self._files_to_migrate(graph.migration_order, target_path),
            baseline_test_count=sum(m.test_count for m in manifests),
            migration_plan=migration_plan,
        )