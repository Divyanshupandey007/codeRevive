from pathlib import Path, PurePosixPath
from typing import List, Optional

from app.config import settings
from app.models.agent import FilePatchResult


class SecurityViolationError(Exception):
    """Raised when a patch fails a pre-execution safety check."""


def _normalize_rel_path(file_path: str) -> str:
    return PurePosixPath(file_path.replace("\\", "/")).as_posix().lstrip("./")


def _check_expected_target(
    patch_file_path: str, expected_target_path: Optional[str]
) -> None:
    if expected_target_path is None:
        return
    normalized_patch = _normalize_rel_path(patch_file_path)
    normalized_expected = _normalize_rel_path(expected_target_path)
    if normalized_patch != normalized_expected:
        raise SecurityViolationError(
            f"LLM attempted to modify an unexpected file: got {patch_file_path!r}, "
            f"expected {expected_target_path!r}."
        )


def _check_path_traversal(repo_path: Path, file_path: str) -> Path:
    if Path(file_path).is_absolute():
        raise SecurityViolationError(
            f"Patch targets absolute path {file_path!r} outside repository root. Refusing to apply"
        )
    repo_root = repo_path.resolve()
    resolved_target = (repo_root / file_path).resolve()

    try:
        resolved_target.relative_to(repo_root)
    except ValueError:
        raise SecurityViolationError(
            f"Patch targets {file_path} which resolves to {resolved_target} "
            f"outside the repository root {repo_root}. Refusing to apply"
        )

    return resolved_target


def _check_forbidden_patterns(
    new_content: str,
    file_path: str,
    forbidden_patterns: Optional[List[str]] = None,
) -> None:
    if not new_content or not new_content.strip():
        raise SecurityViolationError(
            f"Patch to {file_path} produced empty content. Refusing to apply"
        )
    stripped = new_content.lstrip()
    if stripped.startswith("```"):
        raise SecurityViolationError(
            f"Patch to {file_path} contains markdown code fences. Refusing to apply"
        )

    patterns = (
        settings.guardrail_forbidden_patterns
        if forbidden_patterns is None
        else forbidden_patterns
    )
    hits = [pattern for pattern in patterns if pattern in new_content]
    if hits:
        raise SecurityViolationError(
            f"Patch to {file_path} contains forbidden pattern(s): {hits}. Refusing to apply"
        )


def validate_patch_safety(
    repo_path: Path,
    patch: FilePatchResult,
    expected_target_path: Optional[str] = None,
) -> None:
    repo_path = Path(repo_path)
    _check_expected_target(patch.file_path, expected_target_path)
    _check_path_traversal(repo_path, patch.file_path)
    _check_forbidden_patterns(patch.new_content, patch.file_path)