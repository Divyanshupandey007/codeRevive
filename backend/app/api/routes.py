from typing import Any, Dict, Optional

from app.orchestrator.service import (
    MigrationJobRequest,
    get_active_config,
    load_latest_summary,
    run_migration_job,
)


def health_check() -> Dict[str, str]:
    return {"status": "ok", "service": "coderevive-migration-engine"}


def get_config_endpoint() -> Dict[str, Any]:
    return get_active_config()


def get_latest_report_endpoint() -> Optional[Dict[str, Any]]:
    summary = load_latest_summary()
    return summary.model_dump() if summary is not None else None


def start_migration_endpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    request = MigrationJobRequest.model_validate(payload or {})
    result = run_migration_job(request)
    return {
        "summary": result.summary.model_dump(),
        "summary_path": result.summary_path,
    }