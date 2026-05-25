from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request

from .config import PROJECT_ROOT, settings


READ_SCOPE = "literature:read"
WRITE_DRAFT_SCOPE = "literature:write_draft"
MOVE_FILE_SCOPE = "literature:move_file"
SUPPORTED_SCOPES = [READ_SCOPE, WRITE_DRAFT_SCOPE, MOVE_FILE_SCOPE]

OAUTH_DATA_DIR = PROJECT_ROOT / "data"
CLIENTS_PATH = OAUTH_DATA_DIR / "oauth_clients.json"
CODES_PATH = OAUTH_DATA_DIR / "oauth_codes.json"
TOKENS_PATH = OAUTH_DATA_DIR / "oauth_tokens.json"

_LOCK = threading.Lock()


def scope_string(scopes: list[str] | set[str] | tuple[str, ...] | None = None) -> str:
    selected = list(scopes or SUPPORTED_SCOPES)
    return " ".join(scope for scope in SUPPORTED_SCOPES if scope in selected)


def normalize_scopes(raw_scope: str | None) -> list[str]:
    requested = [scope for scope in (raw_scope or "").split() if scope]
    if not requested:
        return list(SUPPORTED_SCOPES)

    normalized: list[str] = []
    for scope in requested:
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(f"unsupported scope: {scope}")
        if scope not in normalized:
            normalized.append(scope)
    return normalized


def get_public_base_url(request: Request) -> str:
    if settings.oauth_public_base_url:
        configured = settings.oauth_public_base_url.rstrip("/")
        parsed = urlsplit(configured)
        if not parsed.scheme:
            return f"https://{configured}".rstrip("/")
        return configured

    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme
    return f"{scheme}://{host}".rstrip("/")


def resource_metadata_url(request: Request) -> str:
    return f"{get_public_base_url(request)}/.well-known/oauth-protected-resource"


def mcp_resource_url(request: Request) -> str:
    return f"{get_public_base_url(request)}/mcp"


def pkce_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def secrets_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _load_collection(path: Path, key: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle) or {}
    if isinstance(loaded, dict) and isinstance(loaded.get(key), dict):
        return dict(loaded[key])
    if isinstance(loaded, dict):
        return dict(loaded)
    return {}


def _save_collection(path: Path, key: str, values: dict[str, Any]) -> None:
    OAUTH_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {key: values}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def register_client(metadata: dict[str, Any]) -> dict[str, Any]:
    client_id = "mcp_" + secrets.token_urlsafe(24)
    now = int(time.time())
    clean_metadata = dict(metadata)
    clean_metadata["token_endpoint_auth_method"] = "none"

    with _LOCK:
        clients = _load_collection(CLIENTS_PATH, "clients")
        clients[client_id] = {
            "client_id": client_id,
            "client_id_issued_at": now,
            "metadata": clean_metadata,
        }
        _save_collection(CLIENTS_PATH, "clients", clients)

    return {
        "client_id": client_id,
        "client_id_issued_at": now,
        "token_endpoint_auth_method": "none",
        "grant_types": clean_metadata.get("grant_types") or ["authorization_code"],
        "response_types": clean_metadata.get("response_types") or ["code"],
        "redirect_uris": clean_metadata.get("redirect_uris") or [],
        "scope": clean_metadata.get("scope") or scope_string(),
    }


def get_client(client_id: str) -> dict[str, Any] | None:
    with _LOCK:
        clients = _load_collection(CLIENTS_PATH, "clients")
    client = clients.get(client_id)
    return dict(client) if isinstance(client, dict) else None


def redirect_uri_allowed(client: dict[str, Any], redirect_uri: str) -> bool:
    metadata = client.get("metadata") or {}
    redirect_uris = metadata.get("redirect_uris") or []
    if not redirect_uris:
        return True
    return redirect_uri in redirect_uris


def create_authorization_code(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    code_challenge: str,
    code_challenge_method: str,
    resource: str,
) -> str:
    code = "code_" + secrets.token_urlsafe(32)
    now = int(time.time())
    record = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope_string(scopes),
        "scopes": scopes,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "resource": resource,
        "expires_at": now + settings.oauth_code_expires_seconds,
        "used": False,
        "created_at": now,
    }

    with _LOCK:
        codes = _load_collection(CODES_PATH, "codes")
        codes[code] = record
        _save_collection(CODES_PATH, "codes", codes)
    return code


def get_authorization_code(code: str) -> dict[str, Any] | None:
    with _LOCK:
        codes = _load_collection(CODES_PATH, "codes")
    record = codes.get(code)
    return dict(record) if isinstance(record, dict) else None


def mark_authorization_code_used(code: str) -> None:
    with _LOCK:
        codes = _load_collection(CODES_PATH, "codes")
        record = codes.get(code)
        if isinstance(record, dict):
            record["used"] = True
            codes[code] = record
            _save_collection(CODES_PATH, "codes", codes)


def issue_access_token(
    *,
    client_id: str,
    scopes: list[str],
    resource: str,
) -> dict[str, Any]:
    access_token = "tok_" + secrets.token_urlsafe(48)
    now = int(time.time())
    expires_in = settings.oauth_token_expires_seconds
    record = {
        "client_id": client_id,
        "scope": scope_string(scopes),
        "scopes": scopes,
        "resource": resource,
        "expires_at": now + expires_in,
        "created_at": now,
    }

    with _LOCK:
        tokens = _load_collection(TOKENS_PATH, "tokens")
        tokens[access_token] = record
        _save_collection(TOKENS_PATH, "tokens", tokens)

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": record["scope"],
    }


def validate_access_token(access_token: str) -> dict[str, Any] | None:
    if not settings.oauth_enabled:
        return None

    with _LOCK:
        tokens = _load_collection(TOKENS_PATH, "tokens")
    record = tokens.get(access_token)
    if not isinstance(record, dict):
        return None
    if int(record.get("expires_at") or 0) <= int(time.time()):
        return None
    return dict(record)
