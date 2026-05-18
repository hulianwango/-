from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import mcp_tools
from .security import log_mcp_event, require_mcp_access, scrub_public_payload


router = APIRouter(prefix="/mcp", dependencies=[Depends(require_mcp_access)])


class SearchPapersRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=10)


class MetadataRequest(BaseModel):
    paper_id: str


class ReadChunksRequest(BaseModel):
    paper_id: str
    chunk_ids: list[str] = Field(default_factory=list, max_length=5)


class ReadPageRequest(BaseModel):
    paper_id: str
    page_number: int = Field(ge=1)


class SaveDraftRequest(BaseModel):
    paper_id: str
    annotation_json: dict[str, Any]


class UpdateDraftRequest(BaseModel):
    draft_id: str
    annotation_json: dict[str, Any]


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_papers",
            "description": "Search indexed PDF text chunks. Empty query is rejected.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_paper_metadata",
            "description": "Return safe paper metadata by paper_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"paper_id": {"type": "string"}},
                "required": ["paper_id"],
            },
        },
        {
            "name": "read_text_chunks",
            "description": "Read up to five indexed text chunks for one paper.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string"},
                    "chunk_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                    },
                },
                "required": ["paper_id", "chunk_ids"],
            },
        },
        {
            "name": "read_page_text",
            "description": "Read one page of extracted text for one paper.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string"},
                    "page_number": {"type": "integer", "minimum": 1},
                },
                "required": ["paper_id", "page_number"],
            },
        },
        {
            "name": "save_annotation_draft",
            "description": "Save an AI annotation draft for local human review only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string"},
                    "annotation_json": {"type": "object"},
                },
                "required": ["paper_id", "annotation_json"],
            },
        },
        {
            "name": "update_annotation_draft",
            "description": "Update a pending AI annotation draft only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "annotation_json": {"type": "object"},
                },
                "required": ["draft_id", "annotation_json"],
            },
        },
    ]


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "search_papers": lambda args: mcp_tools.search_papers(
        query=str(args.get("query", "")),
        limit=int(args.get("limit", 10)),
    ),
    "get_paper_metadata": lambda args: mcp_tools.get_paper_metadata(
        paper_id=str(args.get("paper_id", ""))
    ),
    "read_text_chunks": lambda args: mcp_tools.read_text_chunks(
        paper_id=str(args.get("paper_id", "")),
        chunk_ids=list(args.get("chunk_ids") or []),
    ),
    "read_page_text": lambda args: mcp_tools.read_page_text(
        paper_id=str(args.get("paper_id", "")),
        page_number=int(args.get("page_number", 0)),
    ),
    "save_annotation_draft": lambda args: mcp_tools.save_annotation_draft(
        paper_id=str(args.get("paper_id", "")),
        annotation_json=dict(args.get("annotation_json") or {}),
    ),
    "update_annotation_draft": lambda args: mcp_tools.update_annotation_draft(
        draft_id=str(args.get("draft_id", "")),
        annotation_json=dict(args.get("annotation_json") or {}),
    ),
}


def call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    if tool_name not in TOOL_HANDLERS:
        log_mcp_event(tool_name, 404)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool not found.")
    try:
        result = TOOL_HANDLERS[tool_name](arguments)
        log_mcp_event(
            tool_name,
            200,
            {
                "paper_id": arguments.get("paper_id"),
                "chunk_count": len(arguments.get("chunk_ids") or []),
                "query_len": len(str(arguments.get("query") or "")),
            },
        )
        return scrub_public_payload(result)
    except HTTPException as exc:
        log_mcp_event(tool_name, exc.status_code)
        raise


@router.get("/tools")
def list_tools() -> dict[str, Any]:
    return {"tools": _tools()}


@router.post("/tools/search_papers")
def rest_search_papers(payload: SearchPapersRequest) -> Any:
    return call_tool("search_papers", payload.model_dump())


@router.post("/tools/get_paper_metadata")
def rest_get_paper_metadata(payload: MetadataRequest) -> Any:
    return call_tool("get_paper_metadata", payload.model_dump())


@router.post("/tools/read_text_chunks")
def rest_read_text_chunks(payload: ReadChunksRequest) -> Any:
    return call_tool("read_text_chunks", payload.model_dump())


@router.post("/tools/read_page_text")
def rest_read_page_text(payload: ReadPageRequest) -> Any:
    return call_tool("read_page_text", payload.model_dump())


@router.post("/tools/save_annotation_draft")
def rest_save_annotation_draft(payload: SaveDraftRequest) -> Any:
    return call_tool("save_annotation_draft", payload.model_dump())


@router.post("/tools/update_annotation_draft")
def rest_update_annotation_draft(payload: UpdateDraftRequest) -> Any:
    return call_tool("update_annotation_draft", payload.model_dump())


@router.post("")
@router.post("/")
async def mcp_json_rpc(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "private-literature-mcp", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            tool_name = str(params.get("name") or "")
            arguments = dict(params.get("arguments") or {})
            tool_result = call_tool(tool_name, arguments)
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(tool_result, ensure_ascii=False),
                    }
                ],
                "isError": False,
            }
        elif method == "notifications/initialized":
            result = {}
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="method not found.")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    except HTTPException as exc:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": exc.status_code, "message": str(exc.detail)},
        }

