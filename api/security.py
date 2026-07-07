from __future__ import annotations

import os

from fastapi import HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader

API_KEY_ENV = "APPLE_API_KEY"
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def configured_api_key() -> str | None:
    value = os.getenv(API_KEY_ENV)
    return value if value else None


async def require_api_key(
    header_api_key: str | None = Security(_api_key_header),
    query_api_key: str | None = Query(None, alias="api_key", include_in_schema=False),
) -> None:
    """Protect APPLE endpoints when APPLE_API_KEY is configured.

    Header auth is preferred:
        X-API-Key: <key>

    Query auth is also accepted for browser EventSource/SSE clients, because
    the native EventSource API cannot set custom headers.
    """
    expected = configured_api_key()
    if expected is None:
        return

    provided = header_api_key or query_api_key
    if provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
