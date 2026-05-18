from __future__ import annotations

import json
import re
import uuid
from typing import Any

from fastapi import HTTPException, status

from .config import settings
from .database import db_session, utcnow
from .security import enforce_public_keys, scrub_public_payload


ANNOTATION_FIELDS = {
    "main_work",
    "material_system",
    "methods",
    "key_results",
    "mechanisms",
    "evidence",
    "page_numbers",
    "relevance_to_project",
    "recommended_tags",
    "limitations",
    "confidence",
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


def _limit_text_budget(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = settings.max_response_chars
    limited: list[dict[str, Any]] = []
    for row in rows:
        clean = dict(row)
        snippet = str(clean.get("snippet") or "")
        if remaining <= 0:
            clean["snippet"] = ""
        elif len(snippet) > remaining:
            clean["snippet"] = snippet[: max(0, remaining - 3)].rstrip() + "..."
            remaining = 0
        else:
            remaining -= len(snippet)
        limited.append(clean)
    return limited


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff-]+", query, flags=re.UNICODE)
    if not tokens:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query is required.")
    quoted_tokens = []
    for token in tokens[:12]:
        quoted_tokens.append('"' + token.replace('"', '""') + '"')
    return " OR ".join(quoted_tokens)


def _paper_exists(connection, paper_id: str) -> bool:  # type: ignore[no-untyped-def]
    row = connection.execute(
        "SELECT 1 FROM papers WHERE paper_id = ? LIMIT 1", (paper_id,)
    ).fetchone()
    return row is not None


def search_papers(query: str, limit: int = 10) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query is required.")
    limit = max(1, min(int(limit or 10), settings.max_search_limit))

    with db_session() as connection:
        try:
            rows = connection.execute(
                """
                SELECT
                    p.paper_id,
                    p.title,
                    p.authors,
                    p.year,
                    p.journal,
                    p.doi,
                    f.page_number,
                    f.chunk_id,
                    snippet(paper_chunks_fts, 5, '', '', ' ... ', 32) AS snippet,
                    bm25(paper_chunks_fts) AS score
                FROM paper_chunks_fts AS f
                JOIN papers AS p ON p.paper_id = f.paper_id
                WHERE paper_chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (_fts_query(query), limit),
            ).fetchall()
        except Exception:
            rows = connection.execute(
                """
                SELECT
                    p.paper_id,
                    p.title,
                    p.authors,
                    p.year,
                    p.journal,
                    p.doi,
                    c.page_number,
                    c.chunk_id,
                    substr(c.text, 1, 800) AS snippet,
                    0.0 AS score
                FROM paper_chunks AS c
                JOIN papers AS p ON p.paper_id = c.paper_id
                WHERE c.text LIKE ? OR p.title LIKE ? OR p.doi LIKE ?
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()

    records = [enforce_public_keys(dict(row)) for row in rows]
    return scrub_public_payload(_limit_text_budget(records))


def get_paper_metadata(paper_id: str) -> dict[str, Any]:
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
    return scrub_public_payload(enforce_public_keys(dict(row)))


def read_text_chunks(paper_id: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
    if not chunk_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="chunk_ids are required.")
    chunk_ids = chunk_ids[: settings.max_chunks_per_request]
    placeholders = ",".join("?" for _ in chunk_ids)

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                p.paper_id,
                p.title,
                p.authors,
                p.year,
                p.journal,
                p.doi,
                c.page_number,
                c.chunk_id,
                c.text AS snippet
            FROM paper_chunks AS c
            JOIN papers AS p ON p.paper_id = c.paper_id
            WHERE c.paper_id = ? AND c.chunk_id IN ({placeholders})
            ORDER BY c.page_number, c.chunk_index
            """,
            (paper_id, *chunk_ids),
        ).fetchall()

    records = [enforce_public_keys(dict(row)) for row in rows]
    return scrub_public_payload(_limit_text_budget(records))


def read_page_text(paper_id: str, page_number: int) -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                p.paper_id,
                p.title,
                p.authors,
                p.year,
                p.journal,
                p.doi,
                pg.page_number,
                pg.text AS snippet
            FROM paper_pages AS pg
            JOIN papers AS p ON p.paper_id = pg.paper_id
            WHERE pg.paper_id = ? AND pg.page_number = ?
            """,
            (paper_id, int(page_number)),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="page not found.")
    return scrub_public_payload(_limit_text_budget([enforce_public_keys(dict(row))])[0])


def _validate_annotation(annotation_json: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(annotation_json, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="annotation_json must be an object."
        )
    clean = {key: value for key, value in annotation_json.items() if key in ANNOTATION_FIELDS}
    if not clean:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="annotation_json has no allowed fields."
        )
    return clean


def save_annotation_draft(paper_id: str, annotation_json: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_annotation(annotation_json)
    now = utcnow()
    draft_id = f"d_{uuid.uuid4().hex[:20]}"

    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        connection.execute(
            """
            INSERT INTO paper_ai_drafts (
                draft_id, paper_id, annotation_json, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (draft_id, paper_id, json.dumps(clean, ensure_ascii=False), now, now),
        )
    return {"paper_id": paper_id, "draft_id": draft_id, "status": "pending"}


def update_annotation_draft(draft_id: str, annotation_json: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_annotation(annotation_json)
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
                detail="only pending drafts can be updated.",
            )
        connection.execute(
            """
            UPDATE paper_ai_drafts
            SET annotation_json = ?, updated_at = ?
            WHERE draft_id = ?
            """,
            (json.dumps(clean, ensure_ascii=False), now, draft_id),
        )
    return {"paper_id": row["paper_id"], "draft_id": draft_id, "status": "pending"}
