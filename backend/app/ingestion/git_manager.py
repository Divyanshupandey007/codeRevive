from pathlib import Path
import shutil
from git import Repo

class GitWorkspaceManager:
    def __init__(self,workspace_root:Path | None=None):
        self.workspace_root=workspace_root or (Path(__file__).resolve().parents[2] / "workspace")
        self.workspace_root.mkdir(parents=True,exist_ok=True)

    def prepare_workspace(self,repo_url:str,branch_name:str= "migration/spring-boot-3",clean:bool=False)->dict:
        repo_name=repo_url.rstrip("/").split("/")[-1].replace(".git","")
        target_path=self.workspace_root / repo_name

        if target_path.exists() and clean:
            shutil.rmtree(target_path)

        if not target_path.exists():
            cloned_repo=Repo.clone_from(repo_url,target_path)
        else:
            cloned_repo=Repo(target_path)

        baseline_sha=cloned_repo.head.commit.hexsha

        if branch_name not in cloned_repo.heads:
            migration_branch=cloned_repo.create_head(branch_name)
        else:
            migration_branch=cloned_repo.heads[branch_name]

        migration_branch.checkout()

        return{
            "target_path":target_path,
            "baseline_sha":baseline_sha,
            "active_branch":cloned_repo.active_branch.name
        }

if __name__=="__main__":
    manager=GitWorkspaceManager()
    result=manager.prepare_workspace(
        repo_url="https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git"
    )
    print("Workspace ready")
    print(f"Path: {result["target_path"]}")
    print(f"Baseline commit: {result["baseline_sha"]}")
    print(f"Active branch: {result["active_branch"]}")