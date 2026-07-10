"""Shared API-key authentication helpers."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from flow2api.services.auth_keys import get_api_key_by_token


def parse_bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def token_from_request(request: Request) -> str | None:
    token = parse_bearer_token(request.headers.get("authorization"))
    if token:
        return token
    for key in ("access_token", "token"):
        raw = request.query_params.get(key)
        if raw:
            value = str(raw).strip()
            if value:
                return value
    return None


def resolve_api_key_id(token: str | None) -> int | None:
    if not token:
        return None
    return get_api_key_by_token(token)


def path_requires_api_key(path: str) -> bool:
    if path.startswith("/static/"):
        return False
    if path in ("/", "/favicon.ico"):
        return False
    if path.startswith("/admin"):
        return False
    if path.startswith("/api/admin/"):
        return False
    if path.startswith("/api/ext/"):
        return False
    if path.startswith("/api/internal/captcha/"):
        return False
    protected_prefixes = (
        "/api/",
        "/video/",
        "/image/",
        "/outputs/",
        "/inputs/",
        "/media/",
    )
    return any(path.startswith(prefix) for prefix in protected_prefixes)


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    token = parse_bearer_token(authorization)
    if not token:
        raise HTTPException(401, "missing_bearer_token")
    return token


def auth_key_id(token: str = Depends(bearer_token)) -> int:
    key_id = resolve_api_key_id(token)
    if not key_id:
        raise HTTPException(401, "invalid_api_key")
    return key_id
