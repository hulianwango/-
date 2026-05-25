from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from .config import settings
from .database import db_session, utcnow


ALL_CATEGORIES = "__all__"
LOCKED_PAPERS_ROOT = Path("D:/OneDrive/桌面/论文文件集合")
WINDOWS_FORBIDDEN_CATEGORY_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def normalize_category_path(raw_path: str | None) -> str:
    raw_path = (raw_path or "").strip()
    if raw_path in {"", ALL_CATEGORIES}:
        return ""
    if (
        raw_path.startswith(("/", "\\"))
        or Path(raw_path).is_absolute()
        or re.match(r"^[A-Za-z]:", raw_path)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category path must be relative to the papers folder.",
        )

    normalized = raw_path.replace("\\", "/").strip("/")
    parts = [part.strip() for part in normalized.split("/")]
    if any(not part or part in {".", ".."} for part in parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category path contains an invalid segment.",
        )
    for part in parts:
        if WINDOWS_FORBIDDEN_CATEGORY_CHARS.search(part) or part.endswith((" ", ".")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category path contains characters that are invalid on Windows.",
            )
        name = part.split(".", 1)[0].upper()
        if name in WINDOWS_RESERVED_NAMES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category path uses a reserved Windows name.",
            )
    return "/".join(parts)


def papers_root() -> Path:
    configured_root = settings.papers_dir.resolve()
    locked_root = LOCKED_PAPERS_ROOT.resolve()
    if configured_root != locked_root:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="papers folder must be D:/OneDrive/桌面/论文文件集合.",
        )
    return locked_root


def _require_inside_papers_root(path: Path, detail: str) -> Path:
    root = papers_root()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        ) from exc
    return resolved


def category_dir(category_path: str) -> Path:
    category_path = normalize_category_path(category_path)
    root = papers_root()
    target = root if not category_path else root.joinpath(*category_path.split("/"))
    return _require_inside_papers_root(
        target,
        "category path must stay inside the papers folder.",
    )


def category_path_for_file(file_path: str | Path) -> str:
    try:
        folder = Path(file_path).resolve().parent
        relative = folder.relative_to(papers_root())
    except Exception:
        return ""
    if str(relative) == ".":
        return ""
    return relative.as_posix()


def category_label(category_path: str) -> str:
    return "根目录" if not category_path else category_path.split("/")[-1]


def paper_category_fields(file_path: str | Path) -> dict[str, str]:
    category_path = category_path_for_file(file_path)
    return {
        "category_path": category_path,
        "category_label": category_label(category_path),
    }


def _category_record(category_path: str, paper_count: int = 0) -> dict[str, Any]:
    return {
        "category_path": category_path,
        "category_label": category_label(category_path),
        "paper_count": paper_count,
    }


def list_paper_categories(include_empty: bool = True) -> list[dict[str, Any]]:
    root = papers_root()
    categories: dict[str, dict[str, Any]] = {"": _category_record("")}

    if include_empty and root.exists():
        for folder in root.rglob("*"):
            if not folder.is_dir():
                continue
            try:
                relative = folder.resolve().relative_to(root)
            except ValueError:
                continue
            category_path = "" if str(relative) == "." else relative.as_posix()
            categories.setdefault(category_path, _category_record(category_path))

    with db_session() as connection:
        rows = connection.execute("SELECT file_path FROM papers").fetchall()

    for row in rows:
        category_path = category_path_for_file(row["file_path"])
        record = categories.setdefault(category_path, _category_record(category_path))
        record["paper_count"] = int(record.get("paper_count") or 0) + 1

    records = [
        record
        for record in categories.values()
        if include_empty or int(record.get("paper_count") or 0) > 0
    ]
    return sorted(records, key=lambda item: (item["category_path"] != "", item["category_path"]))


def list_empty_categories() -> list[dict[str, Any]]:
    root = papers_root()
    if not root.exists() or not root.is_dir():
        return []

    empty_categories: list[dict[str, Any]] = []
    for folder in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
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
        category_path = relative.as_posix()
        empty_categories.append(_category_record(category_path))
    return sorted(empty_categories, key=lambda item: item["category_path"])


def delete_empty_category(category_path: str) -> dict[str, Any]:
    normalized_category = normalize_category_path(category_path)
    if not normalized_category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="root category cannot be deleted.",
        )

    target_dir = category_dir(normalized_category)
    if not target_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="category folder not found.",
        )
    if not target_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="category path is not a folder.",
        )
    try:
        has_contents = any(target_dir.iterdir())
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="category folder cannot be inspected.",
        ) from exc
    if has_contents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="category folder is not empty.",
        )

    try:
        target_dir.rmdir()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="empty category folder could not be deleted.",
        ) from exc

    return {
        "status": "deleted",
        "category_path": normalized_category,
        "category_label": category_label(normalized_category),
    }


def move_paper_file(
    paper_id: str,
    category_path: str,
    *,
    create_missing_category: bool = True,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    normalized_category = normalize_category_path(category_path)
    root = papers_root()
    if not root.exists() or not root.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="papers folder not found.",
        )

    target_dir = category_dir(normalized_category)

    overwritten = False
    backup_path: Path | None = None
    source_path: Path | None = None
    target_path: Path | None = None
    try:
        with db_session() as connection:
            row = connection.execute(
                "SELECT paper_id, file_path FROM papers WHERE paper_id = ?",
                (paper_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="paper not found.",
                )

            source_path = Path(row["file_path"]).resolve()
            _require_inside_papers_root(
                source_path,
                "paper file must already be inside the configured papers folder.",
            )
            if not source_path.exists() or not source_path.is_file():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="paper file not found.",
                )
            if source_path.suffix.lower() != ".pdf":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="only PDF paper files can be moved.",
                )
            if target_dir.exists() and not target_dir.is_dir():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="target category exists but is not a folder.",
                )
            if not target_dir.exists():
                if not create_missing_category:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="target category folder not found.",
                    )
                target_dir.mkdir(parents=True, exist_ok=True)

            target_path = _require_inside_papers_root(
                target_dir / source_path.name,
                "target file path must stay inside the papers folder.",
            )
            source_category = category_path_for_file(source_path)
            if source_path == target_path:
                return {
                    "paper_id": paper_id,
                    "status": "unchanged",
                    "category_path": normalized_category,
                    "category_label": category_label(normalized_category),
                    "source_category_path": source_category,
                    "target_category_path": normalized_category,
                }
            if target_path.exists():
                if not target_path.is_file():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="target file path exists but is not a file.",
                    )
                if not overwrite_existing:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "target_file_exists",
                            "message": "a file with the same name already exists in the target category.",
                        },
                    )
                backup_path = _overwrite_backup_path(target_path)
                target_path.rename(backup_path)
                overwritten = True

            source_path.rename(target_path)
            connection.execute(
                """
                UPDATE papers
                SET file_path = ?, updated_at = ?
                WHERE paper_id = ?
                """,
                (str(target_path), utcnow(), paper_id),
            )

        backup_cleanup_failed = False
        if backup_path is not None and backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                backup_cleanup_failed = True

        result = {
            "paper_id": paper_id,
            "status": "overwritten" if overwritten else "moved",
            "category_path": normalized_category,
            "category_label": category_label(normalized_category),
            "source_category_path": source_category,
            "target_category_path": normalized_category,
        }
        if backup_cleanup_failed:
            result["backup_cleanup"] = "failed"
        return result
    except HTTPException:
        if source_path is not None and target_path is not None:
            _rollback_file_move(source_path, target_path, backup_path)
        raise
    except Exception as exc:
        if source_path is not None and target_path is not None:
            _rollback_file_move(source_path, target_path, backup_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="paper file move failed.",
        ) from exc


def _overwrite_backup_path(target_path: Path) -> Path:
    for _ in range(100):
        candidate = target_path.with_name(
            f".{target_path.name}.overwrite-backup-{uuid.uuid4().hex}.tmp"
        )
        if not candidate.exists():
            return candidate
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="could not reserve a temporary overwrite backup path.",
    )


def _rollback_file_move(
    source_path: Path,
    target_path: Path,
    backup_path: Path | None = None,
) -> None:
    try:
        if target_path.exists() and not source_path.exists():
            target_path.rename(source_path)
        if backup_path is not None and backup_path.exists() and not target_path.exists():
            backup_path.rename(target_path)
    except Exception:
        pass
