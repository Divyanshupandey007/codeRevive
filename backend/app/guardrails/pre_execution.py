from pathlib import Path
import sys
from typing import List, Optional

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.models.agent import FilePatchResult
from app.config import settings

class SecurityViolationError(Exception):
    """Raised when a patch fails a pre-execution safety check."""

def _check_path_traversal(repo_path:Path,file_path:str)->Path:
    repo_root=repo_path.resolve()
    resolved_target=(repo_root/file_path).resolve()

    try:
        resolved_target.relative_to(repo_root)
    except ValueError:
        raise SecurityViolationError(
            f"Patch targets {file_path} which resolves to {resolved_target} "
            f"outside the repository root {repo_root}. Refusing to apply"
        )

    return resolved_target

def _check_forbidden_patterns(new_content:str,file_path:str,forbidden_patterns:Optional[List[str]]=None)->None:
    patterns=settings.guardrail_forbidden_patterns if forbidden_patterns is None else forbidden_patterns
    hits=[pattern for pattern in patterns if pattern in new_content]
    if hits:
        raise SecurityViolationError(
            f'Patch to {file_path} contains forbidden pattern(s): {hits}. Refusing to apply'
        )

def validate_patch_safety(repo_path:Path,patch:FilePatchResult)->None:
    repo_path=Path(repo_path)
    _check_path_traversal(repo_path,patch.file_path)
    _check_forbidden_patterns(patch.new_content,patch.file_path)