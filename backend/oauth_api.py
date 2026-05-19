from __future__ import annotations

import html
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .config import settings
from .oauth_store import (
    SUPPORTED_SCOPES,
    create_authorization_code,
    get_authorization_code,
    get_client,
    get_public_base_url,
    issue_access_token,
    mark_authorization_code_used,
    mcp_resource_url,
    normalize_scopes,
    pkce_s256,
    redirect_uri_allowed,
    register_client,
    scope_string,
    secrets_equal,
)


router = APIRouter()


class OAuthRequestError(ValueError):
    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


def _same_resource(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


def _oauth_error(
    error: str,
    description: str,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
    )


def _authorization_server_metadata(request: Request) -> dict[str, Any]:
    base_url = get_public_base_url(request)
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": SUPPORTED_SCOPES,
    }


async def _read_request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        return dict(payload) if isinstance(payload, dict) else {}

    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _verify_password(username: str, password: str) -> bool:
    expected_username = settings.oauth_username
    password_hash = settings.oauth_password_hash
    if not expected_username or not password_hash:
        return False
    if not secrets_equal(username, expected_username):
        return False

    try:
        import bcrypt

        return bool(bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")))
    except Exception:
        pass

    try:
        from passlib.context import CryptContext

        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        return bool(pwd_context.verify(password, password_hash))
    except Exception:
        return False


def _validate_authorize_params(data: dict[str, Any], request: Request) -> dict[str, Any]:
    if not settings.oauth_enabled:
        raise OAuthRequestError("temporarily_unavailable", "OAuth is disabled.")
    if data.get("response_type") != "code":
        raise OAuthRequestError("unsupported_response_type", "response_type must be code.")
    if data.get("code_challenge_method") != "S256":
        raise OAuthRequestError("invalid_request", "code_challenge_method must be S256.")

    client_id = str(data.get("client_id") or "")
    redirect_uri = str(data.get("redirect_uri") or "")
    code_challenge = str(data.get("code_challenge") or "")
    if not client_id or not redirect_uri or not code_challenge:
        raise OAuthRequestError("invalid_request", "client_id, redirect_uri, and code_challenge are required.")

    client = get_client(client_id)
    if client is None:
        raise OAuthRequestError("invalid_client", "Unknown client_id.", status.HTTP_400_BAD_REQUEST)
    if not redirect_uri_allowed(client, redirect_uri):
        raise OAuthRequestError("invalid_request", "redirect_uri is not registered for this client.")

    try:
        scopes = normalize_scopes(str(data.get("scope") or ""))
    except ValueError as exc:
        raise OAuthRequestError("invalid_scope", str(exc)) from exc

    base_url = get_public_base_url(request)
    resource_url = mcp_resource_url(request)
    resource = str(data.get("resource") or resource_url).rstrip("/")
    if not (_same_resource(resource, resource_url) or _same_resource(resource, base_url)):
        raise OAuthRequestError("invalid_target", "resource must match this MCP server.")
    resource = resource_url

    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope_string(scopes),
        "scopes": scopes,
        "state": str(data.get("state") or ""),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }


def _render_authorize_page(data: dict[str, Any], error: str = "") -> str:
    hidden_names = [
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "resource",
    ]
    hidden_fields = "\n".join(
        f'<input type="hidden" name="{name}" value="{html.escape(str(data.get(name) or ""), quote=True)}">'
        for name in hidden_names
    )
    error_block = ""
    if error:
        error_block = f'<p class="error">{html.escape(error)}</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize Literature MCP</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #f7f7f4; color: #1d1d1b; }}
    main {{ width: min(420px, calc(100vw - 32px)); margin: 12vh auto; padding: 24px; background: white; border: 1px solid #ddd8cf; border-radius: 8px; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 16px; }}
    label {{ display: block; font-size: 0.9rem; margin: 14px 0 6px; }}
    input[type=text], input[type=password] {{ box-sizing: border-box; width: 100%; padding: 10px 12px; border: 1px solid #bdb8af; border-radius: 6px; font: inherit; }}
    button {{ margin-top: 18px; width: 100%; padding: 10px 12px; border: 0; border-radius: 6px; background: #244f46; color: white; font: inherit; cursor: pointer; }}
    .error {{ color: #9b1c1c; margin: 0 0 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>Authorize Literature MCP</h1>
    {error_block}
    <form method="post" action="/oauth/authorize" autocomplete="off">
      {hidden_fields}
      <label for="username">Username</label>
      <input id="username" name="username" type="text" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required>
      <button type="submit">Authorize</button>
    </form>
  </main>
</body>
</html>"""


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata(request: Request) -> dict[str, Any]:
    base_url = get_public_base_url(request)
    return {
        "resource": mcp_resource_url(request),
        "authorization_servers": [base_url],
        "scopes_supported": SUPPORTED_SCOPES,
        "resource_documentation": f"{base_url}/local",
    }


@router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server_metadata(request: Request) -> dict[str, Any]:
    return _authorization_server_metadata(request)


@router.get("/.well-known/openid-configuration")
def openid_configuration(request: Request) -> dict[str, Any]:
    return _authorization_server_metadata(request)


@router.post("/oauth/register")
async def oauth_register(request: Request) -> JSONResponse:
    metadata = await request.json()
    if not isinstance(metadata, dict):
        return _oauth_error("invalid_client_metadata", "Client metadata must be a JSON object.")
    client = register_client(metadata)
    return JSONResponse(client, status_code=status.HTTP_201_CREATED)


@router.get("/oauth/authorize", response_model=None)
def oauth_authorize_form(request: Request) -> HTMLResponse | PlainTextResponse:
    data = dict(request.query_params)
    try:
        _validate_authorize_params(data, request)
    except OAuthRequestError as exc:
        return PlainTextResponse(exc.description, status_code=exc.status_code)
    return HTMLResponse(_render_authorize_page(data))


@router.post("/oauth/authorize", response_model=None)
async def oauth_authorize_submit(request: Request) -> HTMLResponse | PlainTextResponse | RedirectResponse:
    data = await _read_request_data(request)
    try:
        authorization = _validate_authorize_params(data, request)
    except OAuthRequestError as exc:
        return PlainTextResponse(exc.description, status_code=exc.status_code)

    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    if not _verify_password(username, password):
        return HTMLResponse(
            _render_authorize_page(data, "Invalid username or password."),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    code = create_authorization_code(
        client_id=authorization["client_id"],
        redirect_uri=authorization["redirect_uri"],
        scopes=authorization["scopes"],
        code_challenge=authorization["code_challenge"],
        code_challenge_method=authorization["code_challenge_method"],
        resource=authorization["resource"],
    )
    redirect_params = {"code": code}
    if authorization["state"]:
        redirect_params["state"] = authorization["state"]
    separator = "&" if "?" in authorization["redirect_uri"] else "?"
    redirect_url = authorization["redirect_uri"] + separator + urlencode(redirect_params)
    return RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/oauth/token")
async def oauth_token(request: Request) -> JSONResponse:
    data = await _read_request_data(request)
    if data.get("grant_type") != "authorization_code":
        return _oauth_error("unsupported_grant_type", "Only authorization_code is supported.")

    code = str(data.get("code") or "")
    redirect_uri = str(data.get("redirect_uri") or "")
    client_id = str(data.get("client_id") or "")
    code_verifier = str(data.get("code_verifier") or "")
    if not code or not redirect_uri or not client_id or not code_verifier:
        return _oauth_error("invalid_request", "code, redirect_uri, client_id, and code_verifier are required.")

    record = get_authorization_code(code)
    if record is None:
        return _oauth_error("invalid_grant", "Authorization code is invalid.")
    if bool(record.get("used")):
        return _oauth_error("invalid_grant", "Authorization code has already been used.")
    if int(record.get("expires_at") or 0) <= int(time.time()):
        return _oauth_error("invalid_grant", "Authorization code has expired.")
    if record.get("client_id") != client_id:
        return _oauth_error("invalid_grant", "client_id does not match authorization code.")
    if record.get("redirect_uri") != redirect_uri:
        return _oauth_error("invalid_grant", "redirect_uri does not match authorization code.")
    if record.get("code_challenge_method") != "S256":
        return _oauth_error("invalid_grant", "Unsupported PKCE method.")

    requested_resource = str(data.get("resource") or "").rstrip("/")
    code_resource = str(record.get("resource") or "").rstrip("/")
    base_url = get_public_base_url(request)
    valid_requested_resource = (
        _same_resource(requested_resource, code_resource)
        or _same_resource(requested_resource, base_url)
        or _same_resource(requested_resource, mcp_resource_url(request))
    )
    if requested_resource and not valid_requested_resource:
        return _oauth_error("invalid_target", "resource does not match authorization code.")
    if not secrets_equal(pkce_s256(code_verifier), str(record.get("code_challenge") or "")):
        return _oauth_error("invalid_grant", "PKCE verification failed.")

    mark_authorization_code_used(code)
    token = issue_access_token(
        client_id=client_id,
        scopes=list(record.get("scopes") or normalize_scopes(record.get("scope") or "")),
        resource=code_resource,
    )
    return JSONResponse(token)
