"""Routing utilities and compatibility definitions for the CodeRevive API."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException as APIError
from fastapi.responses import JSONResponse as APIResponse


def normalize_path(
    raw_path: str,
    version_aliases: Iterable[str] = ("/api/v1",),
) -> str:
    """Extract, normalize, and canonicalize a request path.

    * Strips the query string.
    * Collapses duplicate slashes.
    * Strips a trailing slash (except for ``"/"``).
    * Maps versioned prefixes (e.g. ``/api/v1/...``) to ``/api/...``.
    """
    parsed = urlsplit(raw_path or "/")
    path = parsed.path or "/"

    # Collapse duplicate slashes
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


__all__ = [
    "APIRouter",
    "APIError",
    "APIResponse",
    "normalize_path",
]
