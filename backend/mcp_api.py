from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from . import mcp_tools
from .oauth_store import MOVE_FILE_SCOPE, READ_SCOPE, WRITE_DRAFT_SCOPE
from .security import (
    AuthContext,
    log_mcp_event,
    log_mcp_rpc_event,
    require_mcp_access,
    require_mcp_scopes,
    scrub_public_payload,
)


router = APIRouter()


class SearchPapersRequest(BaseModel):
    query: str = Field(min_length=1, description="Search query")
    limit: int = Field(default=5, ge=1, le=10)


class MetadataRequest(BaseModel):
    paper_id: str


class ReadChunksRequest(BaseModel):
    paper_id: str
    chunk_ids: list[str] = Field(min_length=1, max_length=5)


class ReadPageRequest(BaseModel):
    paper_id: str
    page_number: int = Field(ge=1)


class SaveDraftRequest(BaseModel):
    paper_id: str
    annotation_json: dict[str, Any]


class UpdateDraftRequest(BaseModel):
    draft_id: str
    annotation_json: dict[str, Any]


class ListCategoriesRequest(BaseModel):
    include_empty: bool = Field(
        default=True,
        description="Whether to include existing empty folders under the configured papers folder.",
    )


class MovePaperFileRequest(BaseModel):
    paper_id: str
    category_path: str = Field(
        description="Target folder relative to the configured papers folder. Use an empty string for the root folder."
    )
    create_missing_category: bool = Field(
        default=True,
        description="Create the target folder inside D:/OneDrive/桌面/论文文件集合 if it does not already exist.",
    )


@dataclass(frozen=True)
class ToolDefinition:
    description: str
    request_model: type[BaseModel]
    scopes: set[str]
    handler: Callable[[BaseModel], Any]


def _call_search_papers(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.search_papers(query=data["query"], limit=data["limit"])


def _call_get_paper_metadata(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.get_paper_metadata(paper_id=data["paper_id"])


def _call_read_text_chunks(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.read_text_chunks(paper_id=data["paper_id"], chunk_ids=data["chunk_ids"])


def _call_read_page_text(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.read_page_text(paper_id=data["paper_id"], page_number=data["page_number"])


def _call_save_annotation_draft(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.save_annotation_draft(
        paper_id=data["paper_id"],
        annotation_json=data["annotation_json"],
    )


def _call_update_annotation_draft(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.update_annotation_draft(
        draft_id=data["draft_id"],
        annotation_json=data["annotation_json"],
    )


def _call_list_paper_categories(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.list_paper_categories(include_empty=data["include_empty"])


def _call_move_paper_file(payload: BaseModel) -> Any:
    data = payload.model_dump()
    return mcp_tools.move_paper_file(
        paper_id=data["paper_id"],
        category_path=data["category_path"],
        create_missing_category=data["create_missing_category"],
    )


TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    "search_papers": ToolDefinition(
        description="Search the private literature database and return limited matching paper snippets.",
        request_model=SearchPapersRequest,
        scopes={READ_SCOPE},
        handler=_call_search_papers,
    ),
    "get_paper_metadata": ToolDefinition(
        description="Get safe metadata for one paper without exposing file paths or PDFs.",
        request_model=MetadataRequest,
        scopes={READ_SCOPE},
        handler=_call_get_paper_metadata,
    ),
    "read_text_chunks": ToolDefinition(
        description="Read limited original text chunks from a paper by chunk ids.",
        request_model=ReadChunksRequest,
        scopes={READ_SCOPE},
        handler=_call_read_text_chunks,
    ),
    "read_page_text": ToolDefinition(
        description="Read limited extracted text from one page of a paper.",
        request_model=ReadPageRequest,
        scopes={READ_SCOPE},
        handler=_call_read_page_text,
    ),
    "list_paper_categories": ToolDefinition(
        description="List folder categories under the configured papers folder without exposing local paths.",
        request_model=ListCategoriesRequest,
        scopes={READ_SCOPE},
        handler=_call_list_paper_categories,
    ),
    "move_paper_file": ToolDefinition(
        description=(
            "Move one indexed PDF paper into a relative target folder inside "
            "D:/OneDrive/桌面/论文文件集合. This never accepts absolute paths, never moves files "
            "outside that folder, and never overwrites an existing PDF."
        ),
        request_model=MovePaperFileRequest,
        scopes={MOVE_FILE_SCOPE},
        handler=_call_move_paper_file,
    ),
    "save_annotation_draft": ToolDefinition(
        description="Save AI-generated paper annotation as a pending draft only. Does not approve or write formal annotations.",
        request_model=SaveDraftRequest,
        scopes={WRITE_DRAFT_SCOPE},
        handler=_call_save_annotation_draft,
    ),
    "update_annotation_draft": ToolDefinition(
        description="Update an existing pending AI annotation draft.",
        request_model=UpdateDraftRequest,
        scopes={WRITE_DRAFT_SCOPE},
        handler=_call_update_annotation_draft,
    ),
}


def _input_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.setdefault("required", [])
    return schema


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": definition.description,
            "inputSchema": _input_schema(definition.request_model),
        }
        for name, definition in TOOL_DEFINITIONS.items()
    ]


def call_tool(tool_name: str, arguments: dict[str, Any], auth: AuthContext | None = None) -> Any:
    definition = TOOL_DEFINITIONS.get(tool_name)
    if definition is None:
        log_mcp_event(tool_name, 404)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool not found.")
    try:
        if auth is not None:
            require_mcp_scopes(auth, definition.scopes)
        payload = definition.request_model.model_validate(arguments)
        result = definition.handler(payload)
        log_mcp_event(
            tool_name,
            200,
            {
                "paper_id": getattr(payload, "paper_id", None),
                "chunk_count": len(getattr(payload, "chunk_ids", []) or []),
                "query_len": len(str(getattr(payload, "query", "") or "")),
            },
        )
        return scrub_public_payload(result)
    except ValidationError as exc:
        log_mcp_event(tool_name, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid tool arguments.",
        ) from exc
    except HTTPException as exc:
        log_mcp_event(tool_name, exc.status_code)
        raise


def _json_rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _json_rpc_success(rpc_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _handle_json_rpc_message(payload: Any, auth: AuthContext) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        log_mcp_rpc_event("invalid", False, 200, {"error": "invalid_request"})
        return _json_rpc_error(None, -32600, "Invalid Request")

    has_id = "id" in payload
    rpc_id = payload.get("id")
    method = str(payload.get("method") or "")
    raw_params = payload.get("params") or {}
    params = raw_params if isinstance(raw_params, dict) else {}

    if not method:
        log_mcp_rpc_event(method, has_id, 200, {"error": "missing_method"})
        if has_id:
            return _json_rpc_error(rpc_id, -32600, "Invalid Request")
        return None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "private-literature-mcp", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            if not isinstance(raw_params, dict):
                log_mcp_rpc_event(method, has_id, 200, {"error": "invalid_params"})
                return _json_rpc_error(rpc_id, -32602, "Invalid params")
            tool_name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                log_mcp_rpc_event(method, has_id, 200, {"error": "invalid_arguments"})
                return _json_rpc_error(rpc_id, -32602, "Invalid params")
            tool_result = call_tool(tool_name, arguments, auth)
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
            log_mcp_rpc_event(method, has_id, 200)
            if has_id:
                return _json_rpc_success(rpc_id, {})
            return {"jsonrpc": "2.0", "result": {}}
        else:
            log_mcp_rpc_event(method, has_id, 200, {"unknown": True})
            if has_id:
                return _json_rpc_error(rpc_id, -32601, "Method not found")
            return None

        log_mcp_rpc_event(method, has_id, 200)
        if has_id:
            return _json_rpc_success(rpc_id, result)
        return None
    except HTTPException as exc:
        log_mcp_rpc_event(method, has_id, 200, {"tool_status": exc.status_code})
        code = -32602 if exc.status_code == status.HTTP_400_BAD_REQUEST else -32000
        return _json_rpc_error(rpc_id, code, str(exc.detail))


@router.get("/mcp/tools")
def list_tools(auth: AuthContext = Depends(require_mcp_access)) -> dict[str, Any]:
    _ = auth
    return {"tools": _tools()}


@router.get("/mcp")
@router.get("/mcp/")
@router.head("/mcp")
@router.head("/mcp/")
def mcp_probe(auth: AuthContext = Depends(require_mcp_access)) -> JSONResponse:
    _ = auth
    return JSONResponse(
        {"detail": "Use POST /mcp for MCP JSON-RPC."},
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
    )


@router.post("/mcp/tools/search_papers")
def rest_search_papers(
    payload: SearchPapersRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("search_papers", payload.model_dump(), auth)


@router.post("/mcp/tools/get_paper_metadata")
def rest_get_paper_metadata(
    payload: MetadataRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("get_paper_metadata", payload.model_dump(), auth)


@router.post("/mcp/tools/read_text_chunks")
def rest_read_text_chunks(
    payload: ReadChunksRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("read_text_chunks", payload.model_dump(), auth)


@router.post("/mcp/tools/read_page_text")
def rest_read_page_text(
    payload: ReadPageRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("read_page_text", payload.model_dump(), auth)


@router.post("/mcp/tools/list_paper_categories")
def rest_list_paper_categories(
    payload: ListCategoriesRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("list_paper_categories", payload.model_dump(), auth)


@router.post("/mcp/tools/move_paper_file")
def rest_move_paper_file(
    payload: MovePaperFileRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("move_paper_file", payload.model_dump(), auth)


@router.post("/mcp/tools/save_annotation_draft")
def rest_save_annotation_draft(
    payload: SaveDraftRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("save_annotation_draft", payload.model_dump(), auth)


@router.post("/mcp/tools/update_annotation_draft")
def rest_update_annotation_draft(
    payload: UpdateDraftRequest,
    auth: AuthContext = Depends(require_mcp_access),
) -> Any:
    return call_tool("update_annotation_draft", payload.model_dump(), auth)


@router.post("/", response_model=None)
@router.post("/mcp", response_model=None)
@router.post("/mcp/", response_model=None)
async def mcp_json_rpc(
    request: Request,
    auth: AuthContext = Depends(require_mcp_access),
) -> JSONResponse | Response:
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
    except Exception:
        log_mcp_rpc_event("parse_error", False, 400)
        return JSONResponse(
            _json_rpc_error(None, -32700, "Parse error"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(payload, list):
        responses = [
            response
            for item in payload
            if (response := _handle_json_rpc_message(item, auth)) is not None
        ]
        if not responses:
            return Response(status_code=status.HTTP_200_OK)
        return JSONResponse(responses)

    response = _handle_json_rpc_message(payload, auth)
    if response is None:
        return Response(status_code=status.HTTP_200_OK)
    return JSONResponse(response)
