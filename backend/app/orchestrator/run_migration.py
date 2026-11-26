import argparse
import os
from pathlib import Path
import sys
from typing import Callable, Optional

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

import httpx

from app.config import settings
from app.ingestion.pipeline import IngestionPipeline, UnsupportedRepositoryError
from app.llm.base import LLMProviderError
from app.models.orchestrator import FileStatus, WorkflowState
from app.orchestrator.service import MigrationJobRequest
from app.orchestrator.workflow import MigrationOrchestrator
from app.sandbox.docker_runner import ContainerExecutionError


def _normalize_progress_callback_base(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        return ""
    if "/internal/github-actions/runs" in cleaned:
        return cleaned
    if cleaned.endswith("/api"):
        return f"{cleaned}/internal/github-actions/runs"
    return f"{cleaned}/api/internal/github-actions/runs"


def _build_progress_publisher() -> Optional[Callable[[str], None]]:
    """Build a progress_callback that relays each internal stage transition
    back to the backend's live Pipeline Monitor.

    Returns None (callback disabled) unless CODEREVIVE_PROGRESS_CALLBACK_URL,
    CODEREVIVE_PROGRESS_CALLBACK_TOKEN and GITHUB_RUN_ID are all present, so
    this is a no-op for local runs.
    """
    base_url = _normalize_progress_callback_base(
        settings.progress_callback_url or os.environ.get("CODEREVIVE_PROGRESS_CALLBACK_URL", "")
    )
    token = (
        settings.progress_callback_token
        or os.environ.get("CODEREVIVE_PROGRESS_CALLBACK_TOKEN", "")
    ).strip()
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    if not (base_url and token and run_id):
        return None

    endpoint = f"{base_url}/{run_id}/events"

    def publish(event_text: str) -> None:
        print(event_text, flush=True)
        stage = event_text.split(":", 1)[0] if ":" in event_text else event_text
        try:
            httpx.post(
                endpoint,
                json={"stage": stage, "message": event_text},
                headers={"X-CodeRevive-Progress-Token": token},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            print(f"[PROGRESS_RELAY_WARNING] {exc}", flush=True)

    return publish

_STATUS_MARKER = {
    FileStatus.SUCCESS: "[PASS]",
    FileStatus.FAILED: "[FAIL]",
    FileStatus.REJECTED: "[REJT]",
    FileStatus.SKIPPED: "[SKIP]",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the CodeRevive Spring Boot 2 -> 3 migration agent: "
        "ingest repository, parse code/AST, run Pass 1 solely via OpenRewrite, "
        "verify in Docker sandbox during Pass 2 with AI error resolution, verify integrity before commit, "
        "and output an audit report."
    )

    parser.add_argument(
        "--repo-url",
        default=settings.repo_url,
        help=(
            "Git URL (or local path) of the repo to migrate. "
            f"Defaults to CODEREVIVE_REPO_URL from backend/.env ({settings.repo_url})."
        ),
    )
    parser.add_argument(
        "--target-boot",
        default=settings.target_spring_boot_version,
        help=f"Target Spring Boot 3.x version. Default: {settings.target_spring_boot_version}",
    )
    parser.add_argument(
        "--target-java",
        default=settings.target_java_version,
        help=f"Target Java version. Default: {settings.target_java_version}",
    )
    parser.add_argument(
        "--branch",
        default=settings.migration_branch_name,
        help=f"Migration branch name. Default: {settings.migration_branch_name}",
    )
    parser.add_argument(
        "--no-openrewrite",
        action="store_true",
        help="Disable the OpenRewrite pre-pass and rely solely on the LLM.",
    )
    parser.add_argument(
        "--openrewrite-mode",
        choices=["hybrid", "deterministic", "maven_only"],
        default=settings.openrewrite_mode,
        help="OpenRewrite pass mode: 'hybrid' (Maven plugin + deterministic fallback/normalizer), "
        "'deterministic' (instant local AST/POM pass without Maven plugin download), or 'maven_only'. "
        f"Default: {settings.openrewrite_mode}",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete and re-clone/re-copy the workspace before running.",
    )
    parser.add_argument(
        "--retry-budget",
        type=int,
        default=settings.default_retry_budget,
        help=f"Max sandbox repair rounds before rolling back. Default: {settings.default_retry_budget}",
    )
    return parser.parse_args()


def _summarize_failed_run(summary) -> str:
    """Produce a concise, user-facing failure reason from a MigrationRunSummary."""
    real_violations = [
        v.strip()
        for v in (summary.integrity_violations or [])
        if v and v.strip()
        and not v.strip().startswith("Migration aborted before a passing build")
    ]
    build_errors = [
        e.strip() for e in (summary.last_build_errors or []) if e and e.strip()
    ]
    if not summary.integrity_passed and real_violations and not build_errors:
        msg = f"Post-migration integrity check failed: {real_violations[0]}"
    elif build_errors:
        primary = build_errors[0]
        if summary.total_retries > 0:
            msg = (
                f"Migration rolled back after {summary.total_retries} repair "
                f"attempt(s): {primary}"
            )
        else:
            msg = f"Migration rolled back: {primary}"
    elif real_violations:
        msg = f"Migration rolled back: {real_violations[0]}"
    else:
        msg = "Migration failed before a passing build was reached; workspace reverted to baseline."
    return msg[:397] + "..." if len(msg) > 400 else msg


def main() -> int:
    args = _parse_args()
    request = MigrationJobRequest(
        repo_url=args.repo_url,
        target_spring_boot=args.target_boot,
        target_java=args.target_java,
        branch_name=args.branch,
        use_openrewrite=not args.no_openrewrite,
        openrewrite_mode=args.openrewrite_mode,
        retry_budget=args.retry_budget,
        clean=args.clean,
    )

    print("========== CODEREVIVE: AUTONOMOUS MIGRATION ENGINE ==========")

    publish_progress = _build_progress_publisher()

    print(f"\n[1/3] Ingesting {request.repo_url} ...")
    if publish_progress:
        publish_progress(f"ingesting:{request.repo_url}")
    try:
        ingestion_report = IngestionPipeline(
            branch_name=request.branch_name,
            workspace_root=settings.resolved_workspace_root,
            target_spring_boot=request.target_spring_boot,
            target_java=request.target_java,
        ).run(request.repo_url, clean=request.clean)
    except UnsupportedRepositoryError as exc:
        print(f"\nRepository not supported: {exc}")
        if publish_progress:
            publish_progress(f"failed:Repository not supported: {exc}")
        return 1

    repo_path = Path(ingestion_report.repo_info.target_path)

    print(f"  repo:               {repo_path}")
    print(f"  isolated workspace: {ingestion_report.repo_info.isolated_workspace}")
    print(f"  branch:             {ingestion_report.repo_info.active_branch}")
    print(f"  baseline commit:    {ingestion_report.repo_info.baseline_sha}")
    print(
        f"  Spring Boot:        {ingestion_report.pom.spring_boot_version} -> {request.target_spring_boot}"
    )
    print(f"  migration targets:  {len(ingestion_report.files_to_migrate)}")
    print(f"  baseline @Test:     {ingestion_report.baseline_test_count}")

    if publish_progress:
        publish_progress(f"orchestrating:{repo_path}")

    print(
        "\n[2/3] Running hybrid OpenRewrite + LLM migration loop with sandbox verification ..."
    )
    try:
        orchestrator = MigrationOrchestrator(
            repo_path=repo_path,
            retry_budget=request.retry_budget,
            use_openrewrite=request.use_openrewrite,
            openrewrite_mode=request.openrewrite_mode,
            progress_callback=publish_progress,
        )
        summary = orchestrator.run(ingestion_report)
    except (ValueError, ImportError) as exc:
        print(f"\nCould not set up the LLM provider: {exc}")
        print(
            "Set CODEREVIVE_LLM_PROVIDER and the matching API key in backend/.env (see .env.example)."
        )
        if publish_progress:
            publish_progress(f"failed:Could not set up the LLM provider: {exc}")
        return 1
    except (LLMProviderError, ContainerExecutionError) as exc:
        print(f"\nMigration run aborted: {exc}")
        if publish_progress:
            publish_progress(f"failed:{exc}")
        return 1

    print("\n[3/3] Writing the audit report ...")
    out_path = settings.resolved_workspace_root / "migration_run_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    print(f"  report written to: {out_path}")

    print("\n========== MIGRATION RUN SUMMARY ==========")
    print(f"final_state:            {summary.final_state.value}")
    print(f"target_spring_boot:     {summary.target_spring_boot_version}")
    print(
        f"openrewrite_used:       {summary.openrewrite_used} ({summary.openrewrite_files_modified} files)"
    )
    print(f"final_commit:           {summary.final_commit}")
    print(
        f"total_files_migrated:   {summary.total_files_migrated}/{len(ingestion_report.files_to_migrate)}"
    )
    print(f"total_retries:          {summary.total_retries}")
    print(f"integrity_passed:       {summary.integrity_passed}")
    print(f"total_duration_seconds: {summary.total_duration_seconds}")

    print("\nPer-file results:")
    for status in summary.file_statuses:
        marker = _STATUS_MARKER.get(status.status, "[????]")
        via = f", via={status.applied_by}" if status.applied_by else ""
        extra = (
            ", no changes needed"
            if status.status == FileStatus.SKIPPED and status.attempts == 0
            else ""
        )
        print(
            f"  {marker} {status.file_path} (attempts={status.attempts}{via}{extra}, commit={status.commit_sha})"
        )
        err_prefix = "[resolved in Pass 2]" if status.status == FileStatus.SUCCESS else "-"
        for err in status.errors_encountered[:3]:
            print(f"        {err_prefix} {err}")

    if summary.last_build_errors:
        print("\nLast build diagnostics:")
        for err in summary.last_build_errors[:10]:
            print(f"  - {err}")

    if summary.integrity_violations:
        print("\nIntegrity violations:")
        for violation in summary.integrity_violations:
            print(f"  - {violation}")

    if summary.final_state != WorkflowState.COMPLETED and publish_progress:
        publish_progress(f"failed:{_summarize_failed_run(summary)}")

    return 0 if summary.final_state == WorkflowState.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())