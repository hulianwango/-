from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from .config import settings


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


def require_mcp_access(request: Request) -> None:
    require_bearer_token(request)
    client = request.client.host if request.client else "unknown"
    rate_limiter.check(client, settings.rate_limit_per_minute)


def require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
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
