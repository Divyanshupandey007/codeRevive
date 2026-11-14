from genericpath import isdir
from pathlib import Path
import sys
from typing import List

from git import InvalidGitRepositoryError, Repo

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.models.manifest import MigrationTarget, ProjectIngestionReport, RepoInfo
from app.ingestion.ast_parser import JavaAstParser
from app.ingestion.git_manager import GitWorkspaceManager
from app.ingestion.graph_builder import DependencyGraphBuilder
from app.ingestion.pom_parser import PomParser

class IngestionPipeline:
    def __init__(self,branch_name:str="migration/spring-boot-3"):
        self.branch_name=branch_name

    @staticmethod
    def _files_to_migrate(migration_order:List[MigrationTarget],target_path:Path=None)->List[MigrationTarget]:
        targets=[t for t in migration_order if t.matched_rules]

        pom_target=MigrationTarget(
            identifier="pom.xml",
            file_path="pom.xml",
            package_name=None,
            migration_depth=-1,
            in_degree=0,
            out_degree=0,
            is_leaf=True,
            is_test=False,
            has_javax=False,
            matched_rules=["pom-migration"]
        )

        extra_targets:List[MigrationTarget]=[]
        if target_path and Path(target_path).is_dir():
            res_dir=Path(target_path)/"src"/"main"/"resources"
            if res_dir.is_dir():
                for p in res_dir.rglob("*"):
                    if p.is_file() and p.suffix in (".html",".xml",".properties"):
                        try:
                            content=p.read_text(encoding="utf-8",errors="ignore")
                            if "javax." in content or "javax/" in content or "#request" in content:
                                rel=p.relative_to(target_path).as_posix()
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
                                        has_javax=True,
                                        matched_rules=["javax-in-resource"]
                                    )
                                )
                        except Exception:
                            pass

        return sorted(targets+[pom_target]+extra_targets,key=lambda t:t.migration_depth)

    def _repo_info_from_local_path(self,path:Path)->RepoInfo:
        try:
            repo=Repo(path)
            if self.branch_name not in repo.heads:
                repo.create_head(self.branch_name)
            repo.heads[self.branch_name].checkout()
            active_branch=repo.active_branch.name
            baseline_sha=repo.head.commit.hexsha
        except InvalidGitRepositoryError:
            active_branch="Unknown (not a git repo)"
            baseline_sha="Unknown (not a repo)"

        return RepoInfo(
            repo_url=str(path),
            target_path=str(path),
            active_branch=active_branch,
            baseline_sha=baseline_sha
        )

    def _prepare_repo(self,repo_source:str,clean:bool)->RepoInfo:
        local_path=Path(repo_source)
        if local_path.is_dir():
            return self._repo_info_from_local_path(local_path)

        result=GitWorkspaceManager().prepare_workspace(repo_url=repo_source,branch_name=self.branch_name,clean=clean)

        return RepoInfo(
            repo_url=repo_source,
            target_path=str(result["target_path"]),
            active_branch=result["active_branch"],
            baseline_sha=result["baseline_sha"]
        )

    def run(self,repo_source:str,clean:bool=False)->ProjectIngestionReport:
        repo_info=self._prepare_repo(repo_source,clean)
        target_path=Path(repo_info.target_path)

        pom=PomParser(target_path/"pom.xml").parse()

        manifests=JavaAstParser(str(target_path)).parse_repo()
        graph=DependencyGraphBuilder().build_graph(manifests)

        return ProjectIngestionReport(
            repo_info=repo_info,
            pom=pom,
            graph=graph,
            files_to_migrate=self._files_to_migrate(graph.migration_order,target_path),
            baseline_test_count=sum(m.test_count for m in manifests)
        )

if __name__=="__main__":
    report=IngestionPipeline().run(
        "https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git"
    )

    print("\n========== PROJECT INGESTION REPORT ==========")
    print(f"Repo: {report.repo_info.repo_url}")
    print(f"Branch: {report.repo_info.active_branch}")
    print(f"Baseline commit: {report.repo_info.baseline_sha}")

    print(f"\nSpring Boot: {report.pom.spring_boot_version}  ->  needs upgrade to 3.x")
    print(f"Java: {report.pom.java_version}  ->  needs upgrade to 17")

    print(f"\nGraph: {report.graph.total_nodes} nodes, {report.graph.total_edges} edges")

    print(f"\nFiles to migrate ({len(report.files_to_migrate)}), ordered by depth:")
    for target in report.files_to_migrate:
        print(f"  depth={target.migration_depth:<3} has_javax={str(target.has_javax):<5} {target.file_path}")

    print(f"\nBaseline test count: {report.baseline_test_count}  (guardrail)")

    out_path=Path(__file__).resolve().parents[2] / "workspace" / "ingestion_report.json"
    out_path.write_text(report.model_dump_json(indent=2))
    print(f"\nFull report written to: {out_path}")