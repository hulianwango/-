from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    request_headers: dict[str, str] = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _json_body(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _header(headers: dict[str, str], name: str) -> str:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value
    return ""


def _load_bearer_token_from_config() -> str:
    config_path = os.path.join(os.getcwd(), "config.local.yaml")
    if not os.path.exists(config_path):
        config_path = os.path.join(os.getcwd(), "config.example.yaml")
    try:
        import yaml

        with open(config_path, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        mcp_config = config.get("mcp") or {}
        return str(mcp_config.get("bearer_token") or "")
    except Exception:
        return ""


def _json_rpc(
    base_url: str,
    token: str,
    rpc_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status, _headers, body = _request(
        base_url,
        "POST",
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params or {},
        },
        {"Authorization": f"Bearer {token}"},
    )
    _check(status == 200, f"{method} must return HTTP 200 after authentication")
    payload = _json_body(body)
    _check(payload.get("jsonrpc") == "2.0", f"{method} response must include jsonrpc=2.0")
    _check(payload.get("id") == rpc_id, f"{method} response must echo id")
    _check("result" in payload, f"{method} response must include result")
    _check("error" not in payload, f"{method} response must not include error")
    return payload


def _check_tools_list(base_url: str, token: str) -> None:
    initialize = _json_rpc(base_url, token, 1, "initialize")
    capabilities = initialize["result"].get("capabilities")
    _check(isinstance(capabilities, dict), "initialize result.capabilities must be an object")
    _check(isinstance(capabilities.get("tools"), dict), "initialize capabilities must declare tools")
    print("OK MCP initialize tools capability")

    tools_list = _json_rpc(base_url, token, 2, "tools/list")
    result = tools_list["result"]
    tools = result.get("tools")
    _check(isinstance(tools, list), "tools/list result.tools must be an array")
    _check(len(tools) > 0, "tools/list result.tools must not be empty")

    required_names = {"search_papers", "read_text_chunks", "save_annotation_draft"}
    seen_names: set[str] = set()
    for index, tool in enumerate(tools):
        _check(isinstance(tool, dict), f"tool #{index} must be an object")
        _check(isinstance(tool.get("name"), str) and tool["name"], f"tool #{index} needs name")
        _check(
            isinstance(tool.get("description"), str) and tool["description"],
            f"tool {tool.get('name') or index} needs description",
        )
        _check("input_schema" not in tool, f"tool {tool['name']} must not use input_schema")
        input_schema = tool.get("inputSchema")
        _check(isinstance(input_schema, dict), f"tool {tool['name']} needs inputSchema")
        _check(input_schema.get("type") == "object", f"tool {tool['name']} inputSchema must be object")
        _check(
            isinstance(input_schema.get("properties"), dict),
            f"tool {tool['name']} inputSchema needs properties",
        )
        _check(
            isinstance(input_schema.get("required"), list),
            f"tool {tool['name']} inputSchema needs required",
        )
        seen_names.add(tool["name"])

    missing = sorted(required_names - seen_names)
    _check(not missing, f"tools/list is missing required tools: {', '.join(missing)}")
    print(f"OK MCP tools/list schema: {len(tools)} tools")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test OAuth metadata for the MCP server.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MCP_BASE_URL", "http://127.0.0.1:8000"),
        help="Running FastAPI server base URL.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MCP_ACCESS_TOKEN") or os.environ.get("MCP_BEARER_TOKEN") or "",
        help="Bearer token to test authenticated MCP initialize and tools/list.",
    )
    parser.add_argument(
        "--skip-tools-list",
        action="store_true",
        help="Only test OAuth discovery and 401 challenge, not authenticated MCP tools/list.",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    bearer_token = args.token or _load_bearer_token_from_config()

    status, _headers, body = _request(base_url, "GET", "/.well-known/oauth-protected-resource")
    _check(status == 200, "protected resource metadata must return HTTP 200")
    protected_resource = _json_body(body)
    _check("resource" in protected_resource, "protected resource metadata must include resource")
    _check(
        str(protected_resource.get("resource") or "").rstrip("/").endswith("/mcp"),
        "protected resource metadata resource must point to the /mcp resource",
    )
    _check(
        "authorization_servers" in protected_resource,
        "protected resource metadata must include authorization_servers",
    )
    authorization_servers = protected_resource.get("authorization_servers")
    _check(
        isinstance(authorization_servers, list)
        and authorization_servers
        and not str(authorization_servers[0]).rstrip("/").endswith("/mcp"),
        "authorization server must remain the base URL, not /mcp",
    )
    print("OK protected resource metadata")

    status, _headers, body = _request(base_url, "GET", "/.well-known/oauth-authorization-server")
    _check(status == 200, "authorization server metadata must return HTTP 200")
    authorization_server = _json_body(body)
    _check(
        authorization_server.get("code_challenge_methods_supported") == ["S256"],
        "authorization server metadata must advertise S256 PKCE",
    )
    _check(
        authorization_server.get("token_endpoint_auth_methods_supported") == ["none"],
        "authorization server metadata must advertise public-client token auth",
    )
    print("OK authorization server metadata")

    status, _headers, body = _request(base_url, "GET", "/.well-known/openid-configuration")
    _check(status == 200, "OpenID configuration must return HTTP 200")
    openid_configuration = _json_body(body)
    _check(
        openid_configuration.get("authorization_endpoint")
        == authorization_server.get("authorization_endpoint"),
        "OpenID configuration must expose the same authorization endpoint",
    )
    _check(
        openid_configuration.get("token_endpoint") == authorization_server.get("token_endpoint"),
        "OpenID configuration must expose the same token endpoint",
    )
    print("OK OpenID configuration")

    status, _headers, body = _request(
        base_url,
        "POST",
        "/oauth/register",
        {
            "client_name": "OAuth smoke test",
            "redirect_uris": ["https://example.com/oauth/callback"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "literature:read literature:write_draft",
        },
    )
    _check(status in {200, 201}, "dynamic client registration must succeed")
    client = _json_body(body)
    client_id = str(client.get("client_id") or "")
    _check(client_id, "dynamic client registration must return client_id")
    _check(client.get("token_endpoint_auth_method") == "none", "client must be registered as public")
    print(f"OK dynamic client registration: {client_id[:10]}...")

    status, headers, _body = _request(
        base_url,
        "POST",
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    www_authenticate = _header(headers, "WWW-Authenticate")
    _check(status == 401, "unauthenticated /mcp must return HTTP 401")
    _check("resource_metadata=" in www_authenticate, "WWW-Authenticate must include resource_metadata")
    _check(
        'scope="literature:read literature:write_draft"' in www_authenticate,
        "WWW-Authenticate must include MCP OAuth scopes",
    )
    _check(
        "/.well-known/oauth-protected-resource" in www_authenticate,
        "WWW-Authenticate must point to protected resource metadata",
    )
    print("OK unauthenticated MCP challenge")

    if not args.skip_tools_list:
        _check(
            bool(bearer_token),
            "authenticated tools/list test requires --token, MCP_ACCESS_TOKEN, MCP_BEARER_TOKEN, or mcp.bearer_token in config",
        )
        _check_tools_list(base_url, bearer_token)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
