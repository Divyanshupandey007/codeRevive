from pathlib import Path
from git import Repo

repo_url="https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git"
workspace_root=Path(r"D:/Projects/codeRevive/backend/workspace")
target_path=workspace_root / "mybatis-spring-boot-jpetstore"

workspace_root.mkdir(parents=True,exist_ok=True)

if target_path.exists():
    raise FileExistsError(f"Workspace already eists.")

cloned_repo=Repo.clone_from(repo_url,target_path)
baseline_sha=cloned_repo.head.commit.hexsha
print(baseline_sha)