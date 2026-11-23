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
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

backend_root = Path(__file__).resolve().parents[1]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.api.jobs import JobConflictError
from app.api.router import normalize_path
from app.api.routes import api_router
from app.config import settings
from app.ingestion.pipeline import UnsupportedRepositoryError
from app.llm.base import LLMProviderError
from app.sandbox.docker_runner import ContainerExecutionError

logger = logging.getLogger("coderevive.api")

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


def _resolve_spa_asset(
    raw_path: str,
    dist_dir: Optional[Path],
) -> Optional[Tuple[int, bytes, str, Dict[str, str]]]:
    """Serve compiled Angular static assets and HTML5 PathLocationStrategy fallback."""
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
                int(status.HTTP_200_OK),
                candidate.read_bytes(),
                mime,
                {"Cache-Control": cache},
            )
        if Path(rel_part).suffix.lower() in _STATIC_ASSET_EXTENSIONS:
            return None

    return (
        int(status.HTTP_200_OK),
        index_file.read_bytes(),
        "text/html; charset=utf-8",
        {"Cache-Control": "no-cache"},
    )


class CustomCORSMiddleware(CORSMiddleware):
    """CORS middleware attaching X-Request-ID to preflight responses."""

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code == 200:
            req_id = request_headers.get("x-request-id") or uuid4().hex[:12]
            response.headers["X-Request-ID"] = req_id
        return response


class PathNormalizationAndSPAMiddleware:
    """ASGI middleware to canonicalize paths (/api/v1/ -> /api/, collapse slashes) and serve SPA assets."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path", "/")
        norm_path = normalize_path(raw_path)

        # Handle HEAD requests by routing as GET and stripping body per RFC 7231
        is_head = scope.get("method") == "HEAD"
        if is_head:
            scope["method"] = "GET"

        # Default Content-Type to application/json for POST/PUT/PATCH if body is present but header missing
        if scope.get("method") in ("POST", "PUT", "PATCH"):
            raw_headers = list(scope.get("headers", []))
            has_ct = any(k.lower() == b"content-type" for k, v in raw_headers)
            has_cl = any(k.lower() == b"content-length" and v != b"0" for k, v in raw_headers)
            if not has_ct and has_cl:
                raw_headers.append((b"content-type", b"application/json"))
                scope["headers"] = raw_headers

        # Angular SPA handling
        method = scope.get("method", "GET").upper()
        if method in ("GET", "HEAD") and settings.frontend_dist_dir:
            spa = _resolve_spa_asset(raw_path, settings.frontend_dist_dir)
            if spa is not None:
                spa_status, raw_bytes, mime, spa_headers = spa
                body = b"" if is_head else raw_bytes
                response_headers = [
                    (b"content-type", mime.encode("latin-1")),
                    (b"content-length", str(len(raw_bytes)).encode("latin-1")),
                    (b"x-content-type-options", b"nosniff"),
                ]
                for hk, hv in spa_headers.items():
                    response_headers.append((hk.lower().encode("latin-1"), hv.encode("latin-1")))

                await send({
                    "type": "http.response.start",
                    "status": spa_status,
                    "headers": response_headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                })
                return

        # Rewrite path in ASGI scope for standard routing
        scope["path"] = norm_path

        if is_head:
            async def head_send(message: Dict[str, Any]) -> None:
                if message["type"] == "http.response.body":
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                else:
                    await send(message)

            await self.app(scope, receive, head_send)
            return

        await self.app(scope, receive, send)


class RequestHeadersAndLimitMiddleware(BaseHTTPMiddleware):
    """Enforces body limits and injects security headers and X-Request-ID."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        super().__init__(app)
        self.max_body_bytes = max_body_bytes

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        req_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]

        # Enforce request body size limit
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
                if length > self.max_body_bytes:
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={"detail": f"Request body exceeds {self.max_body_bytes} bytes."},
                        headers={"X-Request-ID": req_id, "X-Content-Type-Options": "nosniff"},
                    )
                if length < 0:
                    return JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content={"detail": "Negative Content-Length header."},
                        headers={"X-Request-ID": req_id, "X-Content-Type-Options": "nosniff"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header."},
                    headers={"X-Request-ID": req_id, "X-Content-Type-Options": "nosniff"},
                )

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def create_app() -> FastAPI:
    """Build and configure the production FastAPI application."""
    app_instance = FastAPI(
        title="CodeRevive Migration Engine API",
        description="Automated Spring Boot 2.x to 3.x Migration Engine",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # -----------------------------------------------------------------------
    # Exception Handlers
    # -----------------------------------------------------------------------

    @app_instance.exception_handler(JobConflictError)
    async def job_conflict_handler(_request: Request, exc: JobConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app_instance.exception_handler(UnsupportedRepositoryError)
    async def unsupported_repo_handler(_request: Request, exc: UnsupportedRepositoryError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app_instance.exception_handler(LLMProviderError)
    async def llm_error_handler(_request: Request, exc: LLMProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )

    @app_instance.exception_handler(ContainerExecutionError)
    async def container_error_handler(_request: Request, exc: ContainerExecutionError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )

    @app_instance.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        for err in exc.errors():
            if err.get("type") in ("json_invalid", "value_error.jsondecode"):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": f"Invalid JSON body: {err.get('msg', 'Invalid JSON')}"},
                )
            if err.get("type") in ("dict_type", "model_type", "model_attributes_type") and (
                not err.get("loc") or tuple(err.get("loc", ())) in (("body",), ("body", "payload"))
            ):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Request body must be a JSON object."},
                )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exc.errors()},
        )

    @app_instance.exception_handler(ValidationError)
    async def pydantic_validation_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exc.errors()},
        )

    @app_instance.exception_handler(json.JSONDecodeError)
    async def json_decode_handler(_request: Request, exc: json.JSONDecodeError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": f"Invalid JSON body: {exc}"},
        )

    @app_instance.exception_handler(404)
    async def not_found_handler(request: Request, exc: HTTPException) -> JSONResponse:
        clean_path = normalize_path(str(request.url.path))
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail != "Not Found" else "Not found"
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": detail, "path": clean_path},
        )

    @app_instance.exception_handler(405)
    async def method_not_allowed_handler(request: Request, exc: HTTPException) -> JSONResponse:
        headers = dict(exc.headers or {})
        allow = headers.get("allow") or headers.get("Allow") or "GET, HEAD"
        allowed_list = [m.strip() for m in allow.split(",") if m.strip()]
        if "GET" in allowed_list and "HEAD" not in allowed_list:
            allowed_list.append("HEAD")
        headers["Allow"] = ", ".join(allowed_list)
        clean_path = normalize_path(str(request.url.path))
        return JSONResponse(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            content={
                "detail": f"Method '{request.method}' not allowed for '{clean_path}'.",
                "allowed_methods": allowed_list,
            },
            headers=headers,
        )

    @app_instance.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error processing %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    # -----------------------------------------------------------------------
    # Routers
    # -----------------------------------------------------------------------
    app_instance.include_router(api_router)

    # -----------------------------------------------------------------------
    # Middlewares (applied in reverse order of addition)
    # -----------------------------------------------------------------------
    app_instance.add_middleware(
        RequestHeadersAndLimitMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )

    if settings.cors_allowed_origins:
        app_instance.add_middleware(
            CustomCORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=settings.cors_allowed_methods,
            allow_headers=settings.cors_allowed_headers,
            max_age=settings.cors_max_age_seconds,
            expose_headers=["Location", "X-Request-ID"],
        )

    app_instance.add_middleware(PathNormalizationAndSPAMiddleware)

    return app_instance


app = create_app()


# ---------------------------------------------------------------------------
# Compatibility Bridge Server for tests and synchronous callers
# ---------------------------------------------------------------------------


class _CodeReviveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _StdlibApiHandler(BaseHTTPRequestHandler):
    """Bridges HTTP connections to the FastAPI application."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _handle_asgi(self) -> None:
        raw_path = self.path or "/"
        parsed = urlsplit(raw_path)
        path = parsed.path or "/"
        query_string = parsed.query.encode("latin-1") if parsed.query else b""

        headers: List[Tuple[bytes, bytes]] = []
        content_length = None
        for k, v in self.headers.items():
            k_lower = k.lower()
            if k_lower == "content-length":
                try:
                    content_length = int(v)
                except ValueError:
                    content_length = -1
            headers.append((k_lower.encode("latin-1"), v.encode("latin-1")))

        body_bytes = b""
        if content_length is not None:
            if content_length < 0:
                self.send_response(status.HTTP_400_BAD_REQUEST)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"detail": "Negative Content-Length header."}')
                return
            if content_length > settings.max_request_body_bytes:
                self.send_response(status.HTTP_413_CONTENT_TOO_LARGE)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(
                    f'{{"detail": "Request body exceeds {settings.max_request_body_bytes} bytes."}}'.encode()
                )
                return
            if content_length > 0:
                body_bytes = self.rfile.read(content_length)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": self.command,
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": query_string,
            "headers": headers,
            "client": self.client_address,
            "server": self.server.server_address,
            "scheme": "http",
        }

        body_sent = False
        response_started = False
        response_status = 200
        response_headers: List[Tuple[bytes, bytes]] = []
        response_chunks: List[bytes] = []
        disconnect_event = asyncio.Event()

        async def receive() -> Dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            await disconnect_event.wait()
            return {"type": "http.disconnect"}

        async def send(message: Dict[str, Any]) -> None:
            nonlocal response_started, response_status, response_headers
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                response_headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    disconnect_event.set()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(app(scope, receive, send))
        finally:
            loop.close()

        self.send_response(response_status)
        for hk, hv in response_headers:
            self.send_header(hk.decode("latin-1"), hv.decode("latin-1"))
        self.end_headers()

        if self.command != "HEAD":
            full_body = b"".join(response_chunks)
            if full_body:
                self.wfile.write(full_body)
        self.wfile.flush()

    def do_GET(self) -> None:
        self._handle_asgi()

    def do_HEAD(self) -> None:
        self._handle_asgi()

    def do_POST(self) -> None:
        self._handle_asgi()

    def do_PUT(self) -> None:
        self._handle_asgi()

    def do_PATCH(self) -> None:
        self._handle_asgi()

    def do_DELETE(self) -> None:
        self._handle_asgi()

    def do_OPTIONS(self) -> None:
        self._handle_asgi()


def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Production server startup using uvicorn."""
    resolved_host = host or settings.api_host
    if port is not None:
        resolved_port = port
    else:
        env_port = os.environ.get("PORT")
        resolved_port = int(env_port) if env_port else settings.api_port

    print(f"CodeRevive API listening on http://{resolved_host}:{resolved_port}")
    uvicorn.run("app.main:app", host=resolved_host, port=resolved_port, log_level="info")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodeRevive Backend API Server")
    parser.add_argument("--host", default=None, help=f"Bind host (default: {settings.api_host})")
    parser.add_argument("--port", type=int, default=None, help=f"Bind port (default: {settings.api_port})")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    serve(host=args.host, port=args.port)
