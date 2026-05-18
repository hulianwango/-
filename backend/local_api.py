from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .database import db_session, utcnow
from .indexer import scan_papers
from .mcp_tools import search_papers
from .security import require_local_request, scrub_public_payload


router = APIRouter(prefix="/local", dependencies=[Depends(require_local_request)])
LOCAL_LIBRARY_LIMIT = 1000
LOCAL_LIBRARY_LIMIT_MAX = 5000


class DraftUpdateRequest(BaseModel):
    annotation_json: dict[str, Any]


def _paper_public(row) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "paper_id": row["paper_id"],
        "title": row["title"],
        "authors": row["authors"],
        "year": row["year"],
        "journal": row["journal"],
        "doi": row["doi"],
    }


@router.post("/scan")
def scan_library() -> dict[str, Any]:
    return scan_papers()


@router.get("/papers")
def list_or_search_papers(query: str = "", limit: int = LOCAL_LIBRARY_LIMIT) -> list[dict[str, Any]]:
    query = query.strip()
    if query:
        return search_papers(query=query, limit=min(limit, 10))

    limit = max(1, min(limit, LOCAL_LIBRARY_LIMIT_MAX))
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT paper_id, title, authors, year, journal, doi
            FROM papers
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return scrub_public_payload([_paper_public(row) for row in rows])


@router.get("/papers/{paper_id}")
def get_local_paper(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT paper_id, title, authors, year, journal, doi
            FROM papers
            WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
    return scrub_public_payload(_paper_public(row))


@router.get("/papers/{paper_id}/pdf")
def read_local_pdf(paper_id: str) -> FileResponse:
    with db_session() as connection:
        row = connection.execute(
            "SELECT file_path FROM papers WHERE paper_id = ?", (paper_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
    pdf_path = Path(row["file_path"])
    if not pdf_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pdf not found.")
    return FileResponse(pdf_path, media_type="application/pdf")


@router.get("/drafts")
def list_drafts(status_filter: str = "pending") -> list[dict[str, Any]]:
    allowed_statuses = {"pending", "accepted", "rejected", "all"}
    if status_filter not in allowed_statuses:
        status_filter = "pending"

    where = "" if status_filter == "all" else "WHERE d.status = ?"
    params: tuple[Any, ...] = () if status_filter == "all" else (status_filter,)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                d.draft_id,
                d.paper_id,
                d.annotation_json,
                d.status,
                p.title,
                p.authors,
                p.year,
                p.journal,
                p.doi
            FROM paper_ai_drafts AS d
            JOIN papers AS p ON p.paper_id = d.paper_id
            {where}
            ORDER BY d.updated_at DESC
            """,
            params,
        ).fetchall()

    drafts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["annotation_json"] = json.loads(item["annotation_json"])
        drafts.append(item)
    return scrub_public_payload(drafts)


@router.put("/drafts/{draft_id}")
def update_local_draft(draft_id: str, payload: DraftUpdateRequest) -> dict[str, Any]:
    now = utcnow()
    with db_session() as connection:
        row = connection.execute(
            "SELECT paper_id, status FROM paper_ai_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft not found.")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only pending drafts can be edited.",
            )
        connection.execute(
            """
            UPDATE paper_ai_drafts
            SET annotation_json = ?, updated_at = ?
            WHERE draft_id = ?
            """,
            (json.dumps(payload.annotation_json, ensure_ascii=False), now, draft_id),
        )
    return {"draft_id": draft_id, "paper_id": row["paper_id"], "status": "pending"}


@router.post("/drafts/{draft_id}/accept")
def accept_draft(draft_id: str) -> dict[str, Any]:
    now = utcnow()
    annotation_id = f"a_{uuid.uuid4().hex[:20]}"

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT draft_id, paper_id, annotation_json, status
            FROM paper_ai_drafts
            WHERE draft_id = ?
            """,
            (draft_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft not found.")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only pending drafts can be accepted.",
            )

        annotation = json.loads(row["annotation_json"])
        paper_id = row["paper_id"]
        connection.execute(
            """
            INSERT INTO paper_annotations (
                annotation_id, paper_id, source_draft_id, annotation_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (annotation_id, paper_id, draft_id, json.dumps(annotation, ensure_ascii=False), now, now),
        )

        mechanism_summary = annotation.get("mechanism_summary") or annotation.get("mechanisms")
        if mechanism_summary:
            connection.execute(
                """
                INSERT INTO paper_mechanisms (
                    paper_id, mechanism_summary, evidence_json, page_numbers_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    str(mechanism_summary),
                    json.dumps(annotation.get("evidence", []), ensure_ascii=False),
                    json.dumps(annotation.get("page_numbers", []), ensure_ascii=False),
                    now,
                    now,
                ),
            )

        relevance_fields = {
            "is_colloid",
            "au_shape",
            "au_size_nm",
            "lspr_peak_nm",
            "er_host",
            "involves_520_nm",
            "involves_540_nm",
            "involves_red_emission",
            "supports_red_emission_design",
            "warns_green_channel_enhancement",
            "mechanism_summary",
            "relevance_score",
        }
        if any(key in annotation for key in relevance_fields):
            connection.execute(
                """
                INSERT INTO project_relevance (
                    paper_id, is_colloid, au_shape, au_size_nm, lspr_peak_nm, er_host,
                    involves_520_nm, involves_540_nm, involves_red_emission,
                    supports_red_emission_design, warns_green_channel_enhancement,
                    mechanism_summary, relevance_score, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    is_colloid = excluded.is_colloid,
                    au_shape = excluded.au_shape,
                    au_size_nm = excluded.au_size_nm,
                    lspr_peak_nm = excluded.lspr_peak_nm,
                    er_host = excluded.er_host,
                    involves_520_nm = excluded.involves_520_nm,
                    involves_540_nm = excluded.involves_540_nm,
                    involves_red_emission = excluded.involves_red_emission,
                    supports_red_emission_design = excluded.supports_red_emission_design,
                    warns_green_channel_enhancement = excluded.warns_green_channel_enhancement,
                    mechanism_summary = excluded.mechanism_summary,
                    relevance_score = excluded.relevance_score,
                    updated_at = excluded.updated_at
                """,
                (
                    paper_id,
                    _bool_int(annotation.get("is_colloid")),
                    annotation.get("au_shape"),
                    annotation.get("au_size_nm"),
                    annotation.get("lspr_peak_nm"),
                    annotation.get("er_host"),
                    _bool_int(annotation.get("involves_520_nm")),
                    _bool_int(annotation.get("involves_540_nm")),
                    _bool_int(annotation.get("involves_red_emission")),
                    _bool_int(annotation.get("supports_red_emission_design")),
                    _bool_int(annotation.get("warns_green_channel_enhancement")),
                    annotation.get("mechanism_summary"),
                    annotation.get("relevance_score"),
                    now,
                    now,
                ),
            )

        for tag in annotation.get("recommended_tags") or []:
            if isinstance(tag, str) and tag.strip():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_tags (paper_id, tag, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (paper_id, tag.strip(), now),
                )

        connection.execute(
            "UPDATE paper_ai_drafts SET status = 'accepted', updated_at = ? WHERE draft_id = ?",
            (now, draft_id),
        )

    return {"draft_id": draft_id, "paper_id": paper_id, "status": "accepted"}


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: str) -> dict[str, Any]:
    now = utcnow()
    with db_session() as connection:
        row = connection.execute(
            "SELECT paper_id, status FROM paper_ai_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft not found.")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only pending drafts can be rejected.",
            )
        connection.execute(
            "UPDATE paper_ai_drafts SET status = 'rejected', updated_at = ? WHERE draft_id = ?",
            (now, draft_id),
        )
    return {"draft_id": draft_id, "paper_id": row["paper_id"], "status": "rejected"}


def _bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0
