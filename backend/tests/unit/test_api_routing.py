from http import HTTPStatus
import http.client
import json
from pathlib import Path
import threading
import time
from typing import Tuple

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.jobs import job_manager
from app.config import Settings, settings
from app.main import _CodeReviveHTTPServer, _StdlibApiHandler, app, normalize_path
from app.models.manifest import (
    DependencyGraphManifest,
    PomManifest,
    ProjectIngestionReport,
    RepoInfo,
)
from app.models.orchestrator import MigrationRunSummary, WorkflowState
from app.orchestrator.service import MigrationServiceResult


def _make_fake_service_result(
    repo_url: str = "https://github.com/org/demo.git",
) -> MigrationServiceResult:
    report = ProjectIngestionReport(
        repo_info=RepoInfo(
            repo_url=repo_url,
            target_path="/tmp/demo",
            active_branch="migration/spring-boot-3",
            baseline_sha="abc1234",
            isolated_workspace=True,
        ),
        pom=PomManifest(spring_boot_version="2.7.14", java_version="11"),
        graph=DependencyGraphManifest(
            total_nodes=1,
            total_edges=0,
            unresolved_import_count=0,
            migration_order=[],
            high_blast_radius=[],
        ),
        files_to_migrate=[],
        baseline_test_count=0,
    )
    summary = MigrationRunSummary(
        repo_url=repo_url,
        baseline_commit="abc1234",
        final_commit="def5678",
        final_state=WorkflowState.COMPLETED,
        integrity_passed=True,
    )
    return MigrationServiceResult(
        ingestion_report=report,
        summary=summary,
        summary_path="/tmp/workspace/migration_run_summary.json",
    )


def _start_test_server() -> Tuple[_CodeReviveHTTPServer, int]:
    server = _CodeReviveHTTPServer(("127.0.0.1", 0), _StdlibApiHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


@pytest.fixture(autouse=True)
def _reset_job_manager():
    job_manager.clear()
    yield
    job_manager.clear()


def test_app_is_fastapi_and_exposes_openapi_docs():
    assert isinstance(app, FastAPI)
    client = TestClient(app)

    schema_resp = client.get("/api/openapi.json")
    assert schema_resp.status_code == HTTPStatus.OK
    schema = schema_resp.json()
    assert schema["info"]["title"] == "CodeRevive Migration Engine API"
    assert "/api/health" in schema["paths"]
    assert "/api/migrations" in schema["paths"]
    assert "/api/migrations/jobs" in schema["paths"]
    assert "/api/migrations/jobs/{job_id}" in schema["paths"]

    docs_resp = client.get("/api/docs")
    assert docs_resp.status_code == HTTPStatus.OK


def test_normalize_path_handles_slashes_queries_and_v1_alias():
    assert normalize_path("/api/health?foo=bar") == "/api/health"
    assert normalize_path("/api/health/?foo=bar") == "/api/health"
    assert normalize_path("/api//migrations///latest/") == "/api/migrations/latest"
    assert normalize_path("/api/v1/health") == "/api/health"
    assert normalize_path("/api/v1/migrations/jobs/123/") == "/api/migrations/jobs/123"
    assert normalize_path("/api/v1") == "/api"
    assert normalize_path("/") == "/"


def test_settings_parses_comma_separated_cors_origins(monkeypatch):
    monkeypatch.setenv(
        "CODEREVIVE_CORS_ALLOWED_ORIGINS",
        "http://localhost:4200, https://coderevive.example.com",
    )
    custom = Settings(_env_file=None)
    assert custom.cors_allowed_origins == [
        "http://localhost:4200",
        "https://coderevive.example.com",
    ]


def test_router_handles_queries_trailing_slashes_head_405_and_v1_aliases():
    server, port = _start_test_server()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        for path in (
            "/",
            "/api",
            "/api/routes",
            "/health",
            "/api/health/",
            "/api/health?cacheBust=123",
            "/api/v1/health?t=1",
            "/api/health/live",
            "/api/health/ready",
            "/api/config/",
            "/api/v1/config?env=dev",
        ):
            conn.request("GET", path)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            assert resp.status == HTTPStatus.OK, (
                f"Expected 200 for {path}, got {resp.status}: {body}"
            )

        # HEAD on GET route returns 200 with Content-Length and empty body
        conn.request("HEAD", "/api/health")
        resp = conn.getresponse()
        raw = resp.read()
        assert resp.status == HTTPStatus.OK
        assert int(resp.getheader("Content-Length") or "0") > 0
        assert raw == b""

        # Wrong method on valid path -> 405 with Allow header
        conn.request("POST", "/api/health", body="{}")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.METHOD_NOT_ALLOWED
        allow = resp.getheader("Allow") or ""
        assert "GET" in allow

        # Unknown /api route -> 404 JSON
        conn.request("GET", "/api/does-not-exist")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.NOT_FOUND

        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_cors_preflight_and_error_responses_work_for_angular_origin():
    client = TestClient(app)

    # Preflight OPTIONS from Angular dev server
    preflight = client.options(
        "/api/migrations/jobs",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert preflight.status_code == HTTPStatus.OK
    assert preflight.headers.get("access-control-allow-origin") == "http://localhost:4200"
    assert preflight.headers.get("access-control-allow-credentials") == "true"
    assert "POST" in (preflight.headers.get("access-control-allow-methods") or "")

    # Error response (422) still includes CORS headers so Angular can read error body
    err_resp = client.post(
        "/api/migrations",
        json={"retry_budget": "bad"},
        headers={"Origin": "http://localhost:4200"},
    )
    assert err_resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert err_resp.headers.get("access-control-allow-origin") == "http://localhost:4200"

    # Disallowed origin does not receive matching Allow-Origin
    disallowed = client.get(
        "/api/health",
        headers={"Origin": "https://evil.example.org"},
    )
    assert disallowed.status_code == HTTPStatus.OK
    assert disallowed.headers.get("access-control-allow-origin") is None


def test_sync_and_async_migration_job_routing_lifecycle(monkeypatch):
    def fake_run(request, progress_callback=None, **_kwargs):
        if progress_callback:
            progress_callback(f"ingesting:{request.repo_url}")
            progress_callback("orchestrating:/tmp/demo")
        return _make_fake_service_result(request.repo_url)

    monkeypatch.setattr("app.api.jobs.run_migration_job", fake_run)
    monkeypatch.setattr("app.api.routes.run_migration_job", fake_run)

    client = TestClient(app)

    # 1. Default POST /api/migrations stays synchronous (backwards compatible)
    sync_resp = client.post(
        "/api/migrations",
        json={"repo_url": "https://github.com/org/sync-repo.git"},
    )
    assert sync_resp.status_code == HTTPStatus.OK
    assert sync_resp.json()["summary"]["repo_url"] == "https://github.com/org/sync-repo.git"

    # 2. Async via POST /api/migrations/jobs -> 202 Accepted
    job_resp = client.post(
        "/api/migrations/jobs",
        json={"repo_url": "https://github.com/org/async-repo.git"},
    )
    assert job_resp.status_code == HTTPStatus.ACCEPTED
    job_id = job_resp.json()["job_id"]
    assert job_resp.headers.get("location") == f"/api/migrations/jobs/{job_id}"

    # Poll until completed
    final_job = None
    for _ in range(50):
        poll = client.get(f"/api/migrations/jobs/{job_id}")
        assert poll.status_code == HTTPStatus.OK
        final_job = poll.json()
        if final_job["status"] == "COMPLETED":
            break
        time.sleep(0.02)

    assert final_job is not None
    assert final_job["status"] == "COMPLETED"
    assert final_job["summary"]["final_state"] == "COMPLETED"

    # Alias GET /api/migrations/{job_id} works, while /api/migrations/latest still wins
    alias_resp = client.get(f"/api/migrations/{job_id}")
    assert alias_resp.status_code == HTTPStatus.OK
    assert alias_resp.json()["job_id"] == job_id

    # Progress events endpoint
    events_resp = client.get(f"/api/migrations/jobs/{job_id}/events")
    assert events_resp.status_code == HTTPStatus.OK
    stages = [ev["stage"] for ev in events_resp.json()["events"]]
    assert "ingesting" in stages
    assert "orchestrating" in stages
    assert "completed" in stages

    # 3. Async via POST /api/migrations?async=true and Prefer: respond-async (with empty body)
    async_q = client.post("/api/migrations?async=true")
    assert async_q.status_code == HTTPStatus.ACCEPTED

    async_hdr = client.post(
        "/api/migrations",
        json={},
        headers={"Prefer": "respond-async"},
    )
    assert async_hdr.status_code == HTTPStatus.ACCEPTED

    # List jobs with query filters
    listed = client.get("/api/migrations?limit=2&offset=0")
    assert listed.status_code == HTTPStatus.OK
    listed_body = listed.json()
    assert listed_body["total"] == 3
    assert len(listed_body["items"]) == 2

    # Delete completed job -> 200, then 404
    del_resp = client.delete(f"/api/migrations/jobs/{job_id}")
    assert del_resp.status_code == HTTPStatus.OK
    assert del_resp.json()["deleted"] is True

    del_again = client.delete(f"/api/migrations/jobs/{job_id}")
    assert del_again.status_code == HTTPStatus.NOT_FOUND


def test_delete_running_job_returns_409_conflict(monkeypatch):
    gate = threading.Event()

    def blocking_run(request, progress_callback=None, **_kwargs):
        gate.wait(timeout=3)
        return _make_fake_service_result(request.repo_url)

    monkeypatch.setattr("app.api.jobs.run_migration_job", blocking_run)

    client = TestClient(app)
    try:
        create_resp = client.post("/api/migrations/jobs", json={})
        assert create_resp.status_code == HTTPStatus.ACCEPTED
        job_id = create_resp.json()["job_id"]

        conflict_resp = client.delete(f"/api/migrations/jobs/{job_id}")
        assert conflict_resp.status_code == HTTPStatus.CONFLICT
    finally:
        gate.set()


def test_cancel_running_migration_job(monkeypatch):
    gate = threading.Event()

    def blocking_run(request, progress_callback=None, **_kwargs):
        gate.wait(timeout=5)
        return _make_fake_service_result(request.repo_url)

    monkeypatch.setattr("app.api.jobs.run_migration_job", blocking_run)
    monkeypatch.setattr("app.api.routes.run_migration_job", blocking_run)

    client = TestClient(app)
    try:
        create_resp = client.post("/api/migrations/jobs", json={})
        assert create_resp.status_code == HTTPStatus.ACCEPTED
        job_id = create_resp.json()["job_id"]

        cancel_resp = client.post(f"/api/migrations/jobs/{job_id}/cancel")
        assert cancel_resp.status_code == HTTPStatus.OK
        assert cancel_resp.json() == {"deleted": True, "job_id": job_id}

        # Release the in-progress migration so the worker observes cancellation.
        gate.set()

        final_job = None
        for _ in range(100):
            poll = client.get(f"/api/migrations/jobs/{job_id}")
            assert poll.status_code == HTTPStatus.OK
            final_job = poll.json()
            if final_job["status"] == "CANCELLED":
                break
            time.sleep(0.02)

        assert final_job is not None
        assert final_job["status"] == "CANCELLED"
        assert final_job["completed_at"] is not None

        events = client.get(f"/api/migrations/jobs/{job_id}/events").json()["events"]
        assert any(ev["stage"] == "cancelled" for ev in events)

        # Cancellation is terminal: repeating it is not found.
        again = client.post(f"/api/migrations/jobs/{job_id}/cancel")
        assert again.status_code == HTTPStatus.NOT_FOUND
    finally:
        gate.set()


def test_cancel_unknown_job_returns_404():
    client = TestClient(app)
    resp = client.post("/api/migrations/jobs/mig_doesnotexist/cancel")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_angular_spa_fallback_serves_deep_links_without_shadowing_api(tmp_path: Path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>Angular App</body></html>", encoding="utf-8")
    (dist / "main.js").write_text("console.log('angular');", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do-not-leak", encoding="utf-8")

    monkeypatch.setattr(settings, "frontend_dist_dir", dist)

    client = TestClient(app)

    # Client-side Angular deep link resolves to index.html
    deep = client.get("/migrations/mig_123")
    assert deep.status_code == HTTPStatus.OK
    assert "Angular App" in deep.text

    # Real static asset served directly
    asset = client.get("/main.js")
    assert asset.status_code == HTTPStatus.OK
    assert "console.log" in asset.text

    # Missing static asset returns 404 (not index.html)
    missing_asset = client.get("/missing.js")
    assert missing_asset.status_code == HTTPStatus.NOT_FOUND

    # Unknown /api route NEVER falls back to index.html
    missing_api = client.get("/api/unknown-route")
    assert missing_api.status_code == HTTPStatus.NOT_FOUND
    assert missing_api.json()["detail"].lower() == "not found"

    # Path traversal blocked
    traversal = client.get("/../secret.txt")
    assert "do-not-leak" not in traversal.text
