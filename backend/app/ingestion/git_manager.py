from pathlib import Path
import os
import re
import shutil
import stat
import time
import uuid
from git import InvalidGitRepositoryError, Repo

from app.config import settings

_UNSAFE_REPO_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_repo_name(repo_url: str) -> str:
    raw = repo_url.rstrip("/").split("/")[-1].removesuffix(".git").strip()
    cleaned = _UNSAFE_REPO_NAME_RE.sub("_", raw)
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"Invalid repository URL or name: {repo_url!r}")
    return cleaned


def _rmtree_force(path: Path, retries: int = 3, delay: float = 0.4) -> bool:
    """Best-effort directory removal that tolerates Windows file locks.

    Git pack files (`.git/objects/pack/*.idx`) and other files left behind by a
    cancelled run are often read-only or briefly locked on Windows, which makes
    a plain ``shutil.rmtree`` raise ``PermissionError`` ([WinError 5]).
    This retries after making files writable and returns whether the dir is gone.
    """
    def _on_error(func, p, _exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        try:
            func(p)
        except OSError:
            pass

    for _ in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_error)
            return not path.exists()
        except OSError:
            time.sleep(delay)
    return not path.exists()


class GitWorkspaceManager:
    def __init__(self, workspace_root: Path | None = None):
        self.workspace_root = Path(workspace_root) if workspace_root else settings.resolved_workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _ensure_git_identity(repo: Repo) -> None:
        reader = repo.config_reader()
        missing_name = False
        missing_email = False
        try:
            reader.get_value("user", "name")
        except Exception:
            missing_name = True
        try:
            reader.get_value("user", "email")
        except Exception:
            missing_email = True

        if not (missing_name or missing_email):
            return
        with repo.config_writer() as cfg:
            if missing_name:
                cfg.set_value("user", "name", "CodeRevive Agent")
            if missing_email:
                cfg.set_value("user", "email", "coderevive@local")

    @staticmethod
    def _resolve_base_ref(repo: Repo) -> str:
        """Resolve the upstream default branch commit rather than a prior migration commit.

        Production migrations must always start from origin/HEAD (e.g. 20d013a), never
        from a previously migrated migration/spring-boot-3 commit sitting in the local
        workspace. Re-running against an already-migrated workspace would otherwise apply
        OpenRewrite a second time and break idempotent transforms such as Pbkdf2 encoder
        normalization.
        """
        try:
            origin = repo.remotes.origin
            for ref in origin.refs:
                if ref.name == "origin/HEAD":
                    return ref.commit.hexsha
            ref_names = {ref.name for ref in origin.refs}
            for candidate in ("origin/main", "origin/master"):
                if candidate in ref_names:
                    return repo.commit(candidate).hexsha
        except Exception:
            pass
        return repo.head.commit.hexsha

    @staticmethod
    def _checkout_migration_branch(
        repo: Repo, branch_name: str, reset_to_base: bool = False
    ) -> tuple[str, str]:
        base_sha = (
            GitWorkspaceManager._resolve_base_ref(repo)
            if reset_to_base
            else repo.head.commit.hexsha
        )
        if branch_name not in repo.heads:
            migration_branch = repo.create_head(branch_name, commit=base_sha)
        else:
            migration_branch = repo.heads[branch_name]
            if reset_to_base:
                migration_branch.set_commit(base_sha)
        migration_branch.checkout(force=reset_to_base)
        return base_sha, repo.active_branch.name

    def prepare_workspace(
        self,
        repo_url: str,
        branch_name: str = "migration/spring-boot-3",
        clean: bool = False,
    ) -> dict:
        repo_name = _safe_repo_name(repo_url)
        target_path = self.workspace_root / repo_name

        if target_path.exists() and clean:
            if not _rmtree_force(target_path):
                # A file in the workspace is locked (common on Windows after a
                # cancelled run leaves git pack files open). Fall back to a fresh,
                # uniquely-named workspace so the new migration can still run.
                target_path = self.workspace_root / f"{repo_name}-{uuid.uuid4().hex[:8]}"
                target_path.mkdir(parents=True, exist_ok=True)

        if not target_path.exists():
            cloned_repo = Repo.clone_from(repo_url, target_path)
        else:
            cloned_repo = Repo(target_path)
            try:
                cloned_repo.git.reset("--hard")
                cloned_repo.git.clean("-fd")
            except Exception:
                pass

        self._ensure_git_identity(cloned_repo)
        baseline_sha, active_branch = self._checkout_migration_branch(
            cloned_repo, branch_name, reset_to_base=True
        )

        return {
            "target_path": target_path,
            "baseline_sha": baseline_sha,
            "active_branch": active_branch,
            "isolated_workspace": True,
        }

    def prepare_local_workspace(
        self,
        source_path: Path,
        branch_name: str = "migration/spring-boot-3",
        clean: bool = True,
    ) -> dict:
        """Copy a local repository into an isolated workspace so the original directory is never mutated."""
        source_path = Path(source_path).resolve()
        if not source_path.is_dir():
            raise FileNotFoundError(f"Local repository path not found: {source_path}")

        target_path = self.workspace_root / source_path.name
        if target_path.exists() and clean:
            if not _rmtree_force(target_path):
                target_path = self.workspace_root / f"{source_path.name}-{uuid.uuid4().hex[:8]}"
                target_path.mkdir(parents=True, exist_ok=True)

        if not target_path.exists():
            shutil.copytree(
                source_path,
                target_path,
                ignore=shutil.ignore_patterns("target", "build", ".idea", "__pycache__"),
            )

        try:
            repo = Repo(target_path)
            repo.git.reset("--hard")
            repo.git.clean("-fd")
        except InvalidGitRepositoryError:
            repo = Repo.init(target_path)
            self._ensure_git_identity(repo)
            repo.git.add(A=True)
            repo.index.commit("Baseline snapshot for CodeRevive migration")

        self._ensure_git_identity(repo)
        baseline_sha, active_branch = self._checkout_migration_branch(repo, branch_name)

        return {
            "target_path": target_path,
            "baseline_sha": baseline_sha,
            "active_branch": active_branch,
            "isolated_workspace": True,
        }