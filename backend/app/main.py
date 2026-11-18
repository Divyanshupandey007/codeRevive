from __future__ import annotations

import argparse
import asyncio
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import os
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import uvicorn

backend_root = Path(__file__).resolve().parents[1]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.api.jobs import JobConflictError
from app.api.routes import (
    api_router,
    get_config_endpoint,
    get_latest_report_endpoint,
    health_check,
    start_migration_endpoint,
)
from app.config import settings
from app.ingestion.pipeline import UnsupportedRepositoryError
from app.llm.base import LLMProviderError
from app.sandbox.docker_runner import ContainerExecutionError

logger = logging.getLogger("coderevive.api")

MAX_REQUEST_BODY_BYTES = settings.max_request_body_bytes

_STATIC_ASSET_EXTENSIONS = frozenset(
    {
        ".js",
        ".mjs",
        ".css",
        ".map",
        ".json",
        ".ico",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".txt",
        ".xml",
    }
)


def normalize_path(raw_path: str, version_aliases: Iterable[str] = ("/api/v1",)) -> str:
    """Extract, normalize, and canonicalize request path (strip query, collapse/trim slashes, alias /api/v1)."""
    parsed = urlsplit(raw_path or "/")
    path = parsed.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    for alias in version_aliases:
        clean_alias = alias.rstrip("/")
        if path == clean_alias:
            return "/api"
        if path.startswith(clean_alias + "/"):
            return "/api" + path[len(clean_alias) :]
    return path


def _resolve_spa_asset(
    raw_path: str,
    dist_dir: Optional[Path],
) -> Optional[Tuple[int, bytes, str, Dict[str, str]]]:
    """
    Serve compiled Angular static assets and HTML5 PathLocationStrategy fallback.

    Never intercepts `/api` or `/api/*` paths (including `/api/v1/*` alias).
    """
    if dist_dir is None:
        return None

    clean_path = normalize_path(raw_path)
    if clean_path == "/api" or clean_path.startswith("/api/"):
        return None

    root = Path(dist_dir).resolve()
    index_file = root / "index.html"
    if not index_file.is_file():
        return None

    parsed_path = unquote(urlsplit(raw_path or "/").path or "/")
    rel_part = parsed_path.lstrip("/")
    if rel_part:
        try:
            candidate = (root / rel_part).resolve()
        except (OSError, ValueError):
            return None
        if not candidate.is_relative_to(root):
            return None
        if candidate.is_file():
            content_type, _ = mimetypes.guess_type(str(candidate))
            mime = content_type or "application/octet-stream"
            cache = (
                "no-cache"
                if candidate.name == "index.html"
                else "public, max-age=31536000, immutable"
            )
            return (
                int(HTTPStatus.OK),
                candidate.read_bytes(),
                mime,
                {"Cache-Control": cache},
            )
        if Path(rel_part).suffix.lower() in _STATIC_ASSET_EXTENSIONS:
            return (
                int(HTTPStatus.NOT_FOUND),
                json.dumps({"detail": "Static asset not found", "path": clean_path}).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )

    return (
        int(HTTPStatus.OK),
        index_file.read_bytes(),
        "text/html; charset=utf-8",
        {"Cache-Control": "no-cache"},
    )


async def _send_asgi_bytes(
    send: Callable[..., Any],
    method: str,
    status_code: int,
    body: bytes,
    content_type: str,
    extra_headers: Optional[Dict[str, str]] = None,
    request_id: Optional[str] = None,
) -> None:
    headers: List[Tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode("latin-1")),
        (b"content-length", str(len(body)).encode("latin-1")),
        (b"x-content-type-options", b"nosniff"),
    ]
    if request_id:
        headers.append((b"x-request-id", request_id.encode("latin-1")))
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    await send({"type": "http.response.start", "status": int(status_code), "headers": headers})
    await send(
        {
            "type": "http.response.body",
            "body": b"" if method == "HEAD" else body,
            "more_body": False,
        }
    )


class _PathNormalizationMiddleware:
    """Normalize duplicate/trailing slashes and /api/v1 aliases before FastAPI routing."""

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(
        self, scope: Dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope["type"] == "http":
            normalized = normalize_path(scope.get("path", "/"))
            scope = dict(scope)
            scope["path"] = normalized
            scope["raw_path"] = normalized.encode("latin-1")
        await self.app(scope, receive, send)


class _RequestGuardAndSPAMiddleware:
    """Enforce body size limits, security/request-id headers, and optional Angular SPA fallback."""

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(
        self, scope: Dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        raw_path = str(scope.get("path", "/"))
        clean_path = normalize_path(raw_path)

        req_headers: Dict[str, str] = {}
        for k, v in scope.get("headers", []):
            key = k.decode("latin-1").lower() if isinstance(k, (bytes, bytearray)) else str(k).lower()
            val = v.decode("latin-1") if isinstance(v, (bytes, bytearray)) else str(v)
            req_headers[key] = val

        request_id = req_headers.get("x-request-id") or uuid4().hex[:12]

        # 1. Serve Angular SPA assets / deep-link index.html when frontend_dist_dir is configured
        if method in ("GET", "HEAD"):
            spa = _resolve_spa_asset(raw_path, settings.frontend_dist_dir)
            if spa is not None:
                spa_status, spa_bytes, spa_mime, spa_headers = spa
                await _send_asgi_bytes(
                    send=send,
                    method=method,
                    status_code=spa_status,
                    body=spa_bytes,
                    content_type=spa_mime,
                    extra_headers=spa_headers,
                    request_id=request_id,
                )
                return

        # 2. Enforce Content-Length & streaming body size bounds
        max_bytes = max(MAX_REQUEST_BODY_BYTES, settings.max_request_body_bytes)
        if "content-length" in req_headers:
            try:
                content_length = int(req_headers["content-length"])
            except ValueError:
                err = json.dumps({"detail": "Invalid Content-Length header."}, indent=2).encode("utf-8")
                await _send_asgi_bytes(
                    send, method, HTTPStatus.BAD_REQUEST, err,
                    "application/json; charset=utf-8", {"Cache-Control": "no-store"}, request_id
                )
                return
            if content_length < 0:
                err = json.dumps({"detail": "Negative Content-Length header."}, indent=2).encode("utf-8")
                await _send_asgi_bytes(
                    send, method, HTTPStatus.BAD_REQUEST, err,
                    "application/json; charset=utf-8", {"Cache-Control": "no-store"}, request_id
                )
                return
            if content_length > max_bytes:
                err = json.dumps(
                    {"detail": f"Request body exceeds {max_bytes} bytes."}, indent=2
                ).encode("utf-8")
                await _send_asgi_bytes(
                    send, method, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, err,
                    "application/json; charset=utf-8", {"Cache-Control": "no-store"}, request_id
                )
                return

        # Wrap receive to cap chunked/streamed payloads as well
        bytes_seen = 0

        async def guarded_receive() -> Dict[str, Any]:
            nonlocal bytes_seen
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                bytes_seen += len(chunk)
                if bytes_seen > max_bytes:
                    raise _PayloadTooLargeError(max_bytes)
            return message

        # Support HEAD cleanly against FastAPI GET routes without polluting OpenAPI schema
        downstream_scope = scope
        if method == "HEAD":
            downstream_scope = dict(scope)
            downstream_scope["method"] = "GET"

        # Inject security & cache headers onto downstream responses (and strip HEAD body)
        async def wrapped_send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing_keys = {k.lower() for k, _ in headers}
                if b"x-content-type-options" not in existing_keys:
                    headers.append((b"x-content-type-options", b"nosniff"))
                if b"x-request-id" not in existing_keys:
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
                if (clean_path == "/api" or clean_path.startswith("/api/")) and b"cache-control" not in existing_keys:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and method == "HEAD":
                message = {**message, "body": b""}
            await send(message)

        try:
            await self.app(downstream_scope, guarded_receive, wrapped_send)
        except _PayloadTooLargeError as exc:
            err = json.dumps(
                {"detail": f"Request body exceeds {exc.limit} bytes."}, indent=2
            ).encode("utf-8")
            await _send_asgi_bytes(
                send, method, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, err,
                "application/json; charset=utf-8", {"Cache-Control": "no-store"}, request_id
            )


class _PayloadTooLargeError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"Request body exceeds {limit} bytes.")
        self.limit = limit


def create_app() -> FastAPI:
    """Create the production FastAPI application with CORS, validation handlers, and routing."""
    fastapi_app = FastAPI(
        title="CodeRevive Migration Engine API",
        description="Autonomous Spring Boot 2.x to 3.x migration engine API.",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    fastapi_app.include_router(api_router)

    @fastapi_app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        for err in errors:
            err_type = err.get("type", "")
            loc = err.get("loc", ())
            if err_type == "json_invalid":
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": f"Invalid JSON body: {err.get('msg', 'malformed JSON')}"},
                )
            if loc == ("body",) and err_type in ("model_type", "model_attributes_type", "dict_type"):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Request body must be a JSON object."},
                )
        unprocessable_status = getattr(
            status,
            "HTTP_422_UNPROCESSABLE_CONTENT",
            getattr(status, "HTTP_422_UNPROCESSABLE_ENTITY", 422),
        )
        return JSONResponse(
            status_code=unprocessable_status,
            content={"detail": errors},
        )

    @fastapi_app.exception_handler(ValidationError)
    async def pydantic_validation_exception_handler(
        _request: Request, exc: ValidationError
    ) -> JSONResponse:
        unprocessable_status = getattr(
            status,
            "HTTP_422_UNPROCESSABLE_CONTENT",
            getattr(status, "HTTP_422_UNPROCESSABLE_ENTITY", 422),
        )
        return JSONResponse(
            status_code=unprocessable_status,
            content={"detail": exc.errors()},
        )

    @fastapi_app.exception_handler(UnsupportedRepositoryError)
    async def unsupported_repo_exception_handler(
        _request: Request, exc: UnsupportedRepositoryError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @fastapi_app.exception_handler(JobConflictError)
    async def job_conflict_exception_handler(
        _request: Request, exc: JobConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @fastapi_app.exception_handler(ValueError)
    @fastapi_app.exception_handler(LLMProviderError)
    @fastapi_app.exception_handler(ContainerExecutionError)
    async def domain_error_exception_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )

    @fastapi_app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:  # pragma: no cover - defensive fallback
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": f"Internal server error: {exc}"},
        )

    # Starlette executes middlewares in reverse order of registration:
    # Request -> CORSMiddleware -> _RequestGuardAndSPAMiddleware -> _PathNormalizationMiddleware -> FastAPI Router
    fastapi_app.add_middleware(_PathNormalizationMiddleware)
    fastapi_app.add_middleware(_RequestGuardAndSPAMiddleware)

    allowed_origins = settings.cors_allowed_origins
    if allowed_origins:
        allow_creds = (
            False
            if "*" in allowed_origins
            else settings.cors_allow_credentials
        )
        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=allow_creds,
            allow_methods=settings.cors_allowed_methods,
            allow_headers=settings.cors_allowed_headers,
            expose_headers=["Location", "X-Request-ID"],
            max_age=settings.cors_max_age_seconds,
        )

    return fastapi_app


app = create_app()


class _CodeReviveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _StdlibApiHandler(BaseHTTPRequestHandler):
    """Backwards-compatible stdlib HTTP handler delegating directly to the FastAPI ASGI app."""

    asgi_app: FastAPI = app
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _dispatch_to_fastapi(self, method: str) -> None:
        raw_length = self.headers.get("Content-Length")
        body_bytes = b""
        oversized_or_invalid_length = False

        if raw_length is not None:
            try:
                parsed_len = int(raw_length)
            except ValueError:
                oversized_or_invalid_length = True
                parsed_len = 0
            max_bytes = max(MAX_REQUEST_BODY_BYTES, settings.max_request_body_bytes)
            if parsed_len < 0 or parsed_len > max_bytes:
                oversized_or_invalid_length = True
            elif parsed_len > 0:
                body_bytes = self.rfile.read(parsed_len)

        parsed_url = urlsplit(self.path or "/")
        raw_headers: List[Tuple[bytes, bytes]] = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in self.headers.items()
        ]
        scope: Dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": unquote(parsed_url.path or "/"),
            "raw_path": (parsed_url.path or "/").encode("latin-1"),
            "query_string": (parsed_url.query or "").encode("latin-1"),
            "root_path": "",
            "headers": raw_headers,
            "client": self.client_address,
            "server": self.server.server_address,
        }

        response_status = int(HTTPStatus.INTERNAL_SERVER_ERROR)
        response_headers: List[Tuple[bytes, bytes]] = []
        response_chunks: List[bytes] = []
        body_sent = False

        async def receive() -> Dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: Dict[str, Any]) -> None:
            nonlocal response_status, response_headers
            if message["type"] == "http.response.start":
                response_status = int(message["status"])
                response_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    response_chunks.append(chunk)

        asyncio.run(self.asgi_app(scope, receive, send))

        full_body = b"".join(response_chunks)
        self.send_response(response_status)

        has_content_length = False
        for hk, hv in response_headers:
            key_str = hk.decode("latin-1")
            val_str = hv.decode("latin-1")
            if key_str.lower() == "content-length":
                has_content_length = True
            self.send_header(key_str, val_str)

        if not has_content_length:
            self.send_header("Content-Length", str(len(full_body)))

        if oversized_or_invalid_length:
            self.close_connection = True
            self.send_header("Connection", "close")

        self.end_headers()
        if method.upper() != "HEAD" and full_body:
            self.wfile.write(full_body)
        self.wfile.flush()

    def do_OPTIONS(self) -> None:
        self._dispatch_to_fastapi("OPTIONS")

    def do_GET(self) -> None:
        self._dispatch_to_fastapi("GET")

    def do_HEAD(self) -> None:
        self._dispatch_to_fastapi("HEAD")

    def do_POST(self) -> None:
        self._dispatch_to_fastapi("POST")

    def do_PUT(self) -> None:
        self._dispatch_to_fastapi("PUT")

    def do_PATCH(self) -> None:
        self._dispatch_to_fastapi("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch_to_fastapi("DELETE")


def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    resolved_host = host or settings.api_host
    if port is not None:
        resolved_port = port
    else:
        env_port = os.environ.get("PORT")
        resolved_port = int(env_port) if env_port else settings.api_port

    uvicorn.run(app, host=resolved_host, port=resolved_port)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodeRevive Backend FastAPI Server")
    parser.add_argument("--host", default=None, help=f"Bind host (default: {settings.api_host})")
    parser.add_argument("--port", type=int, default=None, help=f"Bind port (default: {settings.api_port})")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    serve(host=args.host, port=args.port)
