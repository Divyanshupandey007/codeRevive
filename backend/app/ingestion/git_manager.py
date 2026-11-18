from pathlib import Path
import re
import shutil
from git import InvalidGitRepositoryError, Repo

from app.config import settings

_UNSAFE_REPO_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_repo_name(repo_url: str) -> str:
    raw = repo_url.rstrip("/").split("/")[-1].removesuffix(".git").strip()
    cleaned = _UNSAFE_REPO_NAME_RE.sub("_", raw)
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"Invalid repository URL or name: {repo_url!r}")
    return cleaned


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
    def _checkout_migration_branch(repo: Repo, branch_name: str) -> tuple[str, str]:
        baseline_sha = repo.head.commit.hexsha
        if branch_name not in repo.heads:
            migration_branch = repo.create_head(branch_name)
        else:
            migration_branch = repo.heads[branch_name]
        migration_branch.checkout()
        return baseline_sha, repo.active_branch.name

    def prepare_workspace(
        self,
        repo_url: str,
        branch_name: str = "migration/spring-boot-3",
        clean: bool = False,
    ) -> dict:
        repo_name = _safe_repo_name(repo_url)
        target_path = self.workspace_root / repo_name

        if target_path.exists() and clean:
            shutil.rmtree(target_path)

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
            cloned_repo, branch_name
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
            shutil.rmtree(target_path)

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