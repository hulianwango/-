from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from .config import settings
from .oauth_store import (
    SUPPORTED_SCOPES,
    resource_metadata_url,
    scope_string,
    token_fingerprint,
    validate_access_token,
)


WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+")
POSIX_PATH_RE = re.compile(r"(?<![\w])/(?:Users|home|mnt|var|tmp|opt|srv)/[^\s\"'<>|]+")

ALLOWED_PUBLIC_KEYS = {
    "paper_id",
    "title",
    "authors",
    "year",
    "journal",
    "doi",
    "page_number",
    "chunk_id",
    "snippet",
    "score",
    "draft_id",
    "status",
}

FORBIDDEN_KEYS = {
    "file_path",
    "pdf_path",
    "database_path",
    "local_folder",
    "download_url",
    "pdf_url",
    "private_notes",
    "notes",
}


@dataclass(frozen=True)
class AuthContext:
    kind: str
    scopes: frozenset[str]
    client_id: str = ""
    token_hash: str = ""


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int) -> None:
        now = time.time()
        window_start = now - 60
        events = self._events[key]
        while events and events[0] < window_start:
            events.popleft()
        if len(events) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded.",
            )
        events.append(now)


rate_limiter = InMemoryRateLimiter()


def scrub_text(value: str, max_chars: int | None = None) -> str:
    value = WINDOWS_PATH_RE.sub("[redacted-path]", value)
    value = POSIX_PATH_RE.sub("[redacted-path]", value)
    if max_chars is not None and len(value) > max_chars:
        return value[: max_chars - 3].rstrip() + "..."
    return value


def scrub_public_payload(value: Any, max_chars: int | None = None) -> Any:
    if isinstance(value, str):
        return scrub_text(value, max_chars=max_chars)
    if isinstance(value, list):
        return [scrub_public_payload(item, max_chars=max_chars) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                continue
            clean[key] = scrub_public_payload(item, max_chars=max_chars)
        return clean
    return value


def enforce_public_keys(record: dict[str, Any], extra_keys: set[str] | None = None) -> dict[str, Any]:
    allowed = set(ALLOWED_PUBLIC_KEYS)
    if extra_keys:
        allowed.update(extra_keys)
    return {key: value for key, value in record.items() if key in allowed}


def require_bearer_token(request: Request) -> None:
    expected = settings.mcp_bearer_token
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not expected or not header.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token.")
    provided = header[len(prefix) :].strip()
    if not secrets_equal(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")


def secrets_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left.encode()).digest(), hashlib.sha256(right.encode()).digest()
    )


def oauth_challenge_header(request: Request) -> str:
    return (
        'Bearer '
        f'resource_metadata="{resource_metadata_url(request)}", '
        f'scope="{scope_string(SUPPORTED_SCOPES)}"'
    )


def _unauthorized(request: Request, detail: str = "Missing or invalid token.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": oauth_challenge_header(request)},
    )


def _oauth_auth_context(token: str) -> AuthContext | None:
    record = validate_access_token(token)
    if record is None:
        return None

    raw_scopes = record.get("scopes")
    if not isinstance(raw_scopes, list):
        raw_scopes = str(record.get("scope") or "").split()
    scopes = frozenset(scope for scope in raw_scopes if scope in SUPPORTED_SCOPES)
    return AuthContext(
        kind="oauth",
        scopes=scopes,
        client_id=str(record.get("client_id") or ""),
        token_hash=token_fingerprint(token),
    )


def require_mcp_access(request: Request) -> AuthContext:
    existing = getattr(request.state, "mcp_auth", None)
    if isinstance(existing, AuthContext):
        return existing

    if not settings.mcp_require_auth:
        auth = AuthContext(kind="disabled", scopes=frozenset(SUPPORTED_SCOPES))
        request.state.mcp_auth = auth
        return auth

    expected = settings.mcp_bearer_token
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise _unauthorized(request, "Missing token.")

    provided = header[len(prefix) :].strip()
    if expected and secrets_equal(provided, expected):
        auth = AuthContext(kind="local", scopes=frozenset(SUPPORTED_SCOPES))
        request.state.mcp_auth = auth
    else:
        auth = _oauth_auth_context(provided)
        if auth is None:
            raise _unauthorized(request, "Invalid token.")
        request.state.mcp_auth = auth

    client = request.client.host if request.client else "unknown"
    rate_limiter.check(client, settings.rate_limit_per_minute)
    return auth


def require_mcp_scopes(auth: AuthContext, required_scopes: set[str]) -> None:
    if not required_scopes or auth.kind in {"local", "disabled"}:
        return
    if not required_scopes.issubset(auth.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient OAuth scope.",
        )


def require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    forwarded_headers = {
        "forwarded",
        "x-forwarded-for",
        "x-real-ip",
        "cf-connecting-ip",
    }
    if any(request.headers.get(header) for header in forwarded_headers):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only.")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only.")


def log_mcp_event(tool_name: str, status_code: int, metadata: dict[str, Any] | None = None) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    safe_meta = metadata or {}
    safe_meta = {
        key: value
        for key, value in safe_meta.items()
        if key not in FORBIDDEN_KEYS and "path" not in key.lower()
    }
    record = {
        "ts": int(time.time()),
        "tool": tool_name,
        "status_code": status_code,
        "metadata": safe_meta,
    }
    line = json.dumps(scrub_public_payload(record), ensure_ascii=False)
    with (settings.logs_dir / "mcp_access.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_mcp_rpc_event(
    method: str,
    has_id: bool,
    status_code: int,
    metadata: dict[str, Any] | None = None,
) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    safe_meta = metadata or {}
    safe_meta = {
        key: value
        for key, value in safe_meta.items()
        if key not in FORBIDDEN_KEYS and "path" not in key.lower() and "token" not in key.lower()
    }
    record = {
        "ts": int(time.time()),
        "method": scrub_text(str(method or "")),
        "has_id": bool(has_id),
        "status_code": status_code,
        "metadata": safe_meta,
    }
    line = json.dumps(scrub_public_payload(record), ensure_ascii=False)
    with (settings.logs_dir / "mcp_rpc.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
