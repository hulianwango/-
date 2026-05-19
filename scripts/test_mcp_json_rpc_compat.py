from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_bearer_token_from_config() -> str:
    config_path = os.path.join(os.getcwd(), "config.local.yaml")
    if not os.path.exists(config_path):
        config_path = os.path.join(os.getcwd(), "config.example.yaml")
    try:
        import yaml

        with open(config_path, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return str((config.get("mcp") or {}).get("bearer_token") or "")
    except Exception:
        return ""


def _post_mcp(base_url: str, token: str, payload: Any) -> tuple[int, bytes]:
    request = Request(
        base_url.rstrip("/") + "/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _json_body(body: bytes) -> Any:
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _post_json_rpc(base_url: str, token: str, payload: Any, label: str) -> Any:
    status, body = _post_mcp(base_url, token, payload)
    _check(status == 200, f"{label} must return HTTP 200, got {status}: {body!r}")
    return _json_body(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test MCP JSON-RPC compatibility.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MCP_BASE_URL", "http://127.0.0.1:8000"),
        help="Running FastAPI server base URL.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MCP_ACCESS_TOKEN") or os.environ.get("MCP_BEARER_TOKEN") or "",
        help="Bearer token for authenticated MCP requests.",
    )
    parser.add_argument(
        "--query",
        default="AuNP Er3+ 520 nm red emission",
        help="Search query for the tools/call search_papers compatibility test.",
    )
    parser.add_argument(
        "--allow-empty-results",
        action="store_true",
        help="Allow search_papers to return an empty array when the local index has no match.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    token = args.token or _load_bearer_token_from_config()
    if not token:
        raise AssertionError("Provide --token, MCP_ACCESS_TOKEN, MCP_BEARER_TOKEN, or mcp.bearer_token in config.")

    notification = _post_json_rpc(
        base_url,
        token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "notifications/initialized",
    )
    _check(
        notification is None or notification.get("result") == {},
        "notifications/initialized must return empty body or result={}",
    )
    print("OK notifications/initialized without id")

    unknown_notification = _post_json_rpc(
        base_url,
        token,
        {"jsonrpc": "2.0", "method": "notifications/unknown"},
        "unknown notification",
    )
    _check(unknown_notification is None, "unknown notification without id should not return an error")
    print("OK unknown notification without id")

    ping = _post_json_rpc(
        base_url,
        token,
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        "ping",
    )
    _check(ping.get("id") == 10 and ping.get("result") == {}, "ping must return result={}")
    print("OK ping")

    missing_params_tools_list = _post_json_rpc(
        base_url,
        token,
        {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
        "tools/list without params",
    )
    tools = missing_params_tools_list.get("result", {}).get("tools")
    _check(isinstance(tools, list) and tools, "tools/list without params must return non-empty tools")
    print(f"OK tools/list without params: {len(tools)} tools")

    unknown_request = _post_json_rpc(
        base_url,
        token,
        {"jsonrpc": "2.0", "id": 12, "method": "not/a-method"},
        "unknown request",
    )
    error = unknown_request.get("error")
    _check(error and error.get("code") == -32601, "unknown request must return JSON-RPC -32601 error")
    print("OK unknown request returns JSON-RPC error")

    search = _post_json_rpc(
        base_url,
        token,
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "search_papers",
                "arguments": {
                    "query": args.query,
                    "limit": 5,
                },
            },
        },
        "tools/call search_papers",
    )
    content = search.get("result", {}).get("content")
    _check(isinstance(content, list) and content, "tools/call search_papers must return content")
    results = json.loads(content[0]["text"])
    print(json.dumps(results, ensure_ascii=False, indent=2))
    _check(isinstance(results, list), "search_papers content text must decode to a list")
    if not args.allow_empty_results:
        _check(bool(results), "search_papers must return at least one result")
    print(f"OK tools/call search_papers: {len(results)} result(s)")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
