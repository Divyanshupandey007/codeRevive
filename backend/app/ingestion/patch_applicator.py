import difflib
from pathlib import Path
from typing import Optional, Tuple

from git import Repo

from app.guardrails.pre_execution import validate_patch_safety
from app.models.agent import FilePatchResult


def apply_patch_with_diff(
    repo_path: Path,
    patch: FilePatchResult,
    expected_target_path: Optional[str] = None,
) -> Tuple[Path, str]:
    repo_path = Path(repo_path)
    validate_patch_safety(repo_path, patch, expected_target_path=expected_target_path)

    target_file = (repo_path.resolve() / patch.file_path).resolve()
    original_content = (
        target_file.read_text(encoding="utf-8") if target_file.is_file() else ""
    )

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(patch.new_content, encoding="utf-8")

    diff = "".join(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            patch.new_content.splitlines(keepends=True),
            fromfile=f"a/{patch.file_path}",
            tofile=f"b/{patch.file_path}",
        )
    )
    return target_file, diff


def commit_patch(
    repo_path: Path, commit_message: str, file_path: Optional[str] = None
) -> str:
    repo = Repo(repo_path)

    if file_path is not None:
        repo.index.add([file_path])
    else:
        repo.git.add(A=True)

    commit = repo.index.commit(commit_message)
    return commit.hexsha