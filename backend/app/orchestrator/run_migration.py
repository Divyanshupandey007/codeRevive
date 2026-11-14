import argparse
from pathlib import Path
import sys

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.ingestion.pipeline import IngestionPipeline
from app.llm.base import LLMProviderError
from app.models.orchestrator import FileStatus, WorkflowState
from app.orchestrator.workflow import MigrationOrchestrator
from app.sandbox.docker_runner import ContainerExecutionError

DEFAULT_REPO_URL="https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git"

_STATUS_MARKER={
    FileStatus.SUCCESS:"[PASS]",
    FileStatus.FAILED:"[FAIL]",
    FileStatus.SKIPPED:"[SKIP]"
}

def _parse_args()->argparse.Namespace:
    parser=argparse.ArgumentParser(
        description="run the autonomous orchestration engine end to "
                     "end - ingest a repo, migrate every javax.* target file "
                     "(plus pom.xml) with an LLM + Docker-sandboxed repair "
                     "loop, verify post-execution integrity, and write the "
                     "audit report."
    )

    parser.add_argument(
        "--repo-url",default=DEFAULT_REPO_URL,
        help=f"Git URL (or local path) of the repo to migrate. Default: {DEFAULT_REPO_URL}"
    )
    parser.add_argument(
        "--clean",action="store_true",
        help="Delete and re-clone the workspace before running, instead of reusing it."
    )
    parser.add_argument(
        "--retry-budget",type=int,default=3,
        help="Max LLM generate+verify attempts per file before rolling it back. Default: 3"
    )
    return parser.parse_args()
 
 
def main()->int:
    args=_parse_args()
 
    print("========== PHASE 5: AUTONOMOUS ORCHESTRATION ENGINE ==========")
 
    print(f"\n[1/3] Ingesting {args.repo_url} ...")
    ingestion_report=IngestionPipeline().run(args.repo_url,clean=args.clean)
    repo_path=Path(ingestion_report.repo_info.target_path)
 
    print(f"  repo:               {repo_path}")
    print(f"  branch:             {ingestion_report.repo_info.active_branch}")
    print(f"  baseline commit:    {ingestion_report.repo_info.baseline_sha}")
    print(f"  migration targets:  {len(ingestion_report.files_to_migrate)}")
    print(f"  baseline @Test:     {ingestion_report.baseline_test_count}")
 
    print("\n[2/3] Running the autonomous migration loop "
          "(LLM -> guardrail -> patch -> Docker -> commit/repair for each target) ...")
    try:
        orchestrator=MigrationOrchestrator(repo_path=repo_path,retry_budget=args.retry_budget)
    except (ValueError,ImportError) as exc:
        print(f"\nCould not set up the LLM provider: {exc}")
        print("Set CODEREVIVE_LLM_PROVIDER and the matching API key in backend/.env (see .env.example).")
        return 1
 
    try:
        summary=orchestrator.run(ingestion_report)
    except (LLMProviderError,ContainerExecutionError) as exc:
        print(f"\nMigration run aborted: {exc}")
        return 1
 
    print("\n[3/3] Writing the audit report ...")
    out_path=backend_root/"workspace"/"migration_run_summary.json"
    out_path.parent.mkdir(parents=True,exist_ok=True)
    out_path.write_text(summary.model_dump_json(indent=2))
    print(f"  report written to: {out_path}")
 
    print("\n========== MIGRATION RUN SUMMARY ==========")
    print(f"final_state:            {summary.final_state.value}")
    print(f"final_commit:           {summary.final_commit}")
    print(f"total_files_migrated:   {summary.total_files_migrated}/{len(ingestion_report.files_to_migrate)}")
    print(f"total_retries:          {summary.total_retries}")
    print(f"integrity_passed:       {summary.integrity_passed}")
    print(f"total_duration_seconds: {summary.total_duration_seconds}")
 
    print("\nPer-file results:")
    for status in summary.file_statuses:
        marker=_STATUS_MARKER[status.status]
        print(f"  {marker} {status.file_path} (attempts={status.attempts}, commit={status.commit_sha})")
        for err in status.errors_encountered[:3]:
            print(f"        - {err}")
 
    if summary.integrity_violations:
        print("\nIntegrity violations:")
        for violation in summary.integrity_violations:
            print(f"  - {violation}")
 
    return 0 if summary.final_state==WorkflowState.COMPLETED else 1
 
 
if __name__=="__main__":
    sys.exit(main())