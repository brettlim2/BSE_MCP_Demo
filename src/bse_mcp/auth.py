"""API-key gate for the public MCP endpoint.

The server holds a real Meltwater key, so the endpoint must not be open. Clients
authenticate with a bearer token (Authorization: Bearer <key>) or X-API-Key
header, checked against the MCP_API_KEYS allowlist. If no keys are configured the
gate is disabled (local development only).
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings

_OPEN_PATHS = {"/healthz", "/health", "/"}


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    return None


def _is_allowed(token: str, allowlist: list[str]) -> bool:
    # Constant-time compare against each configured key.
    return any(hmac.compare_digest(token, key) for key in allowlist)


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next):
        if not self._settings.auth_enabled:
            return await call_next(request)
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)

        token = _extract_token(request)
        if not token or not _is_allowed(token, self._settings.mcp_api_keys):
            return JSONResponse(
                {"error": "unauthorized", "detail": "Valid API key required."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
