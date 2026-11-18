from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from typing import Any

from pydantic import ValidationError

backend_root = Path(__file__).resolve().parents[1]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.api.routes import (
    get_config_endpoint,
    get_latest_report_endpoint,
    health_check,
    start_migration_endpoint,
)
from app.ingestion.pipeline import UnsupportedRepositoryError
from app.llm.base import LLMProviderError
from app.sandbox.docker_runner import ContainerExecutionError

MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MiB


class _CodeReviveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _StdlibApiHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send_json(HTTPStatus.OK, health_check())
        elif self.path == "/api/config":
            self._send_json(HTTPStatus.OK, get_config_endpoint())
        elif self.path == "/api/migrations/latest":
            report = get_latest_report_endpoint()
            if report is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND, {"detail": "No migration report found yet."}
                )
            else:
                self._send_json(HTTPStatus.OK, report)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/api/migrations":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
            return

        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"detail": "Invalid Content-Length header."}
            )
            return

        if length < 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"detail": "Negative Content-Length header."}
            )
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"detail": f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes."},
            )
            return

        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"detail": f"Invalid JSON body: {exc}"}
            )
            return

        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"detail": "Request body must be a JSON object."},
            )
            return

        try:
            result = start_migration_endpoint(payload)
            self._send_json(HTTPStatus.OK, result)
        except ValidationError as exc:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": exc.errors()}
            )
        except UnsupportedRepositoryError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
        except (ValueError, LLMProviderError, ContainerExecutionError) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = _CodeReviveHTTPServer((host, port), _StdlibApiHandler)
    print(f"CodeRevive API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
