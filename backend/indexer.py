from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from .config import settings
from .database import db_session, utcnow


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class PdfPage:
    page_number: int
    text: str


@dataclass
class ExtractedPdf:
    paper_id: str
    file_hash: str
    title: str
    authors: str
    year: int | None
    journal: str
    doi: str
    pages: list[PdfPage]


@dataclass(frozen=True)
class IndexPdfStatus:
    paper_id: str
    indexed: bool
    status: str
    source_path: str = ""
    duplicate_of_path: str = ""


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def paper_id_from_hash(file_hash: str) -> str:
    return f"p_{file_hash[:16]}"


def wait_for_stable_pdf(path: Path, *, timeout_seconds: float = 30.0, interval_seconds: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[int, int] | None = None
    stable_count = 0
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            stable_count = 0
            last_signature = None
            time.sleep(interval_seconds)
            continue
        signature = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size > 0 and signature == last_signature:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0
            last_signature = signature
        time.sleep(interval_seconds)
    return False


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _metadata_value(metadata: dict, key: str) -> str:
    value = metadata.get(key) or ""
    return normalize_text(str(value))


def _guess_year(*texts: str) -> int | None:
    for text in texts:
        match = YEAR_RE.search(text or "")
        if match:
            return int(match.group(0))
    return None


def _guess_doi(text: str) -> str:
    match = DOI_RE.search(text or "")
    return match.group(0).rstrip(".") if match else ""


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            split_at = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if split_at > start + chunk_size // 2:
                end = split_at + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(0, end - overlap)
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def extract_pdf(path: Path, file_hash: str | None = None) -> ExtractedPdf:
    file_hash = file_hash or hash_file(path)
    paper_id = paper_id_from_hash(file_hash)
    pages: list[PdfPage] = []

    with fitz.open(path) as document:
        metadata = document.metadata or {}
        for index, page in enumerate(document, start=1):
            pages.append(PdfPage(page_number=index, text=normalize_text(page.get_text("text"))))

    first_pages = "\n".join(page.text for page in pages[:3])
    title = _metadata_value(metadata, "title") or path.stem
    authors = _metadata_value(metadata, "author")
    doi = _guess_doi(first_pages)
    year = _guess_year(_metadata_value(metadata, "creationDate"), first_pages, path.stem)

    return ExtractedPdf(
        paper_id=paper_id,
        file_hash=file_hash,
        title=title,
        authors=authors,
        year=year,
        journal="",
        doi=doi,
        pages=pages,
    )


def _page_rows_are_current(connection: sqlite3.Connection, paper_id: str, page_count: int) -> bool:
    if page_count <= 0:
        return False
    row = connection.execute(
        "SELECT COUNT(*) AS page_rows FROM paper_pages WHERE paper_id = ?",
        (paper_id,),
    ).fetchone()
    return int(row["page_rows"] if row is not None else 0) == page_count


def _chunk_rows_are_current(connection: sqlite3.Connection, paper_id: str) -> bool:
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM paper_pages WHERE paper_id = ? AND length(trim(text)) > 0)
                AS text_pages,
            (SELECT COUNT(*) FROM paper_chunks WHERE paper_id = ?) AS chunks,
            (SELECT COUNT(*) FROM paper_chunks_fts WHERE paper_id = ?) AS fts_chunks
        """,
        (paper_id, paper_id, paper_id),
    ).fetchone()
    if row is None:
        return False
    text_pages = int(row["text_pages"] or 0)
    chunks = int(row["chunks"] or 0)
    fts_chunks = int(row["fts_chunks"] or 0)
    if text_pages > 0 and chunks == 0:
        return False
    return chunks == fts_chunks


def _unchanged_index_status(
    connection: sqlite3.Connection,
    *,
    paper_id: str,
    file_hash: str,
    path: Path,
) -> IndexPdfStatus | None:
    row = connection.execute(
        """
        SELECT paper_id, file_hash, file_path, page_count
        FROM papers
        WHERE paper_id = ?
        """,
        (paper_id,),
    ).fetchone()
    if row is None or row["file_hash"] != file_hash:
        return None

    page_count = int(row["page_count"] or 0)
    if not _page_rows_are_current(connection, paper_id, page_count):
        return None
    if not _chunk_rows_are_current(connection, paper_id):
        return None

    current_path = str(path)
    if row["file_path"] != current_path:
        existing_path = Path(row["file_path"])
        if existing_path.exists():
            return IndexPdfStatus(
                paper_id=paper_id,
                indexed=False,
                status="duplicate_path",
                source_path=current_path,
                duplicate_of_path=row["file_path"],
            )
        connection.execute(
            """
            UPDATE papers
            SET file_path = ?, updated_at = ?
            WHERE paper_id = ?
            """,
            (current_path, utcnow(), paper_id),
        )
        return IndexPdfStatus(paper_id=paper_id, indexed=False, status="path_updated")

    return IndexPdfStatus(paper_id=paper_id, indexed=False, status="unchanged")


def index_pdf_status(connection: sqlite3.Connection, path: Path) -> IndexPdfStatus:
    file_hash = hash_file(path)
    paper_id = paper_id_from_hash(file_hash)
    unchanged = _unchanged_index_status(
        connection,
        paper_id=paper_id,
        file_hash=file_hash,
        path=path,
    )
    if unchanged is not None:
        return unchanged

    extracted = extract_pdf(path, file_hash=file_hash)
    now = utcnow()

    connection.execute(
        """
        INSERT INTO papers (
            paper_id, title, authors, year, journal, doi, file_path, file_hash,
            page_count, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id) DO UPDATE SET
            title = excluded.title,
            authors = excluded.authors,
            year = excluded.year,
            journal = excluded.journal,
            doi = excluded.doi,
            file_path = excluded.file_path,
            file_hash = excluded.file_hash,
            page_count = excluded.page_count,
            updated_at = excluded.updated_at
        """,
        (
            extracted.paper_id,
            extracted.title,
            extracted.authors,
            extracted.year,
            extracted.journal,
            extracted.doi,
            str(path),
            extracted.file_hash,
            len(extracted.pages),
            now,
            now,
        ),
    )

    connection.execute("DELETE FROM paper_pages WHERE paper_id = ?", (extracted.paper_id,))
    connection.execute("DELETE FROM paper_chunks WHERE paper_id = ?", (extracted.paper_id,))
    connection.execute("DELETE FROM paper_chunks_fts WHERE paper_id = ?", (extracted.paper_id,))

    for page in extracted.pages:
        connection.execute(
            """
            INSERT INTO paper_pages (paper_id, page_number, text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (extracted.paper_id, page.page_number, page.text, now, now),
        )

        for chunk_index, chunk in enumerate(
            _chunk_text(page.text, settings.chunk_size, settings.chunk_overlap), start=1
        ):
            chunk_id = f"{extracted.paper_id}_p{page.page_number:04d}_c{chunk_index:03d}"
            connection.execute(
                """
                INSERT INTO paper_chunks (
                    chunk_id, paper_id, page_number, chunk_index, text, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    extracted.paper_id,
                    page.page_number,
                    chunk_index,
                    chunk,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_chunks_fts (
                    paper_id, chunk_id, page_number, title, authors, text
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    extracted.paper_id,
                    chunk_id,
                    page.page_number,
                    extracted.title,
                    extracted.authors,
                    chunk,
                ),
            )
    return IndexPdfStatus(paper_id=extracted.paper_id, indexed=True, status="indexed")


def index_pdf(connection: sqlite3.Connection, path: Path) -> str:
    return index_pdf_status(connection, path).paper_id


def _index_pdf_with_savepoint(connection: sqlite3.Connection, path: Path) -> IndexPdfStatus:
    connection.execute("SAVEPOINT index_one_pdf")
    try:
        return index_pdf_status(connection, path)
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT index_one_pdf")
        raise
    finally:
        connection.execute("RELEASE SAVEPOINT index_one_pdf")


def _empty_classification_result() -> dict[str, Any]:
    return {
        "dry_run": False,
        "candidates": 0,
        "moved": 0,
        "unchanged": 0,
        "skipped_review": 0,
        "conflicts": 0,
        "failed": 0,
        "renamed": 0,
        "duplicate_file_count": 0,
        "results": [],
    }


def _merge_classification_results(*results: dict[str, Any]) -> dict[str, Any]:
    merged = _empty_classification_result()
    for result in results:
        if not result:
            continue
        for key in (
            "candidates",
            "planned",
            "moved",
            "unchanged",
            "skipped_review",
            "conflicts",
            "failed",
            "renamed",
            "duplicate_file_count",
            "unknown_file_count",
        ):
            merged[key] = int(merged.get(key) or 0) + int(result.get(key) or 0)
        merged.setdefault("results", []).extend(result.get("results") or [])
        for key in ("strict", "hierarchy_order", "target_prefix", "review_policy", "duplicate_policy", "rename_policy", "root"):
            if key in result:
                merged[key] = result[key]
    merged["results"] = merged.get("results", [])[:500]
    return merged


def _auto_classify_after_index(
    paper_ids: list[str] | None = None,
    duplicate_paths: list[str] | None = None,
    duplicate_paper_ids: list[str] | None = None,
) -> dict[str, Any]:
    try:
        from .auto_classifier import classify_unclassified_papers

        results: list[dict[str, Any]] = []
        if paper_ids:
            results.append(
                classify_unclassified_papers(
                    paper_ids=paper_ids,
                    strict=True,
                    include_classified=True,
                    include_duplicates=False,
                    min_auto_score=8,
                    review_policy="best_guess",
                    duplicate_policy="duplicate_zone",
                    rename_policy="chinese_brief_work",
                )
            )
        if duplicate_paths:
            results.append(
                classify_unclassified_papers(
                    paper_ids=duplicate_paper_ids,
                    source_paths=duplicate_paths,
                    include_paper_paths=False,
                    strict=True,
                    include_classified=True,
                    include_duplicates=False,
                    min_auto_score=8,
                    review_policy="best_guess",
                    duplicate_policy="duplicate_zone",
                    rename_policy="chinese_brief_work",
                )
            )
        return _merge_classification_results(*results) if results else _empty_classification_result()
    except Exception as exc:
        return {
            "dry_run": False,
            "candidates": 0,
            "moved": 0,
            "unchanged": 0,
            "failed": 1,
            "results": [{"status": "failed", "error": type(exc).__name__}],
        }


def scan_papers(auto_classify: bool = True) -> dict[str, Any]:
    if not settings.papers_dir.exists():
        return {"scanned": 0, "indexed": 0, "failed": 0, "errors": ["papers_dir_missing"]}

    pdfs = [path for path in settings.papers_dir.rglob("*.pdf") if path.is_file()]
    indexed = 0
    skipped = 0
    indexed_paper_ids: list[str] = []
    duplicate_paper_ids: list[str] = []
    duplicate_paths: list[str] = []
    errors: list[str] = []

    with db_session() as connection:
        for path in pdfs:
            try:
                stat = path.stat()
                is_recent = time.time() - stat.st_mtime < 30
                if is_recent and not wait_for_stable_pdf(
                    path,
                    timeout_seconds=10.0,
                    interval_seconds=0.4,
                ):
                    errors.append("file_not_stable")
                    continue
                status = _index_pdf_with_savepoint(connection, path)
                if status.indexed:
                    indexed += 1
                    indexed_paper_ids.append(status.paper_id)
                elif status.status == "duplicate_path" and status.source_path and is_recent:
                    skipped += 1
                    duplicate_paper_ids.append(status.paper_id)
                    duplicate_paths.append(status.source_path)
                else:
                    skipped += 1
            except Exception as exc:
                errors.append(type(exc).__name__)

    result: dict[str, Any] = {
        "scanned": len(pdfs),
        "indexed": indexed,
        "skipped": skipped,
        "failed": len(errors),
        "errors": errors[:10],
    }
    if auto_classify:
        classification = (
            _auto_classify_after_index(
                indexed_paper_ids,
                duplicate_paths=duplicate_paths,
                duplicate_paper_ids=duplicate_paper_ids,
            )
            if indexed_paper_ids or duplicate_paths
            else _empty_classification_result()
        )
        result["classification"] = classification
        result["auto_classified"] = classification.get("moved", 0)
        result["auto_classify_failed"] = classification.get("failed", 0)
    return result


def _pdf_event_handler_class():  # type: ignore[no-untyped-def]
    from watchdog.events import FileSystemEventHandler

    class PdfHandler(FileSystemEventHandler):
        def _index_and_classify(self, raw_path: str) -> None:
            path = Path(raw_path)
            if not wait_for_stable_pdf(path):
                return
            with db_session() as connection:
                status = index_pdf_status(connection, path)
            if status.indexed:
                _auto_classify_after_index([status.paper_id])
            elif status.status == "duplicate_path" and status.source_path:
                _auto_classify_after_index(
                    duplicate_paths=[status.source_path],
                    duplicate_paper_ids=[status.paper_id],
                )

        def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory and str(event.src_path).lower().endswith(".pdf"):
                self._index_and_classify(event.src_path)

        def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory and str(event.src_path).lower().endswith(".pdf"):
                self._index_and_classify(event.src_path)

    return PdfHandler


def start_paper_watch_observer():  # type: ignore[no-untyped-def]
    from watchdog.observers import Observer

    observer = Observer()
    observer.schedule(_pdf_event_handler_class()(), str(settings.papers_dir), recursive=True)
    observer.start()
    return observer


def watch_and_index() -> None:
    observer = start_paper_watch_observer()
    try:
        while True:
            observer.join(1)
    finally:
        observer.stop()
        observer.join()
