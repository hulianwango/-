from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import paper_files
from .auto_classifier import classify_paper_record
from .database import db_session
from .indexer import hash_file, index_pdf_status


TEMP_CATEGORY_PATHS = {"", "新建文件夹", "plasmon", "待分类"}


def _pdf_files() -> list[Path]:
    root = paper_files.papers_root()
    return sorted(
        (path.resolve() for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: str(path).casefold(),
    )


def _db_papers(connection: sqlite3.Connection) -> list[Any]:
    return connection.execute(
        """
        SELECT
            paper_id, title, authors, year, journal, doi, file_path, file_hash,
            page_count, created_at, updated_at
        FROM papers
        ORDER BY updated_at DESC
        """
    ).fetchall()


def _paper_text_sample(connection: sqlite3.Connection, paper_id: str) -> str:
    rows = connection.execute(
        """
        SELECT text
        FROM paper_pages
        WHERE paper_id = ?
        ORDER BY page_number
        LIMIT 3
        """,
        (paper_id,),
    ).fetchall()
    return "\n".join(row["text"] or "" for row in rows)


def _safe_duplicate_target(source_path: Path, target_dir: Path, file_hash: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = (target_dir / source_path.name).resolve()
    if not candidate.exists():
        return candidate
    stem = source_path.stem
    suffix = source_path.suffix
    for index in range(1, 1000):
        candidate = (target_dir / f"{stem}.duplicate-{file_hash[:8]}-{index}{suffix}").resolve()
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail="could not reserve a duplicate target path.")


def _move_file(source_path: Path, target_path: Path, dry_run: bool) -> str:
    if source_path.resolve() == target_path.resolve():
        return "unchanged"
    if dry_run:
        return "planned"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.rename(target_path)
    return "moved"


def _empty_dirs() -> list[str]:
    root = paper_files.papers_root()
    empty: list[str] = []
    for folder in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        try:
            relative = folder.resolve().relative_to(root)
        except ValueError:
            continue
        if str(relative) == ".":
            continue
        try:
            if any(folder.iterdir()):
                continue
        except OSError:
            continue
        empty.append(relative.as_posix())
    return sorted(empty)


def _source_item(path: Path) -> dict[str, str]:
    root = paper_files.papers_root()
    category = paper_files.category_path_for_file(path)
    try:
        relative_path = path.resolve().relative_to(root).as_posix()
    except ValueError:
        relative_path = str(path)
    return {
        "path": relative_path,
        "name": path.name,
        "category_path": category,
    }


def organize_library(
    *,
    dry_run: bool = True,
    include_duplicates: bool = True,
    include_temp_folders: bool = True,
    apply_moves: bool = False,
) -> dict[str, Any]:
    dry_run = bool(dry_run or not apply_moves)
    root = paper_files.papers_root()
    pdfs = _pdf_files()

    with db_session() as connection:
        rows = _db_papers(connection)
        db_by_path = {str(Path(row["file_path"]).resolve()): row for row in rows}
        db_by_hash = {row["file_hash"]: row for row in rows}
        sample_texts = {row["paper_id"]: _paper_text_sample(connection, row["paper_id"]) for row in rows}

    duplicate_files: list[dict[str, Any]] = []
    unknown_files: list[dict[str, Any]] = []
    planned_moves: list[dict[str, Any]] = []
    moved = 0
    failed = 0

    for path in pdfs:
        resolved_path = str(path.resolve())
        file_hash = hash_file(path)
        db_row_at_path = db_by_path.get(resolved_path)
        db_row_for_hash = db_by_hash.get(file_hash)
        source = _source_item(path)

        if db_row_at_path is not None:
            source_category = source["category_path"]
            if not include_temp_folders or source_category not in TEMP_CATEGORY_PATHS:
                continue
            decision = classify_paper_record(db_row_at_path, sample_texts.get(db_row_at_path["paper_id"], ""))
            target_category = decision["category_path"]
            if source_category == target_category:
                continue
            move_item = {
                "type": "database_paper",
                "paper_id": db_row_at_path["paper_id"],
                "title": db_row_at_path["title"],
                "source": source,
                "target_category_path": target_category,
                "score": decision["score"],
                "reason": decision["reason"],
            }
            if dry_run:
                move_item["status"] = "planned"
            else:
                try:
                    move_result = paper_files.move_paper_file(
                        paper_id=db_row_at_path["paper_id"],
                        category_path=target_category,
                        create_missing_category=True,
                        overwrite_existing=False,
                    )
                    move_item["status"] = move_result.get("status", "moved")
                    moved += 0 if move_item["status"] == "unchanged" else 1
                except HTTPException as exc:
                    failed += 1
                    move_item["status"] = "failed"
                    move_item["error"] = exc.detail
            planned_moves.append(move_item)
            continue

        if db_row_for_hash is not None:
            duplicate_item = {
                "source": source,
                "matches_paper_id": db_row_for_hash["paper_id"],
                "matches_title": db_row_for_hash["title"],
                "canonical_category_path": paper_files.category_path_for_file(db_row_for_hash["file_path"]),
            }
            duplicate_files.append(duplicate_item)
            if include_duplicates:
                target_dir = Path(db_row_for_hash["file_path"]).resolve().parent
                target_path = _safe_duplicate_target(path, target_dir, file_hash)
                move_item = {
                    "type": "duplicate_file",
                    **duplicate_item,
                    "target": {
                        "path": target_path.relative_to(root).as_posix(),
                        "category_path": paper_files.category_path_for_file(target_path),
                    },
                }
                try:
                    status = _move_file(path, target_path, dry_run)
                    move_item["status"] = status
                    if status == "moved":
                        moved += 1
                except Exception as exc:
                    failed += 1
                    move_item["status"] = "failed"
                    move_item["error"] = type(exc).__name__
                planned_moves.append(move_item)
            continue

        unknown_item = {"source": source, "file_hash": file_hash}
        unknown_files.append(unknown_item)
        if dry_run:
            planned_moves.append({**unknown_item, "type": "new_pdf", "status": "needs_index"})
            continue

        try:
            with db_session() as connection:
                status = index_pdf_status(connection, path)
            unknown_item["paper_id"] = status.paper_id
            planned_moves.append({**unknown_item, "type": "new_pdf", "status": status.status})
        except Exception as exc:
            failed += 1
            planned_moves.append({**unknown_item, "type": "new_pdf", "status": "failed", "error": type(exc).__name__})

    return {
        "dry_run": dry_run,
        "root": str(root),
        "filesystem_pdf_count": len(pdfs),
        "database_paper_count": len(db_by_path),
        "duplicate_file_count": len(duplicate_files),
        "unknown_file_count": len(unknown_files),
        "planned_move_count": len([item for item in planned_moves if item.get("status") in {"planned", "needs_index"}]),
        "moved": moved,
        "failed": failed,
        "moves": planned_moves[:500],
        "duplicate_files": duplicate_files[:500],
        "unknown_files": unknown_files[:200],
        "suggested_delete_dirs": _empty_dirs(),
    }
