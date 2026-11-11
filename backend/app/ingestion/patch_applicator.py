from pathlib import Path
import sys
from typing import Optional

from git import GitCommandError, Repo

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.guardrails.pre_execution import validate_patch_safety
from app.models.agent import FilePatchResult

def apply_patch(repo_path:Path,patch:FilePatchResult)->Path:
    repo_path=Path(repo_path)
    validate_patch_safety(repo_path,patch)

    target_file=(repo_path.resolve()/patch.file_path).resolve()
    target_file.parent.mkdir(parents=True,exist_ok=True)
    target_file.write_text(patch.new_content,encoding="utf-8")

    return target_file

def commit_patch(repo_path:Path,commit_message:str,file_path:Optional[str]=None)->str:
    repo=Repo(repo_path)

    if file_path is not None:
        repo.index.add([file_path])
    else:
        repo.git.add(A=True)

    commit=repo.index.commit(commit_message)
    return commit.hexsha

def rollback_patch(repo_path:Path,file_path:str)->None:
    repo=Repo(repo_path)
    try:
        repo.git.checkout("--",file_path)
    except GitCommandError as exc:
        raise RuntimeError(f"Could not roll back {file_path}: {exc}") from exc