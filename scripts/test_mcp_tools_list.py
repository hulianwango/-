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


def _post_json_rpc(
    base_url: str,
    endpoint: str,
    token: str,
    rpc_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": params or {},
    }
    request = Request(
        base_url.rstrip("/") + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()

    if status != 200:
        raise AssertionError(f"{method} returned HTTP {status}: {body.decode('utf-8', errors='replace')}")

    data = json.loads(body.decode("utf-8"))
    if data.get("jsonrpc") != "2.0":
        raise AssertionError(f"{method} response missing jsonrpc=2.0")
    if data.get("id") != rpc_id:
        raise AssertionError(f"{method} response did not echo id {rpc_id}")
    if "error" in data:
        raise AssertionError(f"{method} returned JSON-RPC error: {data['error']}")
    if "result" not in data:
        raise AssertionError(f"{method} response missing result")
    return data


def _check_tool(tool: Any, index: int) -> str:
    if not isinstance(tool, dict):
        raise AssertionError(f"tool #{index} is not an object")

    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise AssertionError(f"tool #{index} missing non-empty name")
    if not isinstance(tool.get("description"), str) or not tool["description"]:
        raise AssertionError(f"tool {name} missing non-empty description")
    if "input_schema" in tool:
        raise AssertionError(f"tool {name} uses input_schema instead of inputSchema")

    input_schema = tool.get("inputSchema")
    if not isinstance(input_schema, dict):
        raise AssertionError(f"tool {name} missing inputSchema")
    if input_schema.get("type") != "object":
        raise AssertionError(f"tool {name} inputSchema.type must be object")
    if not isinstance(input_schema.get("properties"), dict):
        raise AssertionError(f"tool {name} inputSchema.properties must be an object")
    if not isinstance(input_schema.get("required"), list):
        raise AssertionError(f"tool {name} inputSchema.required must be an array")
    return name


def _check_search_papers_call(
    base_url: str,
    endpoint: str,
    token: str,
    query: str,
    limit: int,
    allow_empty_results: bool,
) -> None:
    payload = _post_json_rpc(
        base_url,
        endpoint,
        token,
        3,
        "tools/call",
        {
            "name": "search_papers",
            "arguments": {
                "query": query,
                "limit": limit,
            },
        },
    )
    result = payload["result"]
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise AssertionError("tools/call search_papers result.content must be a non-empty array")

    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise AssertionError("tools/call search_papers first content item must be text")

    text = first.get("text")
    if not isinstance(text, str) or not text:
        raise AssertionError("tools/call search_papers text content must be non-empty")

    search_results = json.loads(text)
    print(json.dumps(search_results, ensure_ascii=False, indent=2))
    if not isinstance(search_results, list):
        raise AssertionError("tools/call search_papers text must decode to a result array")
    if not allow_empty_results and not search_results:
        raise AssertionError("tools/call search_papers returned an empty result array")

    print(f"OK tools/call search_papers returned {len(search_results)} result(s)")


def _check_endpoint(
    base_url: str,
    endpoint: str,
    token: str,
    query: str,
    limit: int,
    allow_empty_results: bool,
) -> None:
    print(f"Testing MCP endpoint {endpoint}")
    initialize = _post_json_rpc(base_url, endpoint, token, 1, "initialize")
    capabilities = initialize["result"].get("capabilities")
    if not isinstance(capabilities, dict):
        raise AssertionError("initialize result.capabilities must be an object")
    if capabilities.get("tools") != {}:
        raise AssertionError("initialize capabilities.tools must be an empty object")
    print("OK initialize declares tools capability")

    tools_list = _post_json_rpc(base_url, endpoint, token, 2, "tools/list")
    tools = tools_list["result"].get("tools")
    print(json.dumps(tools, ensure_ascii=False, indent=2))

    if not isinstance(tools, list):
        raise AssertionError("tools/list result.tools must be an array")
    if not tools:
        raise AssertionError("tools/list result.tools must not be empty")

    names = {_check_tool(tool, index) for index, tool in enumerate(tools)}
    required = {"search_papers", "read_text_chunks", "save_annotation_draft"}
    missing = sorted(required - names)
    if missing:
        raise AssertionError(f"tools/list missing required tools: {', '.join(missing)}")

    print(f"OK tools/list returned {len(tools)} valid tools")
    _check_search_papers_call(
        base_url,
        endpoint,
        token,
        query,
        limit,
        allow_empty_results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Test MCP initialize, tools/list, and tools/call responses.")
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
        help="Search query for the tools/call search_papers smoke test.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Search result limit for the tools/call search_papers smoke test.",
    )
    parser.add_argument(
        "--allow-empty-results",
        action="store_true",
        help="Allow search_papers to return an empty array when the local index has no match.",
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        choices=["/mcp", "/"],
        help="Endpoint to test. Defaults to both /mcp and /.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    token = args.token or _load_bearer_token_from_config()
    if not token:
        raise AssertionError("Provide --token, MCP_ACCESS_TOKEN, MCP_BEARER_TOKEN, or mcp.bearer_token in config.")

    endpoints = args.endpoint or ["/mcp", "/"]
    for endpoint in endpoints:
        _check_endpoint(
            base_url,
            endpoint,
            token,
            args.query,
            args.limit,
            args.allow_empty_results,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
